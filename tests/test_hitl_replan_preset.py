"""HITL、replan、preset、生命周期与调度测试。"""
import asyncio
import tempfile
from dataclasses import dataclass, field

import pytest

from casa.artifact import ArtifactStore
from casa.config import init_config
from casa.contract import ContractBuilder, ContractGate, ContractVersionError, BaseRequired, ContractField
from casa.intent import IntentRouter
from casa.observability import reset_metrics_sink, set_metrics_sink, InMemoryMetricsSink, record_metric
from casa.interrupt import InterruptController
from casa.orchestration import (
    CompileRequest, CompileResult, MockAgentExecutor, Orchestrator,
    Plan, PlanCompiler, PlanExecutor, PlanNormalizer, Preset, Stage, StageResult, StageRunner,
    UsagePolicy, AgentExecutor,
)
from casa.lifecycle import RetentionTier, ArtifactLifecycleManager, ArtifactRetentionPolicy
from casa.scheduler import SessionScheduler, InMemorySchedulerBackend
from casa.tenant import Tenant, reset_tenant_manager, get_tenant_manager


@dataclass(kw_only=True)
class _DomainRequired(BaseRequired):
    subject_ids: list[str] = field(default_factory=list)
    focus_id: str = ""

    @classmethod
    def fields(cls):
        return [
            ContractField(name="subject_ids", required=True, description="分析对象 ID 列表"),
            ContractField(name="focus_id", required=True, description="主分析对象 ID"),
            *super().fields(),
        ]


def test_contract_builder():
    builder = ContractBuilder(required_class=_DomainRequired)
    builder.offer("subject_ids", ["item-001"])
    assert not builder.is_complete()
    builder.offer("focus_id", "item-001")
    contract = builder.build(session_id="s1", user_id="u1")
    assert contract.required.subject_ids == ["item-001"]


def test_contract_gate_min_version():
    builder = ContractBuilder(required_class=_DomainRequired)
    builder.offer("subject_ids", ["item-001"])
    builder.offer("focus_id", "item-001")
    contract = builder.build(session_id="s1", user_id="u1")
    contract.version = 0
    gate = ContractGate(min_contract_version=1)
    with pytest.raises(ContractVersionError):
        gate.submit(contract)


def test_compile_result_summary_and_review():
    plan = Plan(summary="test plan", stages=[Stage(stage_id="s1", agent_id="a")])
    result = CompileResult(
        plan=plan,
        stage_results={
            "s1": StageResult(
                stage_id="s1", agent_id="a", success=True, quality_score=0.5,
            ),
        },
        warnings=["w1"],
    )
    summary = result.summary()
    assert summary["stages_total"] == 1
    assert summary["stages_success"] == 1
    feedback = result.review_feedback()
    assert feedback["actionable"] is True
    assert feedback["stages"][0]["low_quality"] is True


@pytest.mark.asyncio
async def test_mock_agent_injects_inputs():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        store.init_plan("p")
        store.write("raw", {"data": 1})

        agent_io = {"analyst": (["raw"], "analysis")}
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"analyst"})
        mock = MockAgentExecutor({"analyst": {"themes": ["A"]}})
        runner = StageRunner(store=store, executor=mock)
        result = compiler.compile(CompileRequest(seed_stages=[{"agent_id": "analyst"}]))
        await PlanExecutor(store=store, stage_runner=runner).execute(result.plan)

        assert mock.call_count == 1
        ctx = mock.calls_for("analyst")[0]["context"]
        assert "inputs" in ctx
        assert "raw" in ctx["inputs"]
        assert ctx["upstream_status"]["raw"] == "ok"


@pytest.mark.asyncio
async def test_hitl_interaction_and_respond():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        agent_io = {"asker": ([], "answer")}
        store = ArtifactStore("j")
        store.init_plan("p")
        ctrl = InterruptController()

        async def handler(ctx):
            return {
                "answer": "yes",
                "_meta": {
                    "interaction_request": {
                        "type": "confirm",
                        "message": "继续吗？",
                        "options": [{"id": "yes"}, {"id": "no"}],
                    },
                },
            }

        from casa.orchestration import SimpleAgentExecutor
        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({"asker": handler}),
            interrupt_ctrl=ctrl,
        )
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"asker"})
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(store=store, stage_runner=runner, interrupt_ctrl=ctrl),
            interrupt_ctrl=ctrl,
        )
        task = asyncio.create_task(
            orch.run(CompileRequest(seed_stages=[{"agent_id": "asker"}]), job_id="j"),
        )
        sid = ""
        for _ in range(100):
            partial = getattr(orch.executor, "_partial_results", {})
            if ctrl.is_paused and partial:
                sid = next(
                    s for s, r in partial.items() if r.interaction_request
                )
                break
            await asyncio.sleep(0.01)
        assert sid
        assert partial[sid].interaction_request is not None
        ok = await orch.respond_to(sid, {"choice": "yes"})
        assert ok
        result = await task
        assert result.stage_results[sid].interaction_response == {"choice": "yes"}


def test_detect_conflict():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        agent_io = {"a": ([], "o1"), "b": ([], "o2"), "c": ([], "o3")}
        store = ArtifactStore("j")
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a", "b", "c"})
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(store=store, stage_runner=StageRunner(
                store=store, executor=MockAgentExecutor({}),
            )),
        )
        old = Plan(stages=[
            Stage(stage_id="s_a", agent_id="a"),
            Stage(stage_id="s_b", agent_id="b"),
        ])
        new = Plan(stages=[Stage(stage_id="s_c", agent_id="c")])
        conflict = orch._detect_conflict(old, new)
        assert conflict["conflict_level"] == "major"
        assert conflict["overlap_ratio"] == 0.0


def test_preset_export_import():
    agent_io = {"w": ([], "out")}
    compiler = PlanCompiler(
        agent_io_map=agent_io,
        presets={"full": Preset(preset_id="full", selected_agent_ids=["w"])},
    )
    exported = compiler.export_preset("full")
    assert exported["format"] == "casa_preset_v1"
    compiler.presets.clear()
    assert compiler.import_preset(exported)
    assert "full" in compiler.presets


def test_intent_preset_discovery():
    router = IntentRouter(catalog={})
    router.register_presets(
        {"full": Preset(preset_id="full", display_name="全量分析")},
        descriptions={"full": "多 Agent 全量分析报告"},
    )
    found = router.find_preset("全量 分析")
    assert found is not None
    assert found.preset_id == "full"


def test_adaptive_max_parallel():
    sink = InMemoryMetricsSink()
    set_metrics_sink(sink)
    try:
        for _ in range(3):
            record_metric("stage.duration_ms", 100.0, agent_id="a")
            record_metric("stage.duration_ms", 110.0, agent_id="b")
        store = ArtifactStore("j")
        runner = StageRunner(store=store, executor=MockAgentExecutor({}))
        executor = PlanExecutor(
            store=store, stage_runner=runner,
            max_parallel_per_wave=2, metrics_sink=sink,
        )
        limit = executor._adaptive_max_parallel(["a", "b"])
        assert limit == 2
    finally:
        reset_metrics_sink()


def test_agent_health_check_disables_unhealthy():
    sink = InMemoryMetricsSink()
    set_metrics_sink(sink)
    try:
        for _ in range(5):
            record_metric("stage.failure", 1.0, agent_id="bad_agent")
        normalizer = PlanNormalizer(core_pipeline_ids={"bad_agent", "good_agent"})
        plan = Plan(stages=[
            Stage(stage_id="s1", agent_id="bad_agent"),
            Stage(stage_id="s2", agent_id="good_agent", depends_on=["s1"]),
        ])
        result = normalizer.normalize(plan)
        agent_ids = {s.agent_id for s in result.stages}
        assert "bad_agent" not in agent_ids
        assert "good_agent" in agent_ids
    finally:
        reset_metrics_sink()


def test_preset_usage_policy_roundtrip():
    policy = UsagePolicy.for_patch()
    agent_io = {"w": ([], "out")}
    compiler = PlanCompiler(
        agent_io_map=agent_io,
        presets={"patch": Preset(
            preset_id="patch",
            selected_agent_ids=["w"],
            usage_policy=policy,
        )},
    )
    exported = compiler.export_preset("patch")
    compiler.presets.clear()
    compiler.import_preset(exported)
    restored = compiler.presets["patch"]
    assert restored.usage_policy is not None
    assert restored.usage_policy.force_core_pipeline is False
    assert restored.usage_policy.allow_skip_core_if_artifacts_exist is True


@pytest.mark.asyncio
async def test_replan_e2e_pre_completed_and_conflict_warning(tmp_path):
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
    from casa.orchestration import SimpleAgentExecutor
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
    await orch.run(
        CompileRequest(seed_stages=[{"agent_id": "fetch"}, {"agent_id": "analyze"}]),
        job_id="j",
    )
    calls.clear()
    result = await orch.replan(additional_agents=["extra"])
    assert "extra" in calls
    assert "fetch" not in calls
    assert any("replan conflict" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_streaming_ws_forward():
    chunks: list[dict] = []

    class StreamingExecutor(AgentExecutor):
        async def execute(self, agent_id: str, context: dict) -> dict:
            return {"done": True}

        async def execute_streaming(self, agent_id, context, on_chunk=None):
            if on_chunk:
                await on_chunk("token", {"text": "hello"})
            return {"done": True}

    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        store.init_plan("p")
        agent_io = {"writer": ([], "doc")}

        async def ws_send(msg):
            chunks.append(msg)

        runner = StageRunner(
            store=store,
            executor=StreamingExecutor(),
            ws_sender=ws_send,
        )
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"writer"})
        plan = compiler.compile(CompileRequest(seed_stages=[{"agent_id": "writer"}])).plan
        await PlanExecutor(store=store, stage_runner=runner).execute(plan)
        assert any(c.get("type") == "stage.chunk.token" for c in chunks)


def test_daily_cost_cents_scheduler_reject():
    reset_tenant_manager()
    init_config(max_parallel_per_session=4)
    mgr = get_tenant_manager()
    mgr.register(Tenant(tenant_id="cost_t", quotas={"daily_cost_cents": 10}))
    mgr.record_cost_usage("cost_t", 10)
    sched = SessionScheduler(backend=InMemorySchedulerBackend())
    result = sched.submit("s1", tenant_id="cost_t")
    assert result.status == "rejected"
    assert "daily_cost_cents" in result.message


@pytest.mark.asyncio
async def test_orchestrator_auto_cleanup(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    store.write("scratch", {"x": 1})
    mgr = ArtifactLifecycleManager(
        ArtifactRetentionPolicy(overrides={"scratch": RetentionTier.EPHEMERAL}),
    )
    mgr.register_kind("out", RetentionTier.JOB)
    runner = StageRunner(
        store=store,
        executor=MockAgentExecutor({"w": {"v": 1}}),
    )
    orch = Orchestrator(
        compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"}),
        executor=PlanExecutor(store=store, stage_runner=runner),
        lifecycle_manager=mgr,
    )
    await orch.run(
        CompileRequest(seed_stages=[{"agent_id": "w"}]),
        job_id="j",
        auto_cleanup=True,
    )
    assert store.read("scratch") is None
    assert store.read("out") is not None


def test_adaptive_max_parallel_high_variance_unchanged():
    sink = InMemoryMetricsSink()
    set_metrics_sink(sink)
    try:
        for _ in range(5):
            record_metric("stage.duration_ms", 50.0, agent_id="fast")
            record_metric("stage.duration_ms", 5000.0, agent_id="slow")
        store = ArtifactStore("j")
        runner = StageRunner(store=store, executor=MockAgentExecutor({}))
        executor = PlanExecutor(
            store=store, stage_runner=runner,
            max_parallel_per_wave=2, metrics_sink=sink,
        )
        limit = executor._adaptive_max_parallel(["fast", "slow"])
        assert limit == 2
    finally:
        reset_metrics_sink()
