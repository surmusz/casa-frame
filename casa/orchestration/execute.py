"""Stage 执行与 Plan 波次执行。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..artifact import ArtifactSchemaValidator, ArtifactStore
from ..audit import emit_audit
from ..config import get_config
from ..hooks import HookRegistry
from ..observability import get_metrics_sink, log_event, timed_stage
from ..recovery import RecoveryChain, RecoveryContext, default_recovery_chain
from .models import Plan, Stage

ValidatorFn = Callable[[str, dict], list[str]]
logger = logging.getLogger("casa.orchestration")

class StageExecutionError(Exception):
    """Stage 执行失败（已尝试所有容错手段）。"""

    def __init__(
        self,
        stage_id: str,
        agent_id: str,
        error: str,
        *,
        partial_results: dict[str, Any] | None = None,
    ):
        self.stage_id = stage_id
        self.agent_id = agent_id
        self.error = error
        self.partial_results = dict(partial_results or {})
        super().__init__(f"Stage {stage_id} ({agent_id}) failed: {error}")


@dataclass(kw_only=True)
class StageResult:
    """单个 stage 的执行结果。"""

    stage_id: str
    agent_id: str
    success: bool
    artifact_kind: str = ""
    error: str = ""
    skipped: bool = False
    quality_score: float = 1.0
    approval_required: bool = False
    approval_status: str = ""
    interaction_request: dict[str, Any] | None = None
    interaction_response: dict[str, Any] | None = None
    eval_score: float = 1.0
    context_utilization: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "agent_id": self.agent_id,
            "success": self.success,
            "artifact_kind": self.artifact_kind,
            "error": self.error,
            "skipped": self.skipped,
            "approval_required": self.approval_required,
            "approval_status": self.approval_status,
        }


class StageRunner:
    """
    单 stage 执行器：容错链 = 简单重试 × N → 新会话重试 × M → replan → 失败。

    使用方式：
        runner = StageRunner(
            store=artifact_store,
            executor=my_agent_executor,
            schema_validator=ArtifactSchemaValidator(),
        )
        result = await runner.run(stage, plan, completed_stage_ids)
    """

    def __init__(
        self,
        *,
        store: ArtifactStore,
        executor: Any,  # 领域项目实现（AgentExecutor）
        schema_validator: ArtifactSchemaValidator | ValidatorFn | None = None,
        simple_retries: int | None = None,
        fresh_session_retries: int | None = None,
        stage_timeout_seconds: float | None = None,
        ws_sender: Callable[[dict], Awaitable[None]] | None = None,
        recovery_chain: RecoveryChain | None = None,
        hooks: HookRegistry | None = None,
        cache_backend: Any | None = None,
        interrupt_ctrl: Any | None = None,
        agent_memory: Any | None = None,
    ):
        config = get_config()
        self.store = store
        self.executor = executor
        self.schema_validator = schema_validator
        self.simple_retries = simple_retries if simple_retries is not None else config.stage_simple_retries
        self.fresh_session_retries = (
            fresh_session_retries
            if fresh_session_retries is not None
            else config.stage_fresh_session_retries
        )
        self.stage_timeout = stage_timeout_seconds
        self._ws_send = ws_sender
        self._recovery_chain = recovery_chain or default_recovery_chain(
            self.simple_retries, self.fresh_session_retries,
        )
        self._hooks = hooks or HookRegistry()
        self._cache_backend = cache_backend
        self._interrupt_ctrl = interrupt_ctrl
        self._pending_interactions: dict[str, dict[str, Any]] = {}
        self._interaction_history: list[dict[str, Any]] = []
        self._agent_memory = agent_memory

    @staticmethod
    def _estimate_tokens(data: Any) -> int:
        import json
        try:
            return len(json.dumps(data, ensure_ascii=False)) // 4
        except (TypeError, ValueError):
            return 0

    def _trim_context(self, ctx: dict[str, Any], budget: int) -> dict[str, Any]:
        if budget <= 0:
            ctx["_meta"] = ctx.get("_meta", {})
            ctx["_meta"]["context_trimmed"] = list(ctx.get("inputs", {}).keys())
            ctx["inputs"] = {"_trimmed": f"budget={budget}, all inputs dropped"}
            return ctx
        total = sum(self._estimate_tokens(v) for v in ctx.get("inputs", {}).values())
        if total <= budget:
            return ctx

        sorted_kinds = sorted(
            ctx["inputs"].keys(),
            key=lambda k: self._estimate_tokens(ctx["inputs"][k]),
            reverse=True,
        )
        trimmed: list[str] = []
        for kind in sorted_kinds:
            if total <= budget:
                break
            data = ctx["inputs"][kind]
            est = self._estimate_tokens(data)
            if isinstance(data, dict):
                ctx["inputs"][kind] = {
                    k: (str(v)[:200] + "…" if len(str(v)) > 200 else v)
                    for k, v in list(data.items())[:20]
                }
            elif isinstance(data, str):
                ctx["inputs"][kind] = data[: max(budget // 4, 1)] + "…[truncated]"
            trimmed.append(kind)
            total -= est - self._estimate_tokens(ctx["inputs"][kind])

        if trimmed:
            ctx["_meta"] = ctx.get("_meta", {})
            ctx["_meta"]["context_trimmed"] = trimmed
            ctx["_meta"]["original_estimate"] = total
        return ctx

    async def _record_memory(
        self,
        stage: Stage,
        artifact_kind: str,
        *,
        outcome: str,
        quality_score: float = 1.0,
        error_type: str = "",
    ) -> None:
        if not self._agent_memory:
            return
        from ..memory import MemoryRecord
        record = MemoryRecord(
            record_id=f"{stage.stage_id}:{artifact_kind}",
            agent_id=stage.agent_id,
            artifact_kind=artifact_kind,
            outcome=outcome,
            error_type=error_type,
            quality_score=quality_score,
        )
        await self._agent_memory.record(record)

    async def run(
        self,
        stage: Stage,
        plan: Plan,
        completed_stage_ids: set[str],
    ) -> StageResult:
        """
        执行单个 stage，含容错链。

        参数:
            stage: 待执行的 stage
            plan: 所属 plan
            completed_stage_ids: 已完成的 stage_id 集合

        返回:
            StageResult

        抛出:
            StageExecutionError: 所有容错手段耗尽
        """
        agent_id = stage.agent_id
        stage_id = stage.stage_id
        artifact_kind = stage.output_artifact_kind or agent_id

        if stage.pre_completed:
            if not self.store.exists(artifact_kind):
                log_event(
                    "stage.pre_completed_cleared", stage_id=stage_id, agent_id=agent_id,
                    artifact_kind=artifact_kind, reason="artifact_missing",
                )
            else:
                log_event(
                    "stage.skipped", stage_id=stage_id, agent_id=agent_id,
                    artifact_kind=artifact_kind, reason="pre_completed",
                )
                return StageResult(
                    stage_id=stage_id, agent_id=agent_id,
                    success=True, artifact_kind=artifact_kind, skipped=True,
                )

        if self._cache_backend and stage.input_refs:
            policy = plan.usage_policy
            allow_cache_skip = bool(policy and policy.allow_skip_core_if_artifacts_exist)
            if allow_cache_skip:
                from ..cache import cache_key as _cache_key, inputs_fingerprint
                fp = inputs_fingerprint(
                    self.store, stage.input_refs, extract_kind=self._extract_kind_from_ref,
                )
                ck = _cache_key(
                    artifact_kind, stage.input_refs, stage.params,
                    tenant_id=self.store.tenant_id or "",
                    job_id=self.store.job_id,
                    plan_id=self.store.plan_id,
                    inputs_fingerprint=fp,
                )
                cached = self._cache_backend.get(ck)
                if cached is not None:
                    validation_errs = self._validate(stage, cached, artifact_kind)
                    if not validation_errs:
                        self.store.write(artifact_kind, cached)
                        log_event("cache.hit", stage_id=stage_id, artifact_kind=artifact_kind, cache_key=ck)
                        return StageResult(
                            stage_id=stage_id, agent_id=agent_id,
                            success=True, artifact_kind=artifact_kind, skipped=True,
                        )
                    self._cache_backend.invalidate_key(ck)
                    log_event(
                        "cache.invalidated",
                        stage_id=stage_id,
                        artifact_kind=artifact_kind,
                        reason="schema_mismatch",
                    )

        # --- 幂等 skip（须 UsagePolicy 显式允许）---
        policy = plan.usage_policy
        allow_skip = bool(policy and policy.allow_skip_core_if_artifacts_exist)
        if allow_skip and self.store.exists(artifact_kind):
            existing = self.store.read(artifact_kind)
            if existing is not None and not self._validate(stage, existing, artifact_kind):
                logger.info("Skipping %s — artifact %s already exists", stage_id, artifact_kind)
                log_event("stage.skipped", stage_id=stage_id, agent_id=agent_id, artifact_kind=artifact_kind)
                return StageResult(
                    stage_id=stage_id, agent_id=agent_id,
                    success=True, artifact_kind=artifact_kind, skipped=True,
                )
            if existing is not None:
                self.store.delete(artifact_kind)

        with timed_stage(stage_id, agent_id):
            from ..events import publish_event
            publish_event("stage_start", stage_id=stage_id, agent_id=agent_id)
            await self._hooks.fire("stage_start", stage=stage, plan=plan)
            try:
                result = await self._run_with_retries(stage, plan, artifact_kind, stage_id, agent_id)
            except Exception as exc:
                publish_event("stage_error", stage_id=stage_id, agent_id=agent_id, error=str(exc))
                self._record_failure(agent_id, exc)
                await self._hooks.fire("stage_error", stage=stage, error=exc)
                raise
            publish_event("stage_end", stage_id=stage_id, agent_id=agent_id)
            await self._hooks.fire("stage_end", stage=stage, result=result)
            pending = self._pending_interactions.pop(stage_id, None)
            if pending:
                result.interaction_request = pending
                if self._interrupt_ctrl:
                    self._interrupt_ctrl.pause(
                        f"Agent {stage.agent_id} 请求交互: {pending.get('message', '')}",
                        after="stage",
                    )
            return result

    async def _run_with_retries(
        self,
        stage: Stage,
        plan: Plan,
        artifact_kind: str,
        stage_id: str,
        agent_id: str,
    ) -> StageResult:
        mitigations: list[str] = []

        async def execute_fn(fresh_session: bool) -> dict:
            return await self._execute(stage, plan, fresh_session=fresh_session)

        def validate_fn(data: dict) -> list[str]:
            return self._validate(stage, data, artifact_kind)

        def write_fn(data: dict) -> None:
            self._write(stage, data, artifact_kind)

        outcome, data, last_error = await self._recovery_chain.execute(
            stage,
            execute_fn,
            validate_fn=validate_fn,
            write_fn=write_fn,
            context=RecoveryContext(stage_id=stage_id, agent_id=agent_id, artifact_kind=artifact_kind),
        )

        if outcome == "success":
            log_event("stage.success", stage_id=stage_id, agent_id=agent_id, artifact_kind=artifact_kind)
            emit_audit("stage.completed", actor=agent_id, stage_id=stage_id, artifact_kind=artifact_kind)
            result = StageResult(stage_id=stage_id, agent_id=agent_id, success=True, artifact_kind=artifact_kind)
            await self._record_memory(stage, artifact_kind, outcome="success", quality_score=result.quality_score)
            return result

        if outcome == "skipped":
            return StageResult(
                stage_id=stage_id, agent_id=agent_id, success=True,
                artifact_kind=artifact_kind, skipped=True, error=last_error,
            )

        log_event("stage.failed", stage_id=stage_id, agent_id=agent_id, error=last_error, level=logging.ERROR)
        if self._ws_send:
            await self._ws_send({
                "type": "stage_error",
                "stage_id": stage_id,
                "agent_id": agent_id,
                "error": last_error,
                "mitigations": mitigations,
            })
        raise StageExecutionError(stage_id, agent_id, last_error)

    @staticmethod
    def _record_failure(agent_id: str, error: Exception) -> None:
        from ..observability import record_metric
        record_metric(
            "stage.failure", 1.0,
            agent_id=agent_id,
            error_type=type(error).__name__,
        )

    @staticmethod
    def _extract_kind_from_ref(ref_str: str) -> str:
        if ":artifact:" in ref_str:
            return ref_str.rsplit(":artifact:", 1)[-1]
        return ref_str

    def _build_execute_context(
        self, stage: Stage, plan: Plan, *, fresh_session: bool,
    ) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        upstream_status: dict[str, str] = {}
        for ref_str in stage.input_refs:
            kind = self._extract_kind_from_ref(ref_str)
            data = self.store.read(kind)
            if data is not None:
                inputs[kind] = data
                upstream_status[kind] = "ok"
            else:
                upstream_status[kind] = "missing"

        eval_targets_data: dict[str, Any] = {}
        if stage.stage_role == "evaluator":
            stage_map = {s.stage_id: s for s in plan.stages}
            for tid in stage.eval_targets:
                target = stage_map.get(tid)
                if target is None:
                    continue
                ak = target.output_artifact_kind or target.agent_id
                eval_targets_data[tid] = {
                    "artifact": self.store.read(ak),
                    "agent_id": target.agent_id,
                    "artifact_kind": ak,
                }

        if stage.context_limit_tokens and len(inputs) > 10:
            if stage.context_limit_tokens < 32000:
                keys = list(inputs.keys())[:10]
                inputs = {k: inputs[k] for k in keys}
                log_event(
                    "stage.context_truncated",
                    stage_id=stage.stage_id,
                    agent_id=stage.agent_id,
                    kept=len(keys),
                )

        coordination_hints: dict[str, dict] = {}
        for ref_str in stage.input_refs:
            kind = self._extract_kind_from_ref(ref_str)
            hint = self.store.read_coordination_hint(kind)
            if hint:
                coordination_hints[kind] = hint

        ctx: dict[str, Any] = {
            "stage_id": stage.stage_id,
            "input_refs": stage.input_refs,
            "inputs": inputs,
            "upstream_status": upstream_status,
            "coordination_hints": coordination_hints,
            "eval_targets_data": eval_targets_data,
            "eval_criteria": list(stage.eval_criteria),
            "interaction_history": list(self._interaction_history),
            "injected_prompt": stage.injected_prompt,
            "params": stage.params,
            "fresh_session": fresh_session,
            "model_preference": stage.model_preference,
            "context_limit_tokens": stage.context_limit_tokens,
            "llm_config": get_config().get_llm_config(stage.model_preference or None),
            "sandbox": {
                "enforced": True,
                "max_memory_mb": stage.sandbox_memory_mb,
                "max_time_seconds": self.stage_timeout or 300,
                "network": stage.sandbox_network,
                "filesystem": stage.sandbox_filesystem,
            },
        }
        ctx = self._trim_context(ctx, stage.context_limit_tokens)
        return ctx

    async def _execute(self, stage: Stage, plan: Plan, *, fresh_session: bool = False) -> dict:
        ctx = self._build_execute_context(stage, plan, fresh_session=fresh_session)

        if hasattr(self.executor, "execute_streaming") and self._ws_send:
            async def on_chunk(chunk_type: str, data: Any) -> None:
                if self._ws_send:
                    await self._ws_send({
                        "type": f"stage.chunk.{chunk_type}",
                        "stage_id": stage.stage_id,
                        "agent_id": stage.agent_id,
                        "data": data,
                    })
            coro = self.executor.execute_streaming(stage.agent_id, ctx, on_chunk=on_chunk)
        else:
            coro = self.executor.execute(stage.agent_id, ctx)
        if self.stage_timeout:
            return await asyncio.wait_for(coro, timeout=self.stage_timeout)
        return await coro

    def _validate(self, stage: Stage, data: dict, artifact_kind: str = "") -> list[str]:
        if not self.schema_validator:
            return []
        ak = artifact_kind or stage.output_artifact_kind or stage.agent_id
        if isinstance(self.schema_validator, ArtifactSchemaValidator):
            return self.schema_validator.validate(ak, data)
        return self.schema_validator(ak, data)

    def _write(self, stage: Stage, data: dict, artifact_kind: str) -> None:
        from ..observability import get_run_context, record_metric

        payload = dict(data)
        meta: dict[str, Any] = {}
        if "_meta" in payload:
            meta = payload.pop("_meta")
            interaction = meta.pop("interaction_request", None)
            if interaction:
                self._pending_interactions[stage.stage_id] = interaction
            for key in ("tokens_in", "tokens_out", "tokens_total"):
                if key in meta:
                    record_metric(
                        f"stage.{key}",
                        float(meta[key]),
                        stage_id=stage.stage_id,
                        agent_id=stage.agent_id,
                        model=str(meta.get("model", "")),
                    )
            ctx = get_run_context()
            if ctx and ctx.tenant_id and "tokens_total" in meta:
                from ..tenant import get_tenant_manager
                get_tenant_manager().record_token_usage(
                    ctx.tenant_id, int(meta["tokens_total"]),
                )

        self.store.write(artifact_kind, payload)

        if self._cache_backend and stage.input_refs:
            from ..cache import cache_key as _cache_key, inputs_fingerprint
            fp = inputs_fingerprint(
                self.store, stage.input_refs, extract_kind=self._extract_kind_from_ref,
            )
            ck = _cache_key(
                artifact_kind, stage.input_refs, stage.params,
                tenant_id=self.store.tenant_id or "",
                job_id=self.store.job_id,
                plan_id=self.store.plan_id,
                inputs_fingerprint=fp,
            )
            self._cache_backend.put(ck, payload, metadata=meta or None)


# ============================================================================
# 波次并行执行（PlanExecutor）
# ============================================================================



from .plan_executor import PlanExecutor
