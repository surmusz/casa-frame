"""Agent 循环测试。"""
import tempfile

import pytest

from casa.artifact import ArtifactStore
from casa.config import init_config
from casa.interrupt import InterruptController
from casa.orchestration import (
    MockAgentExecutor, Orchestrator,
    PlanCompiler, PlanExecutor, StageRunner,
)
from casa.loop import AgentLoop, LoopPhase, VerifierContext


@pytest.mark.asyncio
async def test_loop_single_pass(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(store=store, executor=MockAgentExecutor({"w": {"ok": True}})),
        ),
    )
    loop = AgentLoop(orchestrator=orch, require_double_pass=False)
    result = await loop.run("run task", deliverable_type="full")
    assert result.success
    assert result.stop_reason == "single_pass_verified"
    assert result.total_iterations == 1


@pytest.mark.asyncio
async def test_loop_double_pass(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(store=store, executor=MockAgentExecutor({"w": {"ok": True}})),
        ),
    )

    call_count = 0

    async def always_pass_verifier(result, context: VerifierContext):
        nonlocal call_count
        call_count += 1
        assert isinstance(context, VerifierContext)
        return []

    loop = AgentLoop(
        orchestrator=orch,
        verifier=always_pass_verifier,
        require_double_pass=True,
    )
    result = await loop.run("run task")
    assert result.success
    assert result.stop_reason == "double_pass_verified"
    assert call_count == 2


@pytest.mark.asyncio
async def test_loop_double_pass_succeeds_on_last_iteration(tmp_path):
    """末轮 iteration==max_iterations 时，double-pass 仍应成功结束。"""
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(store=store, executor=MockAgentExecutor({"w": {"ok": True}})),
        ),
    )

    async def always_pass_verifier(_result, _context: VerifierContext):
        return []

    loop = AgentLoop(
        orchestrator=orch,
        verifier=always_pass_verifier,
        require_double_pass=True,
        max_iterations=1,
    )
    result = await loop.run("run task")
    assert result.success
    assert result.stop_reason == "double_pass_verified"
    assert result.total_iterations == 1


@pytest.mark.asyncio
async def test_loop_iterate_on_issues(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(store=store, executor=MockAgentExecutor({"w": {"ok": True}})),
        ),
    )
    attempts = {"n": 0}

    async def fail_once_verifier(result, context: VerifierContext):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return ["first pass issue"]
        return []

    loop = AgentLoop(
        orchestrator=orch,
        verifier=fail_once_verifier,
        require_double_pass=False,
    )
    result = await loop.run("run task")
    assert result.success
    assert result.total_iterations == 2
    assert result.iterations[0].phase == LoopPhase.ITERATE


@pytest.mark.asyncio
async def test_loop_max_iterations(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(store=store, executor=MockAgentExecutor({"w": {"ok": True}})),
        ),
    )

    async def always_fail(_result, _context: VerifierContext):
        return ["always bad"]

    loop = AgentLoop(orchestrator=orch, verifier=always_fail, max_iterations=2, require_double_pass=False)
    result = await loop.run("run task")
    assert not result.success
    assert result.stop_reason == "max_iterations"


@pytest.mark.asyncio
async def test_loop_abort_by_interrupt(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    ctrl = InterruptController()
    ctrl.abort("user stop", graceful=False)
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(store=store, executor=MockAgentExecutor({"w": {"ok": True}})),
            interrupt_ctrl=ctrl,
        ),
        interrupt_ctrl=ctrl,
    )
    loop = AgentLoop(orchestrator=orch, require_double_pass=False)
    result = await loop.run("run task")
    assert not result.success
    assert result.stop_reason == "aborted"


@pytest.mark.asyncio
async def test_loop_with_replan(tmp_path):
    """第二轮应自动走 replan，additional_agents 由 loop 从 issues 推断。"""
    init_config(artifact_base_dir=str(tmp_path))
    calls: list[str] = []
    agent_io = {
        "fetch": ([], "raw"),
        "extra": (["raw"], "extra_out"),
    }
    store = ArtifactStore("j")
    store.init_plan("p")
    from casa.orchestration import SimpleAgentExecutor
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({
            "fetch": lambda c: (calls.append("fetch") or {"ok": True}),
            "extra": lambda c: (calls.append("extra") or {"ok": True}),
        }),
    )
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"fetch"}),
        executor=PlanExecutor(store=store, stage_runner=runner),
    )

    attempts = {"n": 0}

    async def replan_verifier(result, context: VerifierContext):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return ["need extra agent"]
        return []

    loop = AgentLoop(orchestrator=orch, verifier=replan_verifier, require_double_pass=False, max_iterations=3)
    result = await loop.run("analyze", deliverable_type="full")
    assert result.success
    assert "extra" in calls
    assert result.total_iterations == 2
    assert result.iterations[1].used_replan is True
    assert "fetch" not in calls[1:]  # replan 跳过已完成 fetch


@pytest.mark.asyncio
async def test_verifier_cannot_control_loop_via_context(tmp_path):
    """verifier 即使尝试写 dict 也不影响 loop（只读 VerifierContext）。"""
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(store=store, executor=MockAgentExecutor({"w": {"ok": True}})),
        ),
    )

    async def mutating_verifier(result, context: VerifierContext):
        with pytest.raises(Exception):
            context.loop_iteration = 99  # 冻结 dataclass，赋值应失败
        return []

    loop = AgentLoop(orchestrator=orch, verifier=mutating_verifier, require_double_pass=False)
    result = await loop.run("run task")
    assert result.success
    assert result.total_iterations == 1


@pytest.mark.asyncio
async def test_replan_agent_resolver_override(tmp_path):
    """自定义 replan_agent_resolver 可显式指定追加 agent。"""
    init_config(artifact_base_dir=str(tmp_path))
    calls: list[str] = []
    agent_io = {
        "fetch": ([], "raw"),
        "extra": (["raw"], "extra_out"),
    }
    store = ArtifactStore("j")
    store.init_plan("p")
    from casa.orchestration import SimpleAgentExecutor
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({
            "fetch": lambda c: (calls.append("fetch") or {"ok": True}),
            "extra": lambda c: (calls.append("extra") or {"ok": True}),
        }),
    )
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"fetch"}),
        executor=PlanExecutor(store=store, stage_runner=runner),
    )

    def resolver(_result, _issues, _ctx):
        return ["extra"]

    attempts = {"n": 0}

    async def fail_once(_result, _ctx: VerifierContext):
        attempts["n"] += 1
        return ["missing capability"] if attempts["n"] == 1 else []

    loop = AgentLoop(
        orchestrator=orch,
        verifier=fail_once,
        replan_agent_resolver=resolver,
        require_double_pass=False,
    )
    result = await loop.run("analyze")
    assert result.success
    assert "extra" in calls
