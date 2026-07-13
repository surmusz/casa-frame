"""
CASA Agent Loop — plan→execute→verify→iterate 循环引擎。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable


class LoopPhase(str, Enum):
    PLAN = "plan"
    REVIEW = "review"
    EXECUTE = "execute"
    VERIFY = "verify"
    REVERIFY = "reverify"
    ITERATE = "iterate"
    DONE = "done"


@dataclass(frozen=True)
class VerifierContext:
    """传给 verifier 的只读快照——禁止 verifier 通过副作用控制 loop。"""

    loop_iteration: int
    max_iterations: int
    intent: str
    deliverable_type: str
    prior_success: bool
    prior_artifact_kinds: tuple[str, ...] = ()
    extra_checks: tuple[str, ...] = ()


@dataclass
class _LoopState:
    """Loop 内部可变状态——不暴露给 verifier。"""

    revised_intent: str = ""
    issues: list[str] = field(default_factory=list)
    prior_success: bool = False
    prior_artifact_kinds: list[str] = field(default_factory=list)


@dataclass
class LoopIteration:
    iteration_num: int
    plan_id: str = ""
    phase: LoopPhase = LoopPhase.PLAN
    intent: str = ""
    selected_agents: list[str] = field(default_factory=list)
    plan_summary: str = ""
    stages_total: int = 0
    stages_success: int = 0
    stages_failed: int = 0
    verification_passed: bool = False
    verification_issues: list[str] = field(default_factory=list)
    reverification_passed: bool = False
    reverification_issues: list[str] = field(default_factory=list)
    quality_scores: list[float] = field(default_factory=list)
    tokens_used: int = 0
    used_replan: bool = False
    started_at: str = ""
    finished_at: str = ""


@dataclass
class LoopResult:
    total_iterations: int
    final_plan_id: str = ""
    success: bool = False
    stop_reason: str = ""
    iterations: list[LoopIteration] = field(default_factory=list)
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    final_compile_result: Any | None = None


class AgentLoop:
    """管理 plan→execute→verify→iterate 循环。"""

    def __init__(
        self,
        *,
        orchestrator: Any,
        verifier: Callable[..., Awaitable[list[str]]] | None = None,
        review_gate: Callable[[Any], Awaitable[bool]] | None = None,
        replan_agent_resolver: Callable[[Any, list[str], VerifierContext], list[str]] | None = None,
        max_iterations: int = 5,
        require_double_pass: bool = True,
        loop_timeout_seconds: float = 3600,
    ):
        self._orch = orchestrator
        self._verifier = verifier or self._default_verifier
        self._review_gate = review_gate
        self._replan_agent_resolver = replan_agent_resolver or self._default_replan_agents
        self._max_iterations = max_iterations
        self._require_double_pass = require_double_pass
        self._loop_timeout = loop_timeout_seconds

    async def run(
        self,
        intent: str,
        *,
        deliverable_type: str = "full",
        router: Any | None = None,
        **run_kwargs: Any,
    ) -> LoopResult:
        from .interrupt import InterruptSignal

        iteration = 0
        iterations: list[LoopIteration] = []
        state = _LoopState()
        last_result: Any | None = None
        pending_replan_agents: list[str] | None = None

        import time
        deadline = time.monotonic() + self._loop_timeout
        while iteration < self._max_iterations:
            if time.monotonic() > deadline:
                return LoopResult(
                    total_iterations=iteration,
                    success=False,
                    stop_reason="timeout",
                    iterations=iterations,
                    summary=f"Loop 超时（>{self._loop_timeout}s）",
                )
            iteration += 1
            now = datetime.now(timezone.utc).isoformat()
            record = LoopIteration(iteration_num=iteration, started_at=now)

            if self._orch._interrupt_ctrl:
                ctrl_state = self._orch._interrupt_ctrl.check()
                if ctrl_state.signal in (
                    InterruptSignal.ABORT_GRACEFUL,
                    InterruptSignal.ABORT_IMMEDIATE,
                ):
                    return LoopResult(
                        total_iterations=iteration,
                        success=False,
                        stop_reason="aborted",
                        iterations=iterations,
                        summary=f"Loop 在第 {iteration} 轮被中断: {ctrl_state.reason}",
                    )

            record.phase = LoopPhase.PLAN
            record.intent = state.revised_intent or intent
            if state.issues:
                record.intent = (
                    f"{record.intent}\n[上次发现的问题需要修正: "
                    + "; ".join(state.issues)
                    + "]"
                )

            if router:
                compile_request = await self._plan_with_router(
                    router, record.intent, deliverable_type,
                )
            else:
                compile_request = self._compile_request_from_intent(
                    record.intent, deliverable_type,
                )

            record.phase = LoopPhase.REVIEW
            if self._review_gate:
                preview = self._orch.compiler.compile(compile_request, review_mode=True)
                if self._orch.normalizer:
                    preview.plan = self._orch.normalizer.normalize(
                        preview.plan, deliverable_type=deliverable_type,
                    )
                approved = await self._review_gate(preview)
                if not approved:
                    state.issues = ["Plan 未通过审查"]
                    state.revised_intent = (
                        f"{record.intent} [审查未通过，需要重新设计方案]"
                    )
                    record.finished_at = datetime.now(timezone.utc).isoformat()
                    iterations.append(record)
                    continue

            record.phase = LoopPhase.EXECUTE
            use_replan = self._should_replan(iteration, state)
            record.used_replan = use_replan
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return LoopResult(
                    total_iterations=iteration,
                    success=False,
                    stop_reason="timeout",
                    iterations=iterations,
                    summary=f"Loop 超时（>{self._loop_timeout}s）",
                )
            try:
                if use_replan:
                    extra_agents = pending_replan_agents or []
                    exec_result = await asyncio.wait_for(
                        self._orch.replan(
                            intent=record.intent,
                            deliverable_type=deliverable_type,
                            additional_agents=extra_agents or None,
                            router=router,
                            **{k: v for k, v in run_kwargs.items() if k != "job_id"},
                        ),
                        timeout=remaining,
                    )
                else:
                    exec_result = await asyncio.wait_for(
                        self._orch.run(
                            compile_request,
                            job_id=f"{run_kwargs.get('job_id', '')}_iter{iteration}".strip("_"),
                            **{k: v for k, v in run_kwargs.items() if k != "job_id"},
                        ),
                        timeout=remaining,
                    )
            except asyncio.TimeoutError:
                return LoopResult(
                    total_iterations=iteration,
                    success=False,
                    stop_reason="timeout",
                    iterations=iterations,
                    summary=f"Loop 超时（>{self._loop_timeout}s）",
                )
            last_result = exec_result
            pending_replan_agents = None

            record.plan_id = exec_result.plan.plan_id
            record.selected_agents = list(exec_result.selected_agents)
            record.plan_summary = exec_result.plan.summary
            record.stages_total = len(exec_result.plan.stages)
            record.stages_success = sum(
                1 for r in exec_result.stage_results.values() if r.success
            )
            record.stages_failed = record.stages_total - record.stages_success
            record.quality_scores = [
                r.quality_score for r in exec_result.stage_results.values()
            ]

            if record.stages_success > 0:
                state.prior_success = True
                state.prior_artifact_kinds = list(
                    self._orch.executor.store.list_artifacts(),
                )

            verifier_ctx = VerifierContext(
                loop_iteration=iteration,
                max_iterations=self._max_iterations,
                intent=record.intent,
                deliverable_type=deliverable_type,
                prior_success=state.prior_success,
                prior_artifact_kinds=tuple(state.prior_artifact_kinds),
            )

            record.phase = LoopPhase.VERIFY
            issues = await self._verifier(exec_result, verifier_ctx)
            record.verification_issues = issues
            if issues:
                record.verification_passed = False
                record.phase = LoopPhase.ITERATE
                state.issues = issues
                state.revised_intent = self._build_revision_intent(
                    record.intent, issues, exec_result,
                )
                if state.prior_success:
                    pending_replan_agents = self._replan_agent_resolver(
                        exec_result, issues, verifier_ctx,
                    )
                record.finished_at = datetime.now(timezone.utc).isoformat()
                iterations.append(record)
                continue
            record.verification_passed = True

            if self._require_double_pass:
                record.phase = LoopPhase.REVERIFY
                reverify_ctx = VerifierContext(
                    loop_iteration=iteration,
                    max_iterations=self._max_iterations,
                    intent=record.intent,
                    deliverable_type=deliverable_type,
                    prior_success=state.prior_success,
                    prior_artifact_kinds=tuple(state.prior_artifact_kinds),
                    extra_checks=("loop_iteration_check",),
                )
                reverify_issues = list(await self._verifier(exec_result, reverify_ctx))
                record.reverification_issues = reverify_issues
                if reverify_issues:
                    record.reverification_passed = False
                    record.phase = LoopPhase.ITERATE
                    state.issues = reverify_issues
                    state.revised_intent = self._build_revision_intent(
                        record.intent, reverify_issues, exec_result,
                    )
                    if state.prior_success:
                        pending_replan_agents = self._replan_agent_resolver(
                            exec_result, reverify_issues, reverify_ctx,
                        )
                    record.finished_at = datetime.now(timezone.utc).isoformat()
                    iterations.append(record)
                    continue
                record.reverification_passed = True

            record.phase = LoopPhase.DONE
            record.finished_at = datetime.now(timezone.utc).isoformat()
            iterations.append(record)
            return LoopResult(
                total_iterations=iteration,
                final_plan_id=record.plan_id,
                success=True,
                stop_reason="double_pass_verified" if self._require_double_pass else "single_pass_verified",
                iterations=iterations,
                summary=(
                    f"经过 {iteration} 轮迭代完成。"
                    f"最终 plan 含 {record.stages_total} 个 stage，"
                    f"成功率 {record.stages_success}/{record.stages_total}。"
                ),
                final_compile_result=last_result,
            )

        return LoopResult(
            total_iterations=iteration,
            success=False,
            stop_reason="max_iterations",
            iterations=iterations,
            summary=f"经过 {self._max_iterations} 轮迭代仍未满足完成条件。",
            warnings=[f"达到最大循环轮次 {self._max_iterations}"],
            final_compile_result=(
                last_result if self._usable_compile_result(last_result) else None
            ),
        )

    @staticmethod
    def _usable_compile_result(result: Any) -> bool:
        """仅当所有 stage 均成功时才保留 final_compile_result。"""
        if result is None:
            return False
        stage_results = getattr(result, "stage_results", {}) or {}
        if not stage_results:
            return False
        successes = [r.success for r in stage_results.values()]
        return all(successes) and any(successes)

    @staticmethod
    def _should_replan(iteration: int, state: _LoopState) -> bool:
        """上一轮有成功 stage + 已有 artifact 时，下一轮走 replan 增量路径。"""
        return iteration > 1 and state.prior_success and bool(state.prior_artifact_kinds)

    def _default_replan_agents(
        self,
        result: Any,
        issues: list[str],
        ctx: VerifierContext,
    ) -> list[str]:
        """从 issues/intent 中推断需追加的 agent_id（排除已完成）。"""
        known = set(self._orch.compiler.agent_io_map.keys())
        completed = set(getattr(self._orch, "_completed_agent_ids", set()))
        text = " ".join(issues) + " " + ctx.intent
        found = [aid for aid in sorted(known) if aid in text and aid not in completed]
        return found

    async def _plan_with_router(
        self, router: Any, intent: str, deliverable_type: str,
    ) -> Any:
        from .orchestration import CompileRequest, UsagePolicy

        route_result = await router.route(intent, deliverable_type=deliverable_type)
        policy_cls = {
            "for_user_start": UsagePolicy.for_user_start,
            "for_patch": UsagePolicy.for_patch,
            "for_preview": UsagePolicy.for_preview,
        }
        policy_factory = policy_cls.get(route_result.policy, UsagePolicy.for_user_start)
        return CompileRequest(
            preset_id="",
            seed_stages=[{"agent_id": aid} for aid in route_result.agent_ids],
            deliverable_type=deliverable_type,
            policy=policy_factory(),
            intent_summary=intent,
        )

    @staticmethod
    def _compile_request_from_intent(intent: str, deliverable_type: str) -> Any:
        from .orchestration import CompileRequest
        return CompileRequest(
            intent_summary=intent,
            deliverable_type=deliverable_type,
        )

    @staticmethod
    async def _default_verifier(
        result: Any,
        context: VerifierContext,
    ) -> list[str]:
        issues: list[str] = []
        feedback = result.review_feedback() if hasattr(result, "review_feedback") else {}

        for stage in feedback.get("stages", []):
            if stage.get("low_quality"):
                issues.append(
                    f"Stage {stage['stage_id']} ({stage['agent_id']}) "
                    f"质量分 {stage.get('quality_score', '?')} 低于阈值",
                )
            if not stage.get("success"):
                issues.append(
                    f"Stage {stage['stage_id']} ({stage['agent_id']}) "
                    f"执行失败: {stage.get('error', 'unknown')}",
                )

        plan_stages = {
            s.stage_id: s for s in result.plan.stages
        } if getattr(result, "plan", None) else {}
        for sid, r in (getattr(result, "stage_results", {}) or {}).items():
            s = plan_stages.get(sid)
            if s and getattr(s, "stage_role", "") == "evaluator":
                if not r.success or r.quality_score < 0.7:
                    issues.append(
                        f"EvalStage {sid} 评估未通过: quality_score={r.quality_score}",
                    )

        if feedback.get("actionable") and not issues:
            issues.append("review_feedback 标记为 actionable")

        if "loop_iteration_check" in context.extra_checks:
            if context.loop_iteration >= context.max_iterations:
                issues.append(
                    f"已达到最大循环轮次 {context.max_iterations}",
                )

        return issues

    @staticmethod
    def _build_revision_intent(
        original_intent: str,
        issues: list[str],
        result: Any,
    ) -> str:
        issue_summary = "; ".join(issues[:5])
        return f"{original_intent}\n[修正要求: {issue_summary}]"
