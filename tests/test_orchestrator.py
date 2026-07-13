"""编排器部分结果与 dry_run 测试。"""
import tempfile

import pytest

from casa.authority import CapabilityMatrix, CapabilityRow
from casa.config import init_config, override_config
from casa.artifact import ArtifactStore
from casa.intent import IntentRouter
from casa.orchestration import (
    CompileRequest, Orchestrator, Plan, PlanCompiler, PlanExecutor,
    SimpleAgentExecutor, Stage, StageExecutionError, StageRunner,
)


@pytest.mark.asyncio
async def test_partial_results_on_wave_failure():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        agent_io = {
            "ok": ([], "ok_out"),
            "fail": ([], "fail_out"),
        }
        store = ArtifactStore("j")
        store.init_plan("p")
        def fail_handler(ctx):
            raise RuntimeError("boom")

        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({
                "ok": lambda ctx: {"v": 1},
                "fail": fail_handler,
            }),
        )
        executor = PlanExecutor(store=store, stage_runner=runner)
        plan = Plan(stages=[
            Stage(stage_id="s1", agent_id="ok", output_artifact_kind="ok_out"),
            Stage(stage_id="s2", agent_id="fail", output_artifact_kind="fail_out"),
        ])
        with pytest.raises(StageExecutionError) as exc_info:
            await executor.execute(plan)
        assert "s1" in exc_info.value.partial_results
        assert exc_info.value.partial_results["s1"].success


@pytest.mark.asyncio
async def test_dry_run_skips_execute():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        agent_io = {"w": ([], "out")}
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"})
        store = ArtifactStore("j")
        store.init_plan("p")

        def boom(ctx):
            raise RuntimeError("should not run")

        runner = StageRunner(store=store, executor=SimpleAgentExecutor({"w": boom}))
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(store=store, stage_runner=runner),
        )
        with override_config(dry_run=True):
            result = await orch.run(CompileRequest(), job_id="j")
        assert result.stage_results == {}


@pytest.mark.asyncio
async def test_model_preference_in_context():
    captured: dict = {}

    def handler(ctx):
        captured.update(ctx)
        return {"ok": True}

    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        matrix = CapabilityMatrix()
        matrix.register(CapabilityRow(
            agent_id="w",
            model_preference="claude-sonnet-4",
            context_limit_tokens=200000,
        ))
        agent_io = {"w": ([], "out")}
        compiler = PlanCompiler(
            agent_io_map=agent_io,
            core_pipeline_ids={"w"},
            capability_matrix=matrix,
        )
        store = ArtifactStore("j")
        store.init_plan("p")
        runner = StageRunner(store=store, executor=SimpleAgentExecutor({"w": handler}))
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(store=store, stage_runner=runner),
        )
        await orch.run(CompileRequest(), job_id="j")
        assert captured["model_preference"] == "claude-sonnet-4"
        assert captured["context_limit_tokens"] == 200000


@pytest.mark.asyncio
async def test_run_from_intent_e2e():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp, dry_run=True)
        matrix = CapabilityMatrix()
        matrix.register(CapabilityRow(agent_id="w", display_name="Worker"))
        agent_io = {"w": ([], "out")}
        router = IntentRouter.from_capability_matrix(matrix, agent_io)
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"})
        store = ArtifactStore("j")
        store.init_plan("p")
        runner = StageRunner(store=store, executor=SimpleAgentExecutor({"w": lambda c: {}}))
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(store=store, stage_runner=runner),
        )
        result = await orch.run_from_intent(
            "运行分析任务",
            router=router,
            job_id="j",
        )
        assert len(result.plan.stages) >= 1
        assert any("无 LLM" in w for w in result.warnings)
