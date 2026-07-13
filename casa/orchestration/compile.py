"""Plan 编译与 normalize。"""
from __future__ import annotations

import logging
from typing import Any

from ..artifact import ArtifactDAG
from .models import CompileRequest, CompileResult, Plan, Preset, Stage, UsagePolicy

logger = logging.getLogger("casa.orchestration")

class PlanCompiler:
    """
    Plan Compiler：确定性编译 + IO 图自动闭包。

    使用方式：
        compiler = PlanCompiler(
            agent_io_map={
                "intel_a": (["raw_data"], "product_profile"),
                "analyst_b": (["product_profile"], "analytics"),
            },
            core_pipeline_ids={"intel_a"},
            presets={
                "full": Preset(preset_id="full", selected_agent_ids=["intel_a", "analyst_b"]),
            },
        )
        result = compiler.compile(CompileRequest(preset_id="full"))
        for s in result.plan.stages:
            print(s.stage_id, s.depends_on)
    """

    def __init__(
        self,
        *,
        agent_io_map: dict[str, tuple[list[str], str]],
        core_pipeline_ids: set[str] | None = None,
        required_agent_ids: set[str] | None = None,
        presets: dict[str, Preset] | None = None,
        capability_matrix: Any | None = None,
    ):
        """
        参数:
            agent_io_map: {agent_id: ([input_artifact_kinds], output_artifact_kind)}
            core_pipeline_ids: 核心流水线 agent，始终包含
            required_agent_ids: 强制包含的 agent
            presets: 预设模板
        """
        self.agent_io_map = agent_io_map
        self.core_pipeline_ids = core_pipeline_ids or set()
        self.required_agent_ids = required_agent_ids or set()
        self.presets = presets or {}
        self.dag = ArtifactDAG.from_declarations(agent_io_map)
        self.capability_matrix = capability_matrix

    def _model_pref(self, agent_id: str) -> str:
        if self.capability_matrix is None:
            return ""
        row = self.capability_matrix.get(agent_id)
        return row.model_preference if row else ""

    def _context_limit(self, agent_id: str) -> int:
        if self.capability_matrix is None:
            return 128000
        row = self.capability_matrix.get(agent_id)
        return row.context_limit_tokens if row else 128000

    def _priority(self, agent_id: str) -> int:
        if self.capability_matrix is None:
            return 100
        row = self.capability_matrix.get(agent_id)
        return row.priority if row else 100

    def _sandbox_fields(self, agent_id: str) -> dict[str, Any]:
        if self.capability_matrix is None:
            return {}
        row = self.capability_matrix.get(agent_id)
        if row is None:
            return {}
        return {
            "sandbox_memory_mb": row.sandbox_memory_mb,
            "sandbox_network": row.sandbox_network,
            "sandbox_filesystem": row.sandbox_filesystem,
        }

    def _capability_rows(self) -> dict[str, Any]:
        if self.capability_matrix is None:
            return {}
        if hasattr(self.capability_matrix, "to_rows"):
            return {r.agent_id: r for r in self.capability_matrix.to_rows()}
        if hasattr(self.capability_matrix, "_rows"):
            return dict(self.capability_matrix._rows)
        return {}

    def _inject_evaluators(self, stages: list[Stage]) -> list[Stage]:
        from ..config import get_config

        max_eval = get_config().max_eval_stages_per_plan
        if max_eval == 0:
            return stages

        evaluator_agents = {
            aid for aid, row in self._capability_rows().items()
            if getattr(row, "is_evaluator", False)
        }
        if not evaluator_agents:
            return stages

        new_stages: list[Stage] = []
        eval_added = 0
        for stage in stages:
            new_stages.append(stage)
            if stage.stage_role != "producer":
                continue
            for eid in evaluator_agents:
                if eid == stage.agent_id:
                    continue
                if eval_added >= max_eval:
                    logger.warning(
                        "eval stage 注入已达上限 %d，跳过后续 evaluator",
                        max_eval,
                    )
                    break
                eval_stage = Stage(
                    stage_id=f"{stage.stage_id}_eval_{eid}",
                    agent_id=eid,
                    stage_role="evaluator",
                    eval_targets=[stage.stage_id],
                    eval_criteria=["quality", "consistency"],
                    depends_on=[stage.stage_id],
                    output_artifact_kind=f"{stage.output_artifact_kind}_eval",
                    model_preference=self._model_pref(eid),
                    context_limit_tokens=self._context_limit(eid),
                    priority=self._priority(eid),
                    **self._sandbox_fields(eid),
                )
                new_stages.append(eval_stage)
                eval_added += 1
        return new_stages

    def compile(self, request: CompileRequest, *, review_mode: bool = False) -> CompileResult:
        """编译 Plan。review_mode=True 时标记为 pending_review。"""
        if request.review_mode:
            review_mode = True
        warnings: list[str] = []
        # preset 的 usage_policy 覆盖 request 的默认值
        policy = request.policy

        # 1. 解析 preset → seed agents
        preset = self.presets.get(request.preset_id)
        if preset:
            seed = set(preset.selected_agent_ids)
            if preset.usage_policy:
                policy = preset.usage_policy
        elif request.preset_id:
            raise ValueError(
                f"未知 preset_id: {request.preset_id!r}。"
                f"可用: {sorted(self.presets.keys())}"
            )
        else:
            seed = set()

        if request.seed_stages:
            seed.update(s.get("agent_id", "") for s in request.seed_stages)
            seed.discard("")  # 过滤空 agent_id
            # 白名单校验：拒绝不在 agent_io_map 中的 agent_id
            unknown = seed - set(self.agent_io_map.keys())
            if unknown:
                raise ValueError(f"seed_stages 含未知 agent_id: {sorted(unknown)}")

        pool = set(seed)

        # 2. 强制 core pipeline
        if policy.force_core_pipeline and self.core_pipeline_ids:
            pool.update(self.core_pipeline_ids)

        # 3. 自动闭包（从 artifact 依赖推导）
        if policy.auto_closure:
            pool = self.dag.closure(
                pool,
                required_agent_ids=self.required_agent_ids,
            )

        # 4. 构建 stages
        stages_raw = self.dag.compute_dependencies(pool)
        stages = [
            Stage(
                stage_id=s["agent_id"],
                agent_id=s["agent_id"],
                output_artifact_kind=s.get("output_artifact", s["agent_id"]),
                depends_on=s["depends_on"],
                input_refs=[f"job:{{job_id}}:artifact:{k}" for k in s.get("input_artifacts", [])],
                model_preference=self._model_pref(s["agent_id"]),
                context_limit_tokens=self._context_limit(s["agent_id"]),
                priority=self._priority(s["agent_id"]),
                **self._sandbox_fields(s["agent_id"]),
            )
            for s in stages_raw
        ]
        stages = self._inject_evaluators(stages)

        # 5. 分 wave（改用 dict 查找优化）
        waves = self.dag.partition_waves(stages_raw)
        stage_by_id = {s.stage_id: s for s in stages}
        for i, wave in enumerate(waves):
            for raw in wave:
                stage = stage_by_id.get(raw["stage_id"])
                if stage:
                    stage.parallel_group = f"wave_{i}"

        terminal = []
        if preset:
            terminal = list(preset.terminal_artifacts)

        plan = Plan(
            plan_type="pending_review" if review_mode else "initial",
            preset_id=request.preset_id,
            deliverable_type=request.deliverable_type,
            stages=stages,
            terminal_artifacts=terminal,
            summary=f"{len(stages)} stages in {len(waves)} waves",
            intent_summary=request.intent_summary,
            usage_policy=policy,
        )

        if self.capability_matrix:
            for stage in stages:
                row = self.capability_matrix.get(stage.agent_id)
                if row and row.context_limit_tokens:
                    input_count = len(stage.input_refs)
                    if input_count > 10:
                        warnings.append(
                            f"Agent {stage.agent_id} 的 context_limit={row.context_limit_tokens}, "
                            f"但有 {input_count} 个输入 artifact。建议添加摘要步骤。"
                        )

        return CompileResult(
            plan=plan,
            selected_agents=sorted(seed & pool),
            auto_added_agents=sorted(pool - seed),
            warnings=warnings,
        )

    def preview_closure(self, agent_ids: list[str]) -> set[str]:
        """预览自动闭包结果（平台 UI 用）。"""
        return self.dag.closure(set(agent_ids))

    def export_preset(self, preset_id: str) -> dict[str, Any] | None:
        preset = self.presets.get(preset_id)
        if preset is None:
            return None
        return {
            "format": "casa_preset_v1",
            "preset": preset.to_dict(),
            "agent_io_map": dict(self.agent_io_map),
        }

    def import_preset(self, data: dict[str, Any]) -> bool:
        if data.get("format") != "casa_preset_v1":
            return False
        preset = Preset.from_dict(data["preset"])
        self.presets[preset.preset_id] = preset
        return True


# ============================================================================
# 运行时 normalize 护栏（Normalizer）
# ============================================================================


class PlanNormalizer:
    """
    Plan Normalizer：校验并修复 plan，确保 core pipeline 完整且无禁用的 stage。

    LLM 编排输出必须过 normalize 兜底。
    """

    def __init__(
        self,
        *,
        core_pipeline_ids: set[str] | None = None,
        enabled_agent_ids: set[str] | None = None,
        deliverable_rules: dict[str, dict[str, Any]] | None = None,
        agent_io_map: dict[str, tuple[list[str], str]] | None = None,
    ):
        """
        参数:
            core_pipeline_ids: 必须存在的 agent
            enabled_agent_ids: 当前启用的 agent 白名单
            deliverable_rules: 每种交付物类型的硬规则
                例: {"insights": {"allowed_types": ["analyzer", "synthesizer"],
                                   "forbidden_types": ["report_module"]}}
        """
        self.core_pipeline_ids = core_pipeline_ids or set()
        self.enabled_agent_ids = enabled_agent_ids or set()
        self.deliverable_rules = deliverable_rules or {}
        self.agent_io_map = agent_io_map or {}

    def _stage_from_agent_io(self, plan: Plan, agent_id: str) -> Stage:
        inputs, output = self.agent_io_map.get(agent_id, ([], agent_id))
        producer_by_kind = {
            (s.output_artifact_kind or s.agent_id): s.stage_id for s in plan.stages
        }
        depends_on = sorted({
            producer_by_kind[k] for k in inputs if k in producer_by_kind
        })
        input_refs = [
            f"job:{{job_id}}:artifact:{k}" for k in inputs if k in producer_by_kind
        ]
        return Stage(
            stage_id=agent_id,
            agent_id=agent_id,
            output_artifact_kind=output or agent_id,
            depends_on=depends_on,
            input_refs=input_refs,
        )

    def normalize(self, plan: Plan, *, deliverable_type: str = "") -> Plan:
        """
        校验并修复 plan。

        返回:
            修复后的 Plan（不变或补全）
        """
        if not plan.stages:
            logger.warning("Plan has no stages, cannot normalize")
            return plan

        # 1. 剔除禁用 agent
        plan.stages = [
            s for s in plan.stages
            if not self.enabled_agent_ids or s.agent_id in self.enabled_agent_ids
        ]

        # 2. 确保 core pipeline
        existing_ids = {s.agent_id for s in plan.stages}
        for cid in self.core_pipeline_ids:
            if cid not in existing_ids and (not self.enabled_agent_ids or cid in self.enabled_agent_ids):
                plan.stages.insert(0, self._stage_from_agent_io(plan, cid))
                logger.info("Normalizer: added missing core agent %s", cid)

        # 3. 交付物规则
        if deliverable_type and deliverable_type in self.deliverable_rules:
            rules = self.deliverable_rules[deliverable_type]
            forbidden = set(rules.get("forbidden_types", []))
            plan.stages = [s for s in plan.stages if s.agent_id not in forbidden]

        # 4. 修复 depends_on 引用（跨 stage 一致性）
        stage_ids = {s.stage_id for s in plan.stages}
        producer_by_kind = {
            (s.output_artifact_kind or s.agent_id): s.stage_id for s in plan.stages
        }
        for s in plan.stages:
            s.depends_on = [d for d in s.depends_on if d in stage_ids]
            if self.agent_io_map and s.agent_id in self.agent_io_map:
                inputs, _ = self.agent_io_map[s.agent_id]
                required = sorted({
                    producer_by_kind[k] for k in inputs if k in producer_by_kind
                })
                s.depends_on = sorted(set(s.depends_on) | set(required))
                s.input_refs = [
                    f"job:{{job_id}}:artifact:{k}" for k in inputs if k in producer_by_kind
                ]

        # 基于健康分自动禁用 Agent
        for stage in list(plan.stages):
            health = self.agent_health_check(stage.agent_id)
            if not health["healthy"]:
                logger.warning(
                    "Agent %s 健康分异常（error_rate=%.2f），自动禁用",
                    stage.agent_id, health["error_rate"],
                )
                plan.stages.remove(stage)

        # 5. 再次修复 depends_on（健康禁用可能移除被依赖的 stage）
        stage_ids = {s.stage_id for s in plan.stages}
        producer_by_kind = {
            (s.output_artifact_kind or s.agent_id): s.stage_id for s in plan.stages
        }
        for s in plan.stages:
            s.depends_on = [d for d in s.depends_on if d in stage_ids]
            if self.agent_io_map and s.agent_id in self.agent_io_map:
                inputs, _ = self.agent_io_map[s.agent_id]
                required = sorted({
                    producer_by_kind[k] for k in inputs if k in producer_by_kind
                })
                s.depends_on = sorted(set(s.depends_on) | set(required))
                s.input_refs = [
                    f"job:{{job_id}}:artifact:{k}" for k in inputs if k in producer_by_kind
                ]

        return plan

    def agent_health_check(self, agent_id: str, *, window_minutes: int = 5) -> dict[str, Any]:
        import time
        from ..observability import get_metrics_sink

        metrics = get_metrics_sink().snapshot()
        now = time.time()
        failures = 0
        total = 0
        for rec in metrics:
            if rec.get("tags", {}).get("agent_id") != agent_id:
                continue
            ts = rec.get("timestamp", 0)
            if ts and (now - ts) > window_minutes * 60:
                continue
            if rec.get("name") == "stage.failure":
                failures += 1
            if rec.get("name") in ("stage.duration_ms", "stage.failure"):
                total += 1
        error_rate = failures / max(total, 1)
        healthy = error_rate < 0.5
        return {
            "agent_id": agent_id,
            "healthy": healthy,
            "error_rate": round(error_rate, 2),
            "total_attempts": total,
            "total_failures": failures,
            "recommendation": "enable" if healthy else "disable",
        }


