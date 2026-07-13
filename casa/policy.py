"""
CASA Policy Engine — 声明式规则 + 统一执行入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RulePhase(str, Enum):
    PRE_COMPILE = "pre_compile"
    POST_COMPILE = "post_compile"
    PRE_EXECUTE = "pre_execute"
    POST_STAGE = "post_stage"
    POST_EXECUTE = "post_execute"


@dataclass(kw_only=True)
class PolicyRule:
    rule_id: str
    phase: RulePhase
    description: str = ""
    condition: str = ""
    action: str = "warn"
    action_message: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "phase": self.phase.value,
            "condition": self.condition,
            "action": self.action,
            "enabled": self.enabled,
        }


class PolicyEngine:
    """声明式规则引擎——在 pipeline 各阶段执行。"""

    def __init__(self) -> None:
        self._rules: dict[RulePhase, list[PolicyRule]] = {p: [] for p in RulePhase}

    def add(self, rule: PolicyRule) -> None:
        self._rules[rule.phase].append(rule)

    def evaluate(self, phase: RulePhase, **context: Any) -> list[dict[str, Any]]:
        triggered: list[dict[str, Any]] = []
        for rule in self._rules.get(phase, []):
            if not rule.enabled:
                continue
            if self._check(rule.condition, context):
                triggered.append({
                    "rule_id": rule.rule_id,
                    "action": rule.action,
                    "action_message": rule.action_message,
                    "phase": phase.value,
                })
        return triggered

    @staticmethod
    def _check(condition: str, context: dict[str, Any]) -> bool:
        if not condition:
            return False
        result = context.get("result")
        if "quality_score" in condition and result is not None:
            if hasattr(result, "quality_score") and "<" in condition:
                try:
                    threshold = float(condition.split("<")[-1].strip())
                    return result.quality_score < threshold
                except ValueError:
                    return False
            if hasattr(result, "quality_score") and result.quality_score < 0.5:
                return True
        if "tenant_quota_exceeded" in condition:
            return bool(context.get("quota_exceeded", False))
        if "stage_count >" in condition:
            try:
                limit = int(condition.split(">")[-1].strip())
                stages = context.get("stages", [])
                return len(stages) > limit
            except ValueError:
                return False
        if "approval_required" in condition and result is not None:
            return bool(getattr(result, "approval_required", False))
        return False
