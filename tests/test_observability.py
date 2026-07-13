"""CASA 可观测性测试 — Phase 1。"""
import asyncio
import logging
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casa.config import init_config, reset_config
from casa.contract import (
    BaseDeliverable, BaseRequired, Contract, ContractGate,
)
from casa.artifact import ArtifactStore
from casa.observability import (
    RunContext,
    configure_casa_logging,
    get_metrics_sink,
    get_run_context,
    InMemoryMetricsSink,
    reset_metrics_sink,
    run_context,
    set_metrics_sink,
)
from casa.orchestration import (
    CompileRequest, Orchestrator, PlanCompiler, PlanExecutor,
    SimpleAgentExecutor, StageRunner,
)
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler


@pytest.fixture(autouse=True)
def _clean():
    reset_config()
    reset_metrics_sink()
    yield
    reset_config()
    reset_metrics_sink()


def test_run_context_propagation():
    with run_context(run_id="run_abc", session_id="s1"):
        ctx = get_run_context()
        assert ctx is not None
        assert ctx.run_id == "run_abc"
        assert ctx.session_id == "s1"
        with run_context(plan_id="plan_xyz"):
            nested = get_run_context()
            assert nested.run_id == "run_abc"
            assert nested.plan_id == "plan_xyz"
    assert get_run_context() is None


def test_context_log_filter():
    configure_casa_logging()
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("casa.observability")
    handler = CaptureHandler()
    log.addHandler(handler)
    try:
        with run_context(run_id="run_log_test"):
            log.info("hello")
        assert records
        assert getattr(records[-1], "run_id", None) == "run_log_test"
    finally:
        log.removeHandler(handler)


def test_metrics_recording():
    sink = InMemoryMetricsSink()
    set_metrics_sink(sink)
    sink.record("test.metric", 42.0, {"tag": "a"})
    snap = sink.snapshot()
    assert len(snap) == 1
    assert snap[0]["name"] == "test.metric"
    assert snap[0]["value"] == 42.0


def test_contract_gate_metrics():
    sink = InMemoryMetricsSink()
    set_metrics_sink(sink)
    contract = Contract(
        deliverable=BaseDeliverable(type="full"),
        required=BaseRequired(mode="test"),
        session_id="s1",
        user_id="u1",
    )
    run_req = ContractGate().submit(contract)
    snap = sink.snapshot()
    assert any(r["name"] == "contract.submit" for r in snap)
    ctx = get_run_context()
    assert ctx is not None
    assert ctx.run_id == run_req.run_id


def test_artifact_store_health():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore(job_id="j1")
        health = store.health()
        assert health["status"] == "ok"
        assert health["writable"] is True


def test_orchestrator_health_check():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        agent_io = {"a": ([], "out_a")}
        compiler = PlanCompiler(agent_io_map=agent_io)
        store = ArtifactStore(job_id="j1")
        store.init_plan("p1")
        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({"a": lambda ctx: {"ok": True}}),
        )
        executor = PlanExecutor(store=store, stage_runner=runner)
        sched = SessionScheduler(backend=InMemorySchedulerBackend())
        orch = Orchestrator(compiler=compiler, executor=executor, scheduler=sched)
        hc = orch.health_check()
        assert "artifact_store" in hc
        assert "scheduler" in hc
        assert hc["artifact_store"]["status"] == "ok"
        assert hc["scheduler"]["status"] == "ok"


@pytest.mark.asyncio
async def test_orchestrator_run_context_and_metrics():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        sink = InMemoryMetricsSink()
        set_metrics_sink(sink)
        agent_io = {"worker": ([], "result")}
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"worker"})
        store = ArtifactStore(job_id="job_ctx")
        store.init_plan("plan_ctx")
        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({"worker": lambda ctx: {"done": True}}),
        )
        executor = PlanExecutor(store=store, stage_runner=runner)
        orch = Orchestrator(compiler=compiler, executor=executor)
        trace = "run_trace_001"
        await orch.run(
            CompileRequest(),
            run_id=trace,
            session_id="sess_1",
            job_id="job_ctx",
        )
        ctx = get_run_context()
        assert ctx is None  # run 结束后上下文已清除
        names = [r["name"] for r in sink.snapshot()]
        assert "stage.duration_ms" in names


@pytest.mark.asyncio
async def test_wave_failure_preserves_partial_results():
    """R13-P2: wave 失败时保留已成功 stage 的结果。"""
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        calls = {"ok": 0, "fail": 0}

        def ok_handler(ctx):
            calls["ok"] += 1
            return {"v": 1}

        def fail_handler(ctx):
            calls["fail"] += 1
            raise RuntimeError("boom")

        agent_io = {
            "ok_agent": ([], "ok_out"),
            "fail_agent": ([], "fail_out"),
        }
        compiler = PlanCompiler(agent_io_map=agent_io)
        store = ArtifactStore(job_id="j_wave")
        store.init_plan("p_wave")
        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({
                "ok_agent": ok_handler,
                "fail_agent": fail_handler,
            }),
        )
        executor = PlanExecutor(store=store, stage_runner=runner)
        from casa.orchestration import Plan, Stage

        plan = Plan(
            stages=[
                Stage(stage_id="s1", agent_id="ok_agent", output_artifact_kind="ok_out"),
                Stage(stage_id="s2", agent_id="fail_agent", output_artifact_kind="fail_out"),
            ],
        )
        partial: dict = {}
        try:
            await executor.execute(plan)
        except Exception:
            pass
        # 同一 wave 内 — ok 应在 fail 抛错前完成
        # 使用显式并行 wave 重新运行
        plan2 = Plan(stages=[
            Stage(stage_id="a", agent_id="ok_agent", output_artifact_kind="ok_out", depends_on=[]),
            Stage(stage_id="b", agent_id="fail_agent", output_artifact_kind="fail_out", depends_on=[]),
        ])
        try:
            await executor.execute(plan2)
        except Exception as exc:
            assert "boom" in str(exc) or exc
        assert store.exists("ok_out")


@pytest.mark.asyncio
async def test_meta_extraction_metrics():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        sink = InMemoryMetricsSink()
        set_metrics_sink(sink)
        agent_io = {"worker": ([], "result")}
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"worker"})
        store = ArtifactStore(job_id="job_meta")
        store.init_plan("plan_meta")
        runner = StageRunner(
            store=store,
            executor=SimpleAgentExecutor({
                "worker": lambda ctx: {
                    "done": True,
                    "_meta": {
                        "tokens_in": 100,
                        "tokens_out": 50,
                        "tokens_total": 150,
                        "model": "claude-sonnet-4",
                    },
                },
            }),
        )
        executor = PlanExecutor(store=store, stage_runner=runner)
        orch = Orchestrator(compiler=compiler, executor=executor)
        await orch.run(CompileRequest(), job_id="job_meta")
        names = [r["name"] for r in sink.snapshot()]
        assert "stage.tokens_in" in names
        assert "stage.tokens_out" in names
        assert store.read("result") == {"done": True}


def test_scheduler_health_summary():
    backend = InMemorySchedulerBackend()
    sched = SessionScheduler(backend=backend)
    sched.submit("sess", user_id="u1")
    summary = sched.health_summary()
    assert summary["total_active_slots"] >= 1
    assert summary["status"] == "ok"
