"""Pipeline 组合器。"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from ..hooks import PipelineHook
from .models import CompileRequest, CompileResult

@dataclass(kw_only=True)
class PipelineStep:
    """组合 pipeline 中的一个步骤。"""

    plan_request: CompileRequest
    condition: str = ""
    fallback_request: CompileRequest | None = None
    label: str = ""
    on_skip: str = "continue"  # continue | abort


from .facade import Orchestrator


class PipelineComposer:
    """
    组合多个 plan，支持可选条件分支。

    条件根据先前 stage_results 的 quality_score 或 artifact 字段求值。
    """

    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator

    async def run_sequence(self, steps: list[PipelineStep], **run_kwargs: Any) -> list[CompileResult]:
        results: list[CompileResult] = []
        for i, step in enumerate(steps):
            if step.condition and i > 0 and results:
                if not self._should_branch(step.condition, results[-1]):
                    if step.on_skip == "abort":
                        break
                    continue
            result = await self.orchestrator.run(step.plan_request, **run_kwargs)
            results.append(result)
            if step.condition and step.fallback_request and self._should_branch(step.condition, result):
                fallback = await self.orchestrator.run(step.fallback_request, **run_kwargs)
                results.append(fallback)
        return results

    @staticmethod
    def _should_branch(condition: str, result: CompileResult) -> bool:
        cond = condition.strip()
        if not cond:
            return False
        scores = [r.quality_score for r in result.stage_results.values()]
        avg = sum(scores) / len(scores) if scores else 1.0
        stage_count = len(result.plan.stages)

        parts = [p.strip() for p in cond.replace(" and ", " AND ").split(" AND ")]
        checks: list[bool] = []
        for part in parts:
            lower = part.lower()
            if "quality_score" in lower and "<" in part:
                try:
                    threshold = float(part.split("<")[-1].strip())
                    checks.append(avg < threshold)
                except ValueError:
                    checks.append(False)
            elif "stage_count" in lower and ">" in part:
                try:
                    threshold = float(part.split(">")[-1].strip())
                    checks.append(stage_count > threshold)
                except ValueError:
                    checks.append(False)
            else:
                checks.append(False)
        return all(checks) if checks else False


class ResourceHook(PipelineHook):
    """在每个 wave 前预热 Agent 资源。"""

    def __init__(self, executor: Any):
        self._executor = executor

    async def on_wave_start(self, wave_stages: list[Any]) -> None:
        warmup = getattr(self._executor, "warmup", None)
        if callable(warmup):
            agent_ids = [s.agent_id for s in wave_stages]
            result = warmup(agent_ids)
            if inspect.isawaitable(result):
                await result
