"""中断控制器测试。"""
import asyncio

import pytest

from casa.interrupt import InterruptController, InterruptSignal


def test_pause_resume_cycle():
    ctrl = InterruptController()
    ctrl.pause("user pause")
    assert ctrl.is_paused
    assert ctrl.check().signal == InterruptSignal.PAUSE_AFTER_WAVE
    ctrl.resume()
    assert not ctrl.is_paused
    assert ctrl.check().signal == InterruptSignal.NONE


@pytest.mark.asyncio
async def test_wait_if_paused_blocks_until_resume():
    ctrl = InterruptController()
    ctrl.pause("hold")

    async def resume_later():
        await asyncio.sleep(0.05)
        ctrl.resume()

    task = asyncio.create_task(resume_later())
    await ctrl.wait_if_paused()
    await task


def test_abort_graceful_and_immediate():
    ctrl = InterruptController()
    ctrl.abort("stop", graceful=True)
    assert ctrl.check().signal == InterruptSignal.ABORT_GRACEFUL
    ctrl.resume()
    ctrl.abort("now", graceful=False)
    assert ctrl.check().signal == InterruptSignal.ABORT_IMMEDIATE


@pytest.mark.asyncio
async def test_plan_executor_pauses_between_waves(tmp_path):
    from casa.artifact import ArtifactStore
    from casa.config import init_config
    from casa.orchestration import (
        Orchestrator, Plan, PlanCompiler, PlanExecutor, Stage,
        StageRunner, SimpleAgentExecutor,
    )

    init_config(artifact_base_dir=str(tmp_path))
    calls: list[str] = []

    def make_handler(name: str):
        def handler(ctx):
            calls.append(name)
            return {"v": name}
        return handler

    agent_io = {
        "a": ([], "out_a"),
        "b": (["out_a"], "out_b"),
    }
    store = ArtifactStore("j")
    store.init_plan("p")
    ctrl = InterruptController()
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({"a": make_handler("a"), "b": make_handler("b")}),
    )
    executor = PlanExecutor(store=store, stage_runner=runner, interrupt_ctrl=ctrl)
    plan = Plan(stages=[
        Stage(stage_id="a", agent_id="a", output_artifact_kind="out_a", depends_on=[]),
        Stage(stage_id="b", agent_id="b", output_artifact_kind="out_b", depends_on=["a"]),
    ])

    async def run_with_pause():
        task = asyncio.create_task(executor.execute(plan))
        await asyncio.sleep(0.02)
        ctrl.pause("between waves")
        await asyncio.sleep(0.02)
        ctrl.resume()
        return await task

    results = await run_with_pause()
    assert results["a"].success
    assert results["b"].success
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_replan_skips_completed_stages(tmp_path):
    from casa.artifact import ArtifactStore
    from casa.config import init_config
    from casa.orchestration import (
        Orchestrator, PlanCompiler, PlanExecutor, StageRunner, SimpleAgentExecutor,
    )

    init_config(artifact_base_dir=str(tmp_path))
    calls: list[str] = []

    agent_io = {
        "fetch": ([], "raw"),
        "analyze": (["raw"], "analytics"),
        "extra": (["analytics"], "extra_out"),
    }
    store = ArtifactStore("j")
    store.init_plan("p")
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"fetch"})
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({
            "fetch": lambda c: (calls.append("fetch") or {"ok": True}),
            "analyze": lambda c: (calls.append("analyze") or {"ok": True}),
            "extra": lambda c: (calls.append("extra") or {"ok": True}),
        }),
    )
    orch = Orchestrator(
        compiler=compiler,
        executor=PlanExecutor(store=store, stage_runner=runner),
    )
    from casa.orchestration import CompileRequest

    await orch.run(
        CompileRequest(
            seed_stages=[{"agent_id": "fetch"}, {"agent_id": "analyze"}],
        ),
        job_id="j",
    )
    assert "fetch" in calls and "analyze" in calls
    calls.clear()

    result = await orch.replan(additional_agents=["extra"])
    assert "extra" in calls
    assert "fetch" not in calls
    assert "analyze" not in calls
    assert any(
        s.pre_completed for s in result.plan.stages
        if s.agent_id in ("fetch", "analyze")
    )
