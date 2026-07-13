"""Orchestrator 编排入口。"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable

from ..audit import emit_audit
from ..config import get_config
from ..hooks import HookRegistry
from ..observability import get_metrics_sink, get_run_context, log_event, run_context
from .compile import PlanCompiler, PlanNormalizer
from .execute import PlanExecutor, StageExecutionError, StageResult
from .gates import PolicyEnforcementHook, QualityGate, QualityGateHook
from .models import CompileRequest, CompileResult, Plan, Stage, UsagePolicy

logger = logging.getLogger("casa.orchestration")

class Orchestrator:
    """
    CASA 编排器入口：编译 → normalize → 执行。

    使用方式：
        orch = Orchestrator(
            compiler=PlanCompiler(agent_io_map=..., presets=...),
            normalizer=PlanNormalizer(core_pipeline_ids=...),
            executor=PlanExecutor(store=..., stage_runner=...),
        )
        result = await orch.run(CompileRequest(preset_id="full"))
    """

    def __init__(
        self,
        *,
        compiler: PlanCompiler,
        normalizer: PlanNormalizer | None = None,
        executor: PlanExecutor,
        scheduler: Any | None = None,
        hooks: HookRegistry | None = None,
        interrupt_ctrl: Any | None = None,
        lifecycle_manager: Any | None = None,
        policy_engine: Any | None = None,
        quality_gate: QualityGate | None = None,
    ):
        self.compiler = compiler
        self.normalizer = normalizer
        self.executor = executor
        self.scheduler = scheduler
        self.hooks = hooks or HookRegistry()
        self._interrupt_ctrl = interrupt_ctrl
        self._lifecycle_manager = lifecycle_manager
        self._policy_engine = policy_engine
        if interrupt_ctrl is not None:
            executor._interrupt_ctrl = interrupt_ctrl
        executor._deadlock_recovery = self._recover_plan_from_deadlock
        if policy_engine is not None:
            self.hooks.register(
                PolicyEnforcementHook(policy_engine, interrupt_ctrl),
                priority=50,
            )
        if quality_gate is not None:
            self.hooks.register(
                QualityGateHook(quality_gate, interrupt_ctrl, self),
                priority=60,
            )
        self._current_plan: Plan | None = None
        self._completed_agent_ids: set[str] = set()
        self._rejected_stages: set[str] = set()
        self._pending_approvals: dict[str, StageResult] = {}
        self._interaction_results: dict[str, StageResult] = {}
        self._pending_plans: dict[str, Plan] = {}
        self._approved_plans: dict[str, Plan] = {}

    def _prepare_store_for_plan(
        self,
        plan: Plan,
        *,
        job_id: str = "",
        tenant_id: str = "",
        force: bool = False,
    ) -> None:
        """同步 store 上下文；未 init 或 force 时绑定 plan 目录。"""
        store = self.executor.store
        scope_changed = (
            (job_id and job_id != store.job_id)
            or (tenant_id and tenant_id != store.tenant_id)
        )
        if job_id:
            store.job_id = job_id
        if tenant_id:
            store.tenant_id = tenant_id
        if force or not store._plan_dir or scope_changed:
            store.init_plan(plan.plan_id)
        elif store._plan_id != plan.plan_id and not plan.previous_plan_id:
            store.init_plan(plan.plan_id)

    def _track_interactions(self, stage_results: dict[str, StageResult]) -> None:
        for sid, sr in stage_results.items():
            if sr.interaction_request and not sr.interaction_response:
                self._interaction_results[sid] = sr

    def _detect_conflict(self, old_plan: Plan, new_plan: Plan) -> dict[str, Any]:
        old_ids = {s.stage_id for s in old_plan.stages}
        new_ids = {s.stage_id for s in new_plan.stages}
        overlap = old_ids & new_ids
        overlap_ratio = len(overlap) / max(len(old_ids), 1)
        divergent = list(old_ids - new_ids)
        new_stages = list(new_ids - old_ids)
        if overlap_ratio >= 0.7:
            level, recommendation = "minor", "continue"
        elif overlap_ratio >= 0.3:
            level, recommendation = "major", "abort_current_wave"
        else:
            level, recommendation = "major", "abort_current_wave"
        return {
            "conflict_level": level,
            "overlap_ratio": round(overlap_ratio, 2),
            "divergent_stages": divergent,
            "new_stages": new_stages,
            "recommendation": recommendation,
        }

    async def respond_to(self, stage_id: str, response: dict[str, Any]) -> bool:
        """向等待交互的 stage 注入用户响应。"""
        result = self._interaction_results.get(stage_id)
        if result is None:
            partial = getattr(self.executor, "_partial_results", {})
            result = partial.get(stage_id)
        if result is None or not result.interaction_request:
            return False
        result.interaction_response = response
        runner = self.executor.stage_runner
        if result.interaction_request:
            runner._interaction_history.append({
                "stage_id": stage_id,
                "request": result.interaction_request,
                "response": response,
            })
        self._interaction_results.pop(stage_id, None)
        self.resume()
        return True

    async def approve_plan(self, plan_id: str) -> bool:
        plan = self._pending_plans.pop(plan_id, None)
        if plan is None:
            return False
        plan.plan_type = "approved"
        self._approved_plans[plan_id] = plan
        return True

    async def run_approved_plan(
        self,
        plan_id: str,
        *,
        run_id: str | None = None,
        session_id: str = "",
        job_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        auto_render: bool | None = None,
    ) -> CompileResult:
        """执行已通过审批的 plan。"""
        plan = self._approved_plans.get(plan_id)
        if plan is None:
            raise ValueError(f"未找到已审批 plan: {plan_id}")
        trace_id = run_id or f"run_{uuid.uuid4().hex}"
        effective_tenant = tenant_id or get_config().tenant_id
        self._prepare_store_for_plan(
            plan, job_id=job_id, tenant_id=effective_tenant, force=True,
        )
        try:
            with run_context(
                run_id=trace_id,
                session_id=session_id,
                user_id=user_id,
                job_id=job_id,
                tenant_id=effective_tenant,
                plan_id=plan.plan_id,
            ):
                self._current_plan = plan
                stage_results = await self.executor.execute(plan)
                self._track_interactions(stage_results)
        except Exception:
            raise
        self._approved_plans.pop(plan_id, None)
        stage_agent = {s.stage_id: s.agent_id for s in plan.stages}
        self._completed_agent_ids = {
            stage_agent[sid]
            for sid, sr in stage_results.items()
            if sr.success and sid in stage_agent
        }
        result = CompileResult(plan=plan, stage_results=stage_results)
        should_render = (
            auto_render if auto_render is not None else get_config().auto_render_deliverable
        )
        if should_render and plan.deliverable_type:
            result.deliverable_output = await self._render_deliverable(
                plan.deliverable_type, self.executor.store,
            )
        return result

    async def reject_plan(self, plan_id: str, *, reason: str = "") -> bool:
        plan = self._pending_plans.pop(plan_id, None)
        if plan is None:
            return False
        plan.plan_type = "rejected"
        log_event("plan.rejected", plan_id=plan_id, reason=reason)
        return True

    def cost_breakdown(self) -> dict[str, Any]:
        """从 stage metrics 中提取每个 Agent 的 token 汇总。"""
        sink = get_metrics_sink()
        metrics = sink.snapshot()
        agent_stats: dict[str, dict[str, float]] = {}
        for rec in metrics:
            aid = rec.get("tags", {}).get("agent_id", "")
            if not aid:
                continue
            if aid not in agent_stats:
                agent_stats[aid] = {
                    "tokens_in": 0.0, "tokens_out": 0.0,
                    "tokens_total": 0.0, "duration_ms": 0.0,
                }
            name = rec.get("name", "")
            if name == "stage.tokens_total":
                agent_stats[aid]["tokens_total"] += float(rec["value"])
            elif name == "stage.tokens_in":
                agent_stats[aid]["tokens_in"] += float(rec["value"])
            elif name == "stage.tokens_out":
                agent_stats[aid]["tokens_out"] += float(rec["value"])
            elif name == "stage.duration_ms":
                agent_stats[aid]["duration_ms"] += float(rec["value"])
        total_tokens = sum(s["tokens_total"] for s in agent_stats.values())
        return {
            "total_tokens": total_tokens,
            "per_agent": agent_stats,
            "model_breakdown": {},
        }

    async def run_in_loop(
        self,
        intent: str,
        *,
        max_iterations: int = 5,
        require_double_pass: bool = True,
        verifier: Callable | None = None,
        review_gate: Callable | None = None,
        router: Any | None = None,
        **run_kwargs: Any,
    ) -> Any:
        """在 Agent Loop 中运行——plan→execute→verify→iterate。"""
        from ..loop import AgentLoop

        loop = AgentLoop(
            orchestrator=self,
            verifier=verifier,
            review_gate=review_gate,
            max_iterations=max_iterations,
            require_double_pass=require_double_pass,
        )
        return await loop.run(intent, router=router, **run_kwargs)

    async def run(
        self,
        request: CompileRequest,
        *,
        run_id: str | None = None,
        session_id: str = "",
        job_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        auto_render: bool | None = None,
        lifecycle_manager: Any | None = None,
        auto_cleanup: bool = False,
    ) -> CompileResult:
        """
        完整运行 compile → normalize → execute [→ render]。

        注入优先级：构造函数参数（hooks/recovery_chain）> 全局单例（event_bus 等）。

        参数:
            request: 编译请求
            run_id: 可选 correlation id（默认自动生成）
            session_id: 会话 id（用于日志上下文）
            job_id: 任务 id（用于日志上下文）
            user_id: 用户 id（用于日志上下文）
            tenant_id: 租户 id（用于日志上下文，默认取全局配置）
            auto_render: 执行成功后调用 DeliverableRegistry.render()；
                None 时使用 CASAConfig.auto_render_deliverable

        返回:
            CompileResult（含 Plan、stage 结果，可选 deliverable_output）
        """
        trace_id = run_id or f"run_{uuid.uuid4().hex}"
        effective_tenant = tenant_id or get_config().tenant_id
        with run_context(
            run_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            job_id=job_id,
            tenant_id=effective_tenant,
        ):
            log_event("orchestrator.compile_start", preset_id=request.preset_id)
            await self.hooks.fire("compile_start", request=request)
            result = self.compiler.compile(request)
            await self.hooks.fire("compile_end", result=result)

            if result.plan.plan_type == "pending_review":
                if self.normalizer:
                    result.plan = self.normalizer.normalize(
                        result.plan,
                        deliverable_type=request.deliverable_type,
                    )
                self._pending_plans[result.plan.plan_id] = result.plan
                log_event("plan.pending_review", plan_id=result.plan.plan_id)
                return result

            if self._policy_engine is not None:
                from ..policy import RulePhase
                triggered = self._policy_engine.evaluate(
                    RulePhase.POST_COMPILE, plan=result.plan,
                )
                for item in triggered:
                    if item["action"] == "deny":
                        result.warnings.append(item.get("action_message", item["rule_id"]))
                        return result

            if self.normalizer:
                log_event("orchestrator.normalize_start", plan_id=result.plan.plan_id)
                await self.hooks.fire("normalize_start", plan=result.plan)
                result.plan = self.normalizer.normalize(
                    result.plan,
                    deliverable_type=request.deliverable_type,
                )
                await self.hooks.fire("normalize_end", plan=result.plan)

            with run_context(plan_id=result.plan.plan_id, job_id=job_id):
                config = get_config()
                self._prepare_store_for_plan(
                    result.plan, job_id=job_id, tenant_id=effective_tenant,
                )
                if config.dry_run:
                    log_event("orchestrator.dry_run_skip_execute", plan_id=result.plan.plan_id)
                    stage_results = {}
                else:
                    log_event(
                        "orchestrator.execute_start",
                        plan_id=result.plan.plan_id,
                        stage_count=len(result.plan.stages),
                    )
                    await self.hooks.fire("execute_start", plan=result.plan)
                    self._current_plan = result.plan
                    stage_results = await self.executor.execute(result.plan)
                    self._track_interactions(stage_results)
                    stage_agent = {s.stage_id: s.agent_id for s in result.plan.stages}
                    self._completed_agent_ids = {
                        stage_agent[sid]
                        for sid, sr in stage_results.items()
                        if sr.success and sid in stage_agent
                    }
                    await self.hooks.fire("execute_end", plan=result.plan, results=stage_results)
                result.stage_results = stage_results

            success_count = sum(1 for r in stage_results.values() if r.success)
            log_event(
                "orchestrator.complete",
                plan_id=result.plan.plan_id,
                stage_count=len(result.plan.stages),
                success_count=success_count,
            )
            logger.info(
                "Plan executed: %d stages, %d success",
                len(result.plan.stages),
                success_count,
            )

            should_render = (
                auto_render if auto_render is not None else get_config().auto_render_deliverable
            )
            if should_render and not config.dry_run and request.deliverable_type:
                result.deliverable_output = await self._render_deliverable(
                    request.deliverable_type,
                    self.executor.store,
                )

            mgr = lifecycle_manager or self._lifecycle_manager
            if auto_cleanup and mgr is not None and not config.dry_run:
                ctx = get_run_context()
                cleanup = mgr.cleanup_plan(
                    self.executor.store,
                    result.plan.plan_id,
                    job_id or (ctx.job_id if ctx else ""),
                )
                if cleanup.get("removed"):
                    log_event(
                        "lifecycle.cleanup",
                        plan_id=result.plan.plan_id,
                        removed_count=len(cleanup["removed"]),
                    )

            return result

    async def run_from_intent(
        self,
        intent: str,
        *,
        router: Any,
        run_id: str | None = None,
        session_id: str = "",
        job_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        deliverable_type: str = "full",
        auto_render: bool | None = None,
    ) -> CompileResult:
        """
        一条龙：自然语言意图 → Agent 选择 → compile → normalize → execute → render。
        """
        route_result = await router.route(intent, deliverable_type=deliverable_type)

        seed_stages = [{"agent_id": aid} for aid in route_result.agent_ids]
        policy_cls = {
            "for_user_start": UsagePolicy.for_user_start,
            "for_patch": UsagePolicy.for_patch,
            "for_preview": UsagePolicy.for_preview,
        }
        policy_factory = policy_cls.get(route_result.policy, UsagePolicy.for_user_start)

        request = CompileRequest(
            preset_id="",
            seed_stages=seed_stages,
            deliverable_type=deliverable_type,
            policy=policy_factory(),
            intent_summary=intent,
        )

        result = await self.run(
            request,
            run_id=run_id,
            session_id=session_id,
            job_id=job_id,
            user_id=user_id,
            tenant_id=tenant_id,
            auto_render=auto_render,
        )
        result.warnings.extend(route_result.warnings)
        return result

    def pause(self, reason: str = "", *, after: str = "wave") -> None:
        if self._interrupt_ctrl:
            self._interrupt_ctrl.pause(reason, after=after)

    def resume(self) -> None:
        if self._interrupt_ctrl:
            self._interrupt_ctrl.resume()

    def abort(self, reason: str = "", *, graceful: bool = True) -> None:
        if self._interrupt_ctrl:
            self._interrupt_ctrl.abort(reason, graceful=graceful)

    async def approve(self, stage_id: str, *, comment: str = "") -> bool:
        result = self._pending_approvals.pop(stage_id, None)
        if result is None:
            return False
        result.approval_status = "approved"
        self.resume()
        return True

    async def reject(self, stage_id: str, *, reason: str = "") -> bool:
        result = self._pending_approvals.pop(stage_id, None)
        if result is None:
            return False
        result.approval_status = "rejected"
        self._rejected_stages.add(stage_id)
        self.resume()
        return True

    async def _recover_plan_from_deadlock(
        self,
        plan: Plan,
        pending: set[str],
        completed: set[str],
        results: dict[str, StageResult],
    ) -> Plan | None:
        from .plan_recovery import recover_plan_from_deadlock
        return await recover_plan_from_deadlock(self, plan, pending, completed, results)

    async def replan(
        self,
        *,
        intent: str = "",
        deliverable_type: str = "",
        additional_agents: list[str] | None = None,
        router: Any | None = None,
        **run_kwargs: Any,
    ) -> CompileResult:
        from .plan_recovery import replan_pipeline
        return await replan_pipeline(
            self,
            intent=intent,
            deliverable_type=deliverable_type,
            additional_agents=additional_agents,
            router=router,
            **run_kwargs,
        )

    async def debug_trace(self, run_id: str, *, include_io: bool = False) -> dict[str, Any]:
        from .facade_ops import debug_trace as _debug_trace
        return await _debug_trace(self, run_id, include_io=include_io)

    async def _render_deliverable(self, deliverable_type: str, store: ArtifactStore) -> Any | None:
        from .facade_ops import render_deliverable
        return await render_deliverable(self, deliverable_type, store)

    def health_check(self) -> dict[str, Any]:
        from .facade_ops import health_check as _health_check
        return _health_check(self)

