"""Pipeline 钩子测试。"""
import pytest

from casa.hooks import HookRegistry, PipelineHook
from casa.orchestration import CompileRequest, Orchestrator, PlanCompiler, PlanExecutor, StageRunner, SimpleAgentExecutor
from casa.artifact import ArtifactStore
from casa.config import init_config


class _CaptureHook(PipelineHook):
    def __init__(self):
        self.events: list[str] = []

    async def on_compile_start(self, request):
        self.events.append("compile_start")

    async def on_execute_end(self, plan, results):
        self.events.append("execute_end")


@pytest.mark.asyncio
async def test_orchestrator_fires_hooks(tmp_path):
    init_config(artifact_base_dir=str(tmp_path), dry_run=True)
    hook = _CaptureHook()
    registry = HookRegistry()
    registry.register(hook)
    store = ArtifactStore("j1")
    store.init_plan("p1")
    agent_io = {"a": ([], "out")}
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a"})
    runner = StageRunner(store=store, executor=SimpleAgentExecutor({"a": lambda c: {}}), hooks=registry)
    executor = PlanExecutor(store=store, stage_runner=runner, hooks=registry)
    orch = Orchestrator(compiler=compiler, executor=executor, hooks=registry)
    await orch.run(CompileRequest())
    assert "compile_start" in hook.events
