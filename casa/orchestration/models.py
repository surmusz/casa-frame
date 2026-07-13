"""编排数据模型。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..artifact import ArtifactStore

@dataclass(kw_only=True)
class Stage:
    """一个执行阶段。"""

    stage_id: str
    agent_id: str
    output_artifact_kind: str = ""  # 该 stage 输出的 artifact kind
    depends_on: list[str] = field(default_factory=list)
    parallel_group: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    input_refs: list[str] = field(default_factory=list)
    injected_prompt: str = ""
    model_preference: str = ""
    context_limit_tokens: int = 128000
    pre_completed: bool = False
    stage_role: str = "producer"
    eval_targets: list[str] = field(default_factory=list)
    eval_criteria: list[str] = field(default_factory=list)
    sandbox_memory_mb: int = 512
    sandbox_network: str = "restricted"
    sandbox_filesystem: str = "read_only"
    priority: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "agent_id": self.agent_id,
            "output_artifact_kind": self.output_artifact_kind,
            "depends_on": list(self.depends_on),
            "parallel_group": self.parallel_group,
            "params": self.params,
            "input_refs": self.input_refs,
            "model_preference": self.model_preference,
            "context_limit_tokens": self.context_limit_tokens,
            "pre_completed": self.pre_completed,
            "stage_role": self.stage_role,
            "eval_targets": list(self.eval_targets),
            "eval_criteria": list(self.eval_criteria),
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stage:
        return cls(
            stage_id=data["stage_id"],
            agent_id=data["agent_id"],
            output_artifact_kind=data.get("output_artifact_kind", ""),
            depends_on=list(data.get("depends_on", [])),
            parallel_group=data.get("parallel_group"),
            params=dict(data.get("params", {})),
            input_refs=list(data.get("input_refs", [])),
            injected_prompt=data.get("injected_prompt", ""),
            model_preference=data.get("model_preference", ""),
            context_limit_tokens=int(data.get("context_limit_tokens", 128000)),
            pre_completed=bool(data.get("pre_completed", False)),
            stage_role=data.get("stage_role", "producer"),
            eval_targets=list(data.get("eval_targets", [])),
            eval_criteria=list(data.get("eval_criteria", [])),
            sandbox_memory_mb=int(data.get("sandbox_memory_mb", 512)),
            sandbox_network=data.get("sandbox_network", "restricted"),
            sandbox_filesystem=data.get("sandbox_filesystem", "read_only"),
            priority=int(data.get("priority", 100)),
        )


@dataclass(kw_only=True)
class Plan:
    """一次完整的执行计划。"""

    plan_id: str = field(default_factory=lambda: f"plan_{uuid.uuid4().hex}")
    plan_type: str = "initial"
    preset_id: str = ""
    deliverable_type: str = "full"
    stages: list[Stage] = field(default_factory=list)
    terminal_artifacts: list[str] = field(default_factory=list)
    summary: str = ""
    intent_summary: str = ""
    version: int = 1
    previous_plan_id: str = ""
    revision_history: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_type": self.plan_type,
            "preset_id": self.preset_id,
            "deliverable_type": self.deliverable_type,
            "stages": [s.to_dict() for s in self.stages],
            "terminal_artifacts": list(self.terminal_artifacts),
            "summary": self.summary,
            "version": self.version,
            "previous_plan_id": self.previous_plan_id,
            "usage_policy": self.usage_policy.to_dict() if self.usage_policy else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        policy_data = data.get("usage_policy")
        usage_policy = UsagePolicy.from_dict(policy_data) if policy_data else None
        return cls(
            plan_id=data.get("plan_id", f"plan_{uuid.uuid4().hex}"),
            plan_type=data.get("plan_type", "initial"),
            preset_id=data.get("preset_id", ""),
            deliverable_type=data.get("deliverable_type", "full"),
            stages=[Stage.from_dict(s) for s in data.get("stages", [])],
            terminal_artifacts=list(data.get("terminal_artifacts", [])),
            summary=data.get("summary", ""),
            intent_summary=data.get("intent_summary", ""),
            version=int(data.get("version", 1)),
            previous_plan_id=data.get("previous_plan_id", ""),
            revision_history=list(data.get("revision_history", [])),
            usage_policy=usage_policy,
        )

    def to_mermaid(self, *, direction: str = "TD") -> str:
        """将 plan stages 导出为 Mermaid 流程图。"""
        lines = [f"flowchart {direction}"]
        for stage in self.stages:
            lines.append(f'    {stage.stage_id}["{stage.agent_id}"]')
            for dep in stage.depends_on:
                lines.append(f"    {dep} --> {stage.stage_id}")
        return "\n".join(lines)


# ============================================================================
# 预设模板（Preset）
# ============================================================================


@dataclass(kw_only=True)
class Preset:
    """
    预设模板：一组 Agent 选择 + 终端产物 + 使用策略。

    preset 是"常见运行配置"的快照，避免用户每次手动选 Agent。
    """

    preset_id: str
    display_name: str = ""
    selected_agent_ids: list[str] = field(default_factory=list)
    terminal_artifacts: list[str] = field(default_factory=list)
    auto_closure: bool = True
    usage_policy: UsagePolicy | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "preset_id": self.preset_id,
            "display_name": self.display_name,
            "selected_agent_ids": list(self.selected_agent_ids),
            "terminal_artifacts": list(self.terminal_artifacts),
            "auto_closure": self.auto_closure,
        }
        if self.usage_policy is not None:
            data["usage_policy"] = self.usage_policy.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Preset:
        policy_data = data.get("usage_policy")
        usage_policy = UsagePolicy.from_dict(policy_data) if policy_data else None
        return cls(
            preset_id=data["preset_id"],
            display_name=data.get("display_name", ""),
            selected_agent_ids=list(data.get("selected_agent_ids", [])),
            terminal_artifacts=list(data.get("terminal_artifacts", [])),
            auto_closure=data.get("auto_closure", True),
            usage_policy=usage_policy,
        )


# ============================================================================
# 使用策略（UsagePolicy）
# ============================================================================


@dataclass(kw_only=True)
class UsagePolicy:
    """
    使用策略：控制 Plan Compiler 的行为。

    不同入口（用户启动 / 局部更新 / 平台预览）对应不同策略。
    """

    force_core_pipeline: bool = True
    allow_skip_core_if_artifacts_exist: bool = False
    auto_closure: bool = True
    required_artifacts: list[str] = field(default_factory=list)

    @classmethod
    def from_deliverable_type(cls, deliverable_type: str, **kwargs: Any) -> UsagePolicy:
        from ..deliverable import get_deliverable_registry
        required = list(get_deliverable_registry().required_artifacts(deliverable_type))
        return cls(required_artifacts=required, **kwargs)

    @classmethod
    def for_user_start(cls) -> UsagePolicy:
        return cls(force_core_pipeline=True)

    @classmethod
    def for_patch(cls) -> UsagePolicy:
        return cls(force_core_pipeline=False, allow_skip_core_if_artifacts_exist=True)

    @classmethod
    def for_preview(cls) -> UsagePolicy:
        return cls(force_core_pipeline=False, auto_closure=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "force_core_pipeline": self.force_core_pipeline,
            "allow_skip_core_if_artifacts_exist": self.allow_skip_core_if_artifacts_exist,
            "auto_closure": self.auto_closure,
            "required_artifacts": list(self.required_artifacts),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsagePolicy:
        return cls(
            force_core_pipeline=data.get("force_core_pipeline", True),
            allow_skip_core_if_artifacts_exist=data.get(
                "allow_skip_core_if_artifacts_exist", False,
            ),
            auto_closure=data.get("auto_closure", True),
            required_artifacts=list(data.get("required_artifacts", [])),
        )


# ============================================================================
# 编译时（PlanCompiler）
# ============================================================================


@dataclass(kw_only=True)
class CompileRequest:
    """Plan Compiler 的输入。"""

    preset_id: str = ""
    contract: Any | None = None  # casa.contract.Contract
    artifact_store: ArtifactStore | None = None
    deliverable_type: str = "full"
    seed_stages: list[dict[str, Any]] | None = None
    policy: UsagePolicy = field(default_factory=UsagePolicy)

    # 额外上下文
    intent_summary: str = ""
    review_mode: bool = False

@dataclass(kw_only=True)
class CompileResult:
    """Plan Compiler 的输出。"""

    plan: Plan
    selected_agents: list[str] = field(default_factory=list)
    auto_added_agents: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    graph: dict[str, Any] = field(default_factory=dict)
    stage_results: dict[str, Any] = field(default_factory=dict)
    deliverable_output: Any | None = None

    def summary(self) -> dict[str, Any]:
        stage_results = self.stage_results or {}
        total = len(stage_results)
        success = sum(1 for r in stage_results.values() if r.success)
        skipped = sum(1 for r in stage_results.values() if getattr(r, "skipped", False))
        failed = total - success
        stage_count = len(self.plan.stages) if self.plan else total
        return {
            "plan_id": self.plan.plan_id if self.plan else "",
            "plan_type": getattr(self.plan, "plan_type", ""),
            "deliverable_type": getattr(self.plan, "deliverable_type", ""),
            "stages_total": stage_count,
            "stages_success": success,
            "stages_skipped": skipped,
            "stages_failed": failed,
            "quality_score_avg": round(
                sum(r.quality_score for r in stage_results.values()) / max(total, 1), 2,
            ),
            "warnings": list(self.warnings),
        }

    def review_feedback(self) -> dict[str, Any]:
        stages: list[dict[str, Any]] = []
        warnings = list(self.warnings)
        actionable = False
        deliverable_path = ""
        if isinstance(self.deliverable_output, dict):
            deliverable_path = str(self.deliverable_output.get("path", ""))
        elif self.deliverable_output is not None:
            deliverable_path = str(getattr(self.deliverable_output, "path", ""))

        for sid, r in (self.stage_results or {}).items():
            info: dict[str, Any] = {
                "stage_id": sid,
                "agent_id": r.agent_id,
                "success": r.success,
                "skipped": r.skipped,
                "quality_score": r.quality_score,
                "approval_required": getattr(r, "approval_required", False),
            }
            if r.error:
                info["error"] = r.error
                actionable = True
            if r.quality_score < 0.7:
                info["low_quality"] = True
                actionable = True
            stages.append(info)

        return {
            "summary": self.plan.summary if self.plan else "",
            "stages": stages,
            "deliverable_path": deliverable_path,
            "actionable": actionable or bool(warnings),
            "warnings": warnings,
        }

    def trajectory_summary(self) -> dict[str, Any]:
        """轨迹级评估摘要。"""
        stage_results = self.stage_results or {}
        plan_stages = {s.stage_id: s for s in self.plan.stages} if self.plan else {}

        producer_stages: list[dict[str, Any]] = []
        evaluator_stages: list[dict[str, Any]] = []
        skipped_stages: list[dict[str, Any]] = []
        failed_stages: list[dict[str, Any]] = []

        for sid, r in stage_results.items():
            s = plan_stages.get(sid)
            role = getattr(s, "stage_role", "producer") if s else "producer"
            info = {
                "stage_id": sid,
                "agent_id": r.agent_id,
                "role": role,
                "success": r.success,
                "quality_score": r.quality_score,
                "skipped": r.skipped,
                "depends_on": s.depends_on if s else [],
            }
            if role == "evaluator":
                evaluator_stages.append(info)
            elif r.skipped:
                skipped_stages.append(info)
            elif not r.success:
                failed_stages.append(info)
            else:
                producer_stages.append(info)

        return {
            "plan_id": self.plan.plan_id if self.plan else "",
            "total_stages": len(stage_results),
            "producer_count": len(producer_stages),
            "evaluator_count": len(evaluator_stages),
            "skipped_count": len(skipped_stages),
            "failed_count": len(failed_stages),
            "efficiency_ratio": round(
                len(producer_stages) / max(len(stage_results), 1), 2,
            ),
            "quality_scores": {
                "min": min((r.quality_score for r in stage_results.values()), default=1.0),
                "max": max((r.quality_score for r in stage_results.values()), default=1.0),
                "avg": round(
                    sum(r.quality_score for r in stage_results.values())
                    / max(len(stage_results), 1),
                    2,
                ),
            },
            "producer_stages": producer_stages,
            "evaluator_stages": evaluator_stages,
            "failed_stages": failed_stages,
        }

