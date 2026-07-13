"""Plan 死锁恢复与运行时 replan。"""
from __future__ import annotations

from typing import Any

from ..observability import log_event
from .execute import StageResult
from .models import CompileRequest, CompileResult, Plan, UsagePolicy


async def recover_plan_from_deadlock(
    orch: Any,
    plan: Plan,
    pending: set[str],
    completed: set[str],
    results: dict[str, StageResult],
) -> Plan | None:
    """死锁时重新编译 plan，保留已完成 stage。"""
    stage_map = {s.stage_id: s for s in plan.stages}
    stuck_agents = {stage_map[sid].agent_id for sid in pending if sid in stage_map}
    completed_agents = {
        stage_map[sid].agent_id for sid in completed if sid in stage_map
    }
    if not stuck_agents:
        return None
    seed = sorted(completed_agents | stuck_agents)
    request = CompileRequest(
        preset_id="",
        seed_stages=[{"agent_id": a} for a in seed],
        deliverable_type=plan.deliverable_type,
        policy=UsagePolicy.for_patch(),
        intent_summary=f"[deadlock recovery] retry agents: {', '.join(sorted(stuck_agents))}",
    )
    compiled = orch.compiler.compile(request)
    if orch.normalizer:
        compiled.plan = orch.normalizer.normalize(
            compiled.plan, deliverable_type=plan.deliverable_type,
        )
    completed_artifacts = set(orch.executor.store.list_artifacts())
    successful_completed = {
        sid for sid in completed
        if sid in results and results[sid].success and not results[sid].skipped
    }
    for stage in compiled.plan.stages:
        if stage.stage_id in successful_completed:
            stage.pre_completed = True
            continue
        if stage.agent_id in stuck_agents:
            stage.pre_completed = False
            continue
        ak = stage.output_artifact_kind or stage.agent_id
        if ak in completed_artifacts and stage.stage_id not in pending:
            stage.pre_completed = True
    compiled.plan.previous_plan_id = plan.plan_id
    compiled.plan.version = plan.version + 1
    compiled.plan.revision_history = list(plan.revision_history) + [plan.plan_id]
    log_event(
        "plan.deadlock_replan",
        stuck_agents=list(stuck_agents),
        new_stage_count=len(compiled.plan.stages),
    )
    return compiled.plan


async def replan_pipeline(
    orch: Any,
    *,
    intent: str = "",
    deliverable_type: str = "",
    additional_agents: list[str] | None = None,
    router: Any | None = None,
    **run_kwargs: Any,
) -> CompileResult:
    """运行中重新编译 Plan，保留已完成的 stage。"""
    store = orch.executor.store
    completed_artifacts = set(store.list_artifacts())
    artifact_to_agent = {
        output: aid
        for aid, (_inputs, output) in orch.compiler.agent_io_map.items()
        if output
    }
    current_completed = set(orch._completed_agent_ids)
    for ak in completed_artifacts:
        agent = artifact_to_agent.get(ak)
        if agent:
            current_completed.add(agent)

    extra: set[str] = set(additional_agents or [])
    route_warnings: list[str] = []
    dt = deliverable_type or (orch._current_plan.deliverable_type if orch._current_plan else "full")
    policy = (
        orch._current_plan.usage_policy
        if orch._current_plan and orch._current_plan.usage_policy
        else UsagePolicy.for_user_start()
    )

    if router and intent:
        route_result = await router.route(intent, deliverable_type=dt)
        extra.update(route_result.agent_ids)
        route_warnings = list(route_result.warnings)

    new_seed = list(current_completed | extra)
    request = CompileRequest(
        preset_id="",
        seed_stages=[{"agent_id": aid} for aid in new_seed],
        deliverable_type=dt,
        policy=policy,
        intent_summary=intent or "dynamic replan",
    )
    result = orch.compiler.compile(request)
    if orch.normalizer:
        result.plan = orch.normalizer.normalize(result.plan, deliverable_type=result.plan.deliverable_type)

    if orch._current_plan:
        conflict = orch._detect_conflict(orch._current_plan, result.plan)
        result.warnings.append(
            f"replan conflict: {conflict['conflict_level']} "
            f"(overlap={conflict['overlap_ratio']})"
        )
        result.plan.previous_plan_id = orch._current_plan.plan_id
        result.plan.version = orch._current_plan.version + 1
        result.plan.revision_history = list(orch._current_plan.revision_history) + [
            orch._current_plan.plan_id,
        ]
        if conflict["recommendation"] == "abort_current_wave":
            if orch._interrupt_ctrl:
                orch._interrupt_ctrl.abort("replan conflict: divergent stages", graceful=True)
            result.warnings.append("replan 已中止：存在重大冲突，跳过执行")
            return result

    for stage in result.plan.stages:
        ak = stage.output_artifact_kind or stage.agent_id
        if ak in completed_artifacts:
            stage.pre_completed = True

    orch._current_plan = result.plan
    stage_results = await orch.executor.execute(result.plan)
    orch._track_interactions(stage_results)
    result.stage_results = stage_results
    result.warnings.extend(route_warnings)

    stage_agent = {s.stage_id: s.agent_id for s in result.plan.stages}
    orch._completed_agent_ids = {
        stage_agent[sid]
        for sid, sr in stage_results.items()
        if sr.success and sid in stage_agent
    }

    return result
