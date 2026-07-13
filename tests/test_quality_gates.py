"""Harness 对齐测试。"""
import tempfile

import pytest

from casa.authority import CapabilityMatrix, CapabilityRow
from casa.config import init_config
from casa.artifact import ArtifactStore
from casa.memory import InMemoryAgentMemory, MemoryRecord
from casa.policy import PolicyEngine, PolicyRule, RulePhase
from casa.orchestration import (
    CompileRequest, MockAgentExecutor, Orchestrator, PlanCompiler,
    PlanExecutor, QualityGate, QualityGateRule, QualityGateHook,
    StageRunner, UsagePolicy,
)


def test_inject_evaluators():
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="worker", data_write="out"))
    matrix.register(CapabilityRow(agent_id="judge", is_evaluator=True, data_write="out_eval"))
    agent_io = {"worker": ([], "out"), "judge": (["out"], "out_eval")}
    compiler = PlanCompiler(
        agent_io_map=agent_io,
        core_pipeline_ids={"worker"},
        capability_matrix=matrix,
    )
    result = compiler.compile(CompileRequest(seed_stages=[{"agent_id": "worker"}]))
    roles = {s.stage_role for s in result.plan.stages}
    assert "evaluator" in roles
    eval_stages = [s for s in result.plan.stages if s.stage_role == "evaluator"]
    assert eval_stages[0].eval_targets == ["worker"]


@pytest.mark.asyncio
async def test_evaluator_context_injection():
    captured: dict = {}

    async def judge_handler(ctx):
        captured.update(ctx)
        return {"score": 0.9}

    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        store.init_plan("p")
        store.write("out", {"data": 1})
        matrix = CapabilityMatrix()
        matrix.register(CapabilityRow(agent_id="worker", data_write="out"))
        matrix.register(CapabilityRow(agent_id="judge", is_evaluator=True, data_write="out_eval"))
        agent_io = {"worker": ([], "out"), "judge": (["out"], "out_eval")}
        compiler = PlanCompiler(
            agent_io_map=agent_io,
            core_pipeline_ids={"worker"},
            capability_matrix=matrix,
        )
        from casa.orchestration import SimpleAgentExecutor
        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({
                "worker": lambda c: {"ok": True},
                "judge": judge_handler,
            }),
        )
        plan = compiler.compile(CompileRequest(seed_stages=[{"agent_id": "worker"}])).plan
        await PlanExecutor(store=store, stage_runner=runner).execute(plan)
        assert "eval_targets_data" in captured
        assert "worker" in captured["eval_targets_data"]


def test_quality_gate_triggers_pause():
    from casa.interrupt import InterruptController

    ctrl = InterruptController()
    gate = QualityGate([
        QualityGateRule(
            rule_id="low_q",
            condition="quality_score < 0.7",
            action="pause",
        ),
    ])
    from casa.orchestration import Stage, StageResult
    stage = Stage(stage_id="s1", agent_id="a")
    result = StageResult(stage_id="s1", agent_id="a", success=True, quality_score=0.5)
    triggered = gate.evaluate(stage, result)
    assert triggered[0]["action"] == "pause"


def test_trajectory_summary():
    from casa.orchestration import CompileResult, Plan, Stage, StageResult

    plan = Plan(stages=[
        Stage(stage_id="w", agent_id="w", stage_role="producer"),
        Stage(stage_id="w_eval_j", agent_id="j", stage_role="evaluator"),
    ])
    result = CompileResult(
        plan=plan,
        stage_results={
            "w": StageResult(stage_id="w", agent_id="w", success=True),
            "w_eval_j": StageResult(stage_id="w_eval_j", agent_id="j", success=True),
        },
    )
    traj = result.trajectory_summary()
    assert traj["producer_count"] == 1
    assert traj["evaluator_count"] == 1


@pytest.mark.asyncio
async def test_context_trim_on_budget():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        store.init_plan("p")
        store.write("big", {"k": "x" * 10000})
        agent_io = {"w": (["big"], "out")}

        captured: dict = {}

        def handler(ctx):
            captured.update(ctx)
            return {"ok": True}

        from casa.orchestration import SimpleAgentExecutor
        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({"w": handler}),
        )
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"})
        plan = compiler.compile(CompileRequest(seed_stages=[{"agent_id": "w"}])).plan
        plan.stages[0].context_limit_tokens = 100
        await PlanExecutor(store=store, stage_runner=runner).execute(plan)
        assert "_meta" in captured or len(str(captured.get("inputs", {}))) < 10000


@pytest.mark.asyncio
async def test_agent_memory_records():
    mem = InMemoryAgentMemory()
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        store.init_plan("p")
        agent_io = {"w": ([], "out")}
        runner = StageRunner(
            store=store,
            executor=MockAgentExecutor({"w": {"v": 1}}),
            agent_memory=mem,
        )
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"})
        plan = compiler.compile(CompileRequest(seed_stages=[{"agent_id": "w"}])).plan
        await PlanExecutor(store=store, stage_runner=runner).execute(plan)
        records = await mem.recall("w")
        assert len(records) == 1
        assert records[0].outcome == "success"


def test_policy_engine_post_stage():
    from casa.orchestration import Stage, StageResult

    engine = PolicyEngine()
    engine.add(PolicyRule(
        rule_id="min_q",
        phase=RulePhase.POST_STAGE,
        condition="quality_score < 0.5",
        action="pause",
        action_message="质量过低",
    ))
    result = StageResult(stage_id="s", agent_id="a", success=True, quality_score=0.3)
    triggered = engine.evaluate(RulePhase.POST_STAGE, result=result)
    assert triggered[0]["action"] == "pause"


def test_sandbox_in_context():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        runner = StageRunner(store=store, executor=MockAgentExecutor({}))
        from casa.orchestration import Stage, Plan
        ctx = runner._build_execute_context(
            Stage(stage_id="w", agent_id="w", sandbox_memory_mb=256),
            Plan(),
            fresh_session=False,
        )
        assert ctx["sandbox"]["max_memory_mb"] == 256
        assert ctx["sandbox"]["enforced"] is True


def test_plan_review_mode():
    agent_io = {"w": ([], "out")}
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"})
    result = compiler.compile(
        CompileRequest(seed_stages=[{"agent_id": "w"}], review_mode=True),
    )
    assert result.plan.plan_type == "pending_review"


@pytest.mark.asyncio
async def test_cost_breakdown():
    from casa.observability import reset_metrics_sink, set_metrics_sink, InMemoryMetricsSink, record_metric

    sink = InMemoryMetricsSink()
    set_metrics_sink(sink)
    try:
        record_metric("stage.tokens_total", 100.0, agent_id="a")
        record_metric("stage.duration_ms", 50.0, agent_id="a")
        with tempfile.TemporaryDirectory() as tmp:
            init_config(artifact_base_dir=tmp)
            store = ArtifactStore("j")
            orch = Orchestrator(
                compiler=PlanCompiler(agent_io_map={"w": ([], "out")}),
                executor=PlanExecutor(
                    store=store,
                    stage_runner=StageRunner(store=store, executor=MockAgentExecutor({})),
                ),
            )
            breakdown = orch.cost_breakdown()
            assert breakdown["total_tokens"] == 100.0
            assert "a" in breakdown["per_agent"]
    finally:
        reset_metrics_sink()
