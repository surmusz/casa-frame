"""质量门与策略 Hook。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..hooks import PipelineHook

@dataclass(kw_only=True)
class QualityGateRule:
    """一条质量门规则。"""

    rule_id: str
    description: str = ""
    condition: str = ""
    action: str = "warn"
    action_params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    stage_filter: str = "*"


class QualityGate:
    """声明式质量门——在 Hook 中执行规则。"""

    def __init__(self, rules: list[QualityGateRule] | None = None):
        self._rules = rules or []

    def add_rule(self, rule: QualityGateRule) -> None:
        self._rules.append(rule)

    def evaluate(self, stage: Any, result: Any) -> list[dict[str, Any]]:
        triggered: list[dict[str, Any]] = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            if rule.stage_filter != "*":
                import fnmatch
                if not fnmatch.fnmatch(stage.stage_id, rule.stage_filter) and \
                   not fnmatch.fnmatch(stage.agent_id, rule.stage_filter):
                    continue
            if self._matches(rule.condition, stage, result):
                triggered.append({
                    "rule_id": rule.rule_id,
                    "action": rule.action,
                    "action_params": dict(rule.action_params),
                    "description": rule.description,
                    "stage_id": stage.stage_id,
                })
        return triggered

    @staticmethod
    def _matches(condition: str, stage: Any, result: Any) -> bool:
        if not condition:
            return False
        cond = condition.strip()
        if "quality_score" in cond and "<" in cond:
            try:
                threshold = float(cond.split("<")[-1].strip())
                return result.quality_score < threshold
            except ValueError:
                return False
        if "approval_required" in cond:
            return result.approval_required
        if "eval_score" in cond and "<" in cond:
            try:
                threshold = float(cond.split("<")[-1].strip())
                score = getattr(result, "eval_score", result.quality_score)
                return score < threshold
            except ValueError:
                return False
        if "skipped" in cond and "false" in cond.lower():
            return not result.skipped
        return False


class QualityGateHook(PipelineHook):
    """质量门 Hook——stage 结束时自动评估规则。"""

    def __init__(
        self,
        gate: QualityGate,
        interrupt_ctrl: Any | None = None,
        orchestrator: Any | None = None,
    ):
        self._gate = gate
        self._ctrl = interrupt_ctrl
        self._orch = orchestrator

    async def on_stage_end(self, stage: Any, result: Any) -> None:
        for item in self._gate.evaluate(stage, result):
            if item["action"] == "pause" and self._ctrl:
                self._ctrl.pause(
                    item.get("description") or f"QualityGate {item['rule_id']}",
                    after="stage",
                )
            elif item["action"] == "abort" and self._ctrl:
                self._ctrl.abort(item.get("description", ""), graceful=True)
            elif item["action"] == "warn" and self._orch is not None:
                pass  # 由调用方收集 warnings


class PolicyEnforcementHook(PipelineHook):
    """PolicyEngine 规则执行 Hook。"""

    def __init__(self, engine: Any, interrupt_ctrl: Any | None = None):
        from ..policy import RulePhase
        self._engine = engine
        self._ctrl = interrupt_ctrl
        self._post_stage = RulePhase.POST_STAGE

    async def on_stage_end(self, stage: Any, result: Any) -> None:
        triggered = self._engine.evaluate(self._post_stage, stage=stage, result=result)
        for item in triggered:
            if item["action"] == "pause" and self._ctrl:
                self._ctrl.pause(item.get("action_message", ""), after="stage")

