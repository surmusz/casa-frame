"""Plan 波次并行执行。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ..config import get_config
from ..hooks import HookRegistry
from ..observability import get_metrics_sink, log_event
from .execute import StageExecutionError, StageResult, StageRunner
from .models import Plan

logger = logging.getLogger("casa.orchestration")

class PlanExecutor:
    """
    Plan Executor：按 DAG 波次并行执行 stages。

    使用方式：
        executor = PlanExecutor(
            store=artifact_store,
            stage_runner=StageRunner(...),
        )
        results = await executor.execute(plan)
    """

    def __init__(
        self,
        *,
        store: ArtifactStore,
        stage_runner: StageRunner,
        max_parallel_per_wave: int = 0,
        ws_sender: Callable[[dict], Awaitable[None]] | None = None,
        hooks: HookRegistry | None = None,
        interrupt_ctrl: Any | None = None,
        metrics_sink: Any | None = None,
        deadlock_recovery: Callable[..., Awaitable[Plan | None]] | None = None,
    ):
        self.store = store
        self.stage_runner = stage_runner
        self._max_parallel = max_parallel_per_wave
        self._metrics_sink = metrics_sink or get_metrics_sink()
        self._semaphore = asyncio.Semaphore(max_parallel_per_wave) if max_parallel_per_wave > 0 else None
        self._ws_send = ws_sender
        self._hooks = hooks or getattr(stage_runner, "_hooks", None) or HookRegistry()
        self._interrupt_ctrl = interrupt_ctrl
        self._deadlock_recovery = deadlock_recovery
        if interrupt_ctrl is not None:
            stage_runner._interrupt_ctrl = interrupt_ctrl

    async def execute(self, plan: Plan) -> dict[str, StageResult]:
        """
        执行 plan 中的所有 stage。按 DAG 拓扑波次并行。

        返回:
            {stage_id: StageResult}

        抛出:
            StageExecutionError: 任何 stage 失败（容错耗尽后）
        """
        stages = plan.stages
        stage_map = {s.stage_id: s for s in stages}
        pending = {s.stage_id for s in stages}
        completed: set[str] = set()
        results: dict[str, StageResult] = {}
        self._partial_results: dict[str, StageResult] = {}

        while pending:
            ctrl = self._interrupt_ctrl
            if ctrl is not None:
                from ..interrupt import InterruptSignal
                state = ctrl.check()
                if state.signal == InterruptSignal.ABORT_IMMEDIATE:
                    raise StageExecutionError(
                        "plan", "interrupt",
                        f"执行被中断: {state.reason}",
                        partial_results=dict(results),
                    )
                if ctrl.is_paused:
                    log_event("plan.paused", reason=state.reason)
                    await ctrl.wait_if_paused()
                    log_event("plan.resumed")
                    state = ctrl.check()
                    if state.signal == InterruptSignal.ABORT_IMMEDIATE:
                        raise StageExecutionError(
                            "plan", "interrupt",
                            f"执行被中断: {state.reason}",
                            partial_results=dict(results),
                        )

            ready_stages = sorted(
                [
                    stage_map[sid]
                    for sid in pending
                    if all(d in completed for d in stage_map[sid].depends_on)
                ],
                key=lambda s: s.priority,
            )

            if not ready_stages:
                stuck = [stage_map[sid].agent_id for sid in pending]
                config = get_config()
                if config.auto_replan_on_deadlock and self._deadlock_recovery:
                    new_plan = await self._deadlock_recovery(
                        plan, set(pending), completed, results,
                    )
                    if new_plan:
                        plan = new_plan
                        stage_map = {s.stage_id: s for s in plan.stages}
                        pending = {
                            s.stage_id for s in plan.stages if s.stage_id not in completed
                        }
                        log_event(
                            "plan.deadlock_replan_applied",
                            pending_count=len(pending),
                            stuck_agents=stuck[:5],
                        )
                        continue
                if config.auto_skip_deadlocked_stages:
                    logger.warning("Plan deadlock auto-skip: %s", stuck)
                    for sid in list(pending):
                        results[sid] = StageResult(
                            stage_id=sid,
                            agent_id=stage_map[sid].agent_id,
                            success=False,
                            error="deadlock_skip",
                            skipped=True,
                        )
                        completed.add(sid)
                    pending.clear()
                    break
                logger.error("Plan deadlock: %s", stuck)
                raise StageExecutionError(
                    "plan", "orchestrator",
                    f"Plan deadlock: {', '.join(stuck[:5])}",
                )

            log_event(
                "plan.wave_start",
                wave_size=len(ready_stages),
                stage_ids=[s.stage_id for s in ready_stages],
            )
            await self._hooks.fire("wave_start", wave_stages=ready_stages)

            wave_limit = self._adaptive_max_parallel([s.agent_id for s in ready_stages])
            wave_sem = asyncio.Semaphore(wave_limit) if wave_limit > 0 else None

            async def _run_with_semaphore(s: Stage, p: Plan, c: set[str]) -> StageResult:
                sem = wave_sem or self._semaphore
                if sem:
                    await sem.acquire()
                    try:
                        return await self.stage_runner.run(s, p, c)
                    finally:
                        sem.release()
                return await self.stage_runner.run(s, p, c)

            # 并发执行 ready stages；遇异常或 stage 级 pause 时取消同波 peer
            task_objs: dict[asyncio.Task, str] = {}
            for s in ready_stages:
                task = asyncio.ensure_future(_run_with_semaphore(s, plan, completed))
                task_objs[task] = s.stage_id

            remaining = set(task_objs.keys())
            wave_failed_exc: BaseException | None = None
            wave_paused = False

            while remaining:
                done, _ = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    remaining.discard(task)
                    sid = task_objs[task]
                    exc = task.exception()
                    if exc:
                        wave_failed_exc = exc
                        for pt in list(remaining):
                            pt.cancel()
                        if remaining:
                            await asyncio.gather(*remaining, return_exceptions=True)
                        remaining.clear()
                        break
                    results[sid] = task.result()
                    self._partial_results = dict(results)
                    if (
                        ctrl is not None
                        and ctrl.is_paused
                        and results[sid].interaction_request
                    ):
                        wave_paused = True
                        break
                if wave_failed_exc is not None or wave_paused:
                    break

            if wave_paused and remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
                for task in list(remaining):
                    psid = task_objs[task]
                    if not task.done():
                        continue
                    exc = task.exception()
                    if exc:
                        wave_failed_exc = exc
                    elif psid not in results:
                        results[psid] = task.result()
                remaining.clear()

            if wave_failed_exc is not None:
                for task in task_objs:
                    if task.done() and not task.exception():
                        sid = task_objs[task]
                        if sid not in results:
                            results[sid] = task.result()
                self._partial_results = dict(results)
                sid = next(
                    sid for t, sid in task_objs.items()
                    if t.done() and t.exception() is wave_failed_exc
                )
                log_event(
                    "plan.wave_failed",
                    failed_stage_id=sid,
                    partial_success_count=sum(1 for t in task_objs if t.done() and not t.exception()),
                    level=logging.ERROR,
                )
                logger.error("Stage %s failed, cancelling wave peers", sid)
                if isinstance(wave_failed_exc, StageExecutionError):
                    wave_failed_exc.partial_results = dict(results)
                    raise wave_failed_exc
                raise StageExecutionError(
                    sid, stage_map[sid].agent_id, str(wave_failed_exc), partial_results=dict(results),
                )

            if wave_paused:
                await self._hooks.fire("wave_end", wave_stages=ready_stages, results={
                    sid: results[sid] for sid in results if sid in task_objs.values()
                })
                for sid, result in results.items():
                    if result.success:
                        completed.add(sid)
                        pending.discard(sid)
                if ctrl is not None:
                    log_event("plan.paused", reason=ctrl.check().reason, after="stage")
                    await ctrl.wait_if_paused()
                    log_event("plan.resumed")
                continue

            # 全成功 — 更新 completed / pending
            for sid in task_objs.values():
                result: StageResult = results[sid]
                if result.success:
                    completed.add(sid)
                    pending.discard(sid)
                else:
                    raise StageExecutionError(sid, stage_map[sid].agent_id, result.error)

            await self._hooks.fire("wave_end", wave_stages=ready_stages, results={
                sid: results[sid] for sid in task_objs.values()
            })

            log_event(
                "plan.wave_complete",
                wave_size=len(ready_stages),
                completed_count=len(completed),
            )

            if ctrl is not None:
                if ctrl.should_abort_after_wave():
                    log_event("plan.aborted", reason=ctrl.check().reason, graceful=True)
                    break
                from ..interrupt import InterruptSignal
                if ctrl.check().signal == InterruptSignal.PAUSE_AFTER_STAGE:
                    log_event("plan.paused", reason=ctrl.check().reason, after="stage")
                    await ctrl.wait_if_paused()
                    log_event("plan.resumed")

        return results

    def _adaptive_max_parallel(self, wave_agents: list[str]) -> int:
        """根据历史执行时间动态调整并发上限（仅放宽，不低于配置值）。"""
        if not self._max_parallel or not self._metrics_sink:
            return self._max_parallel

        metrics = self._metrics_sink.snapshot()
        agent_durations: dict[str, list[float]] = {}
        for rec in metrics:
            if rec.get("name") != "stage.duration_ms":
                continue
            aid = rec.get("tags", {}).get("agent_id", "")
            if aid in wave_agents:
                agent_durations.setdefault(aid, []).append(float(rec["value"]))

        if not agent_durations:
            return self._max_parallel

        averages = {
            aid: sum(vals) / len(vals)
            for aid, vals in agent_durations.items()
            if vals
        }
        if len(averages) < 2:
            return self._max_parallel

        vals = list(averages.values())
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        if variance < mean * mean * 0.3:
            return min(self._max_parallel * 2, len(wave_agents))
        return self._max_parallel

