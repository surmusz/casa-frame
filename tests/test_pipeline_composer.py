"""Phase 3 测试：schema 注册表、pipeline 组合器、烟雾集成。"""
import os
import subprocess
import sys

import pytest

from casa.orchestration import CompileRequest, Orchestrator, PlanCompiler, PlanExecutor, PipelineComposer, PipelineStep, StageRunner, SimpleAgentExecutor
from casa.artifact import ArtifactStore
from casa.config import init_config
from casa.schema_registry import InMemorySchemaRegistry, reset_schema_registry


def test_schema_registry_compat():
    reset_schema_registry()
    reg = InMemorySchemaRegistry()
    reg.register("out", 1, {"required": ["a"]})
    reg.register("out", 2, {"required": ["a", "b"]})
    assert reg.check_compatible("out", 1, 2)
    assert not reg.check_compatible("out", 2, 1)


@pytest.mark.asyncio
async def test_pipeline_composer(tmp_path):
    init_config(artifact_base_dir=str(tmp_path), dry_run=True)
    agent_io = {"a": ([], "out")}
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a"})
    store = ArtifactStore("j")
    store.init_plan("p")
    runner = StageRunner(store=store, executor=SimpleAgentExecutor({"a": lambda c: {}}))
    executor = PlanExecutor(store=store, stage_runner=runner)
    orch = Orchestrator(compiler=compiler, executor=executor)
    composer = PipelineComposer(orch)
    results = await composer.run_sequence([
        PipelineStep(plan_request=CompileRequest()),
    ])
    assert len(results) == 1


@pytest.mark.asyncio
async def test_pipeline_composer_multi_step_skip(tmp_path):
    init_config(artifact_base_dir=str(tmp_path), dry_run=True)
    agent_io = {"a": ([], "out")}
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a"})
    store = ArtifactStore("j")
    store.init_plan("p")
    runner = StageRunner(store=store, executor=SimpleAgentExecutor({"a": lambda c: {}}))
    executor = PlanExecutor(store=store, stage_runner=runner)
    orch = Orchestrator(compiler=compiler, executor=executor)
    composer = PipelineComposer(orch)

    results = await composer.run_sequence([
        PipelineStep(plan_request=CompileRequest()),
        PipelineStep(
            plan_request=CompileRequest(),
            condition="stage_count > 99",
            on_skip="continue",
        ),
    ])
    assert len(results) == 1


@pytest.mark.asyncio
async def test_debug_trace(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"a": ([], "out")}
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a"})
    store = ArtifactStore("j")
    store.init_plan("p")
    runner = StageRunner(store=store, executor=SimpleAgentExecutor({"a": lambda c: {"v": 1}}))
    executor = PlanExecutor(store=store, stage_runner=runner)
    orch = Orchestrator(compiler=compiler, executor=executor)
    trace_id = "run_trace_test"
    await orch.run(CompileRequest(), run_id=trace_id, job_id="j")
    timeline = await orch.debug_trace(trace_id)
    assert "stages" in timeline
    assert len(timeline["stages"]) >= 1


def test_smoke_script_passes():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "casa_smoke.py")],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
