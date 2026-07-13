"""P4/P5 测试 — 生命周期调度器、配置监听、死锁重规划、压力测试。"""
import tempfile
import threading
import time

import pytest

from casa.config import init_config, get_config, override_config
from casa.config_loader import ConfigWatcher
from casa.lifecycle import (
    ArtifactLifecycleManager, ArtifactRetentionPolicy,
    LifecycleCleanupScheduler, RetentionTier, scan_plan_dirs,
)
from casa.artifact import ArtifactStore
from casa.orchestration import (
    CompileRequest, MockAgentExecutor, Orchestrator, Plan, PlanCompiler,
    PlanExecutor, Stage, StageRunner,
)
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler


def test_scan_plan_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("job_a")
        store.init_plan("plan_1")
        store.write("x", {"v": 1})
        found = scan_plan_dirs(tmp)
        assert ("", "job_a", "plan_1") in found


def test_lifecycle_scheduler_run_once():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j1")
        store.init_plan("p1")
        store.write("scratch", {"t": 1})
        store.write("report", {"f": 1})

        mgr = ArtifactLifecycleManager(
            ArtifactRetentionPolicy(overrides={"scratch": RetentionTier.EPHEMERAL}),
        )
        sched = LifecycleCleanupScheduler(mgr, interval_seconds=60, base_dir=tmp)
        reports = sched.run_once()
        assert len(reports) == 1
        assert "scratch" in reports[0]["removed"]
        store2 = ArtifactStore("j1", base_dir=tmp)
        store2.init_plan("p1")
        assert store2.read("scratch") is None
        assert store2.read("report") is not None


def test_lifecycle_scheduler_background():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp, lifecycle_cleanup_interval_seconds=0.2)
        store = ArtifactStore("j1")
        store.init_plan("p1")
        store.write("scratch", {"t": 1})

        mgr = ArtifactLifecycleManager(
            ArtifactRetentionPolicy(overrides={"scratch": RetentionTier.EPHEMERAL}),
        )
        sched = LifecycleCleanupScheduler(mgr, interval_seconds=0.2, base_dir=tmp)
        sched.start()
        time.sleep(0.6)
        sched.stop()
        store2 = ArtifactStore("j1", base_dir=tmp)
        store2.init_plan("p1")
        assert store2.read("scratch") is None


def test_config_watcher_poll_reload():
    with tempfile.TemporaryDirectory() as tmp:
        import os
        path = os.path.join(tmp, "casa.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("debug = false\n")
        init_config(debug=False)
        watcher = ConfigWatcher(path, poll_seconds=0.1, mode="poll")
        watcher.start()
        time.sleep(0.15)
        with open(path, "w", encoding="utf-8") as f:
            f.write("debug = true\n")
        time.sleep(0.35)
        watcher.stop()
        assert get_config().debug is True


@pytest.mark.asyncio
async def test_deadlock_auto_replan():
    """循环依赖 plan 在 auto_replan_on_deadlock 下重新编译为合法 DAG 并成功执行。"""
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp, auto_replan_on_deadlock=True)
        agent_io = {"a": ([], "out_a"), "b": (["out_a"], "out_b")}
        store = ArtifactStore("j")
        store.init_plan("p")
        compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a", "b"})
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(
                store=store,
                stage_runner=StageRunner(
                    store=store,
                    executor=MockAgentExecutor({"a": {"v": 1}, "b": {"v": 2}}),
                ),
            ),
        )
        deadlock_plan = Plan(
            stages=[
                Stage(stage_id="a", agent_id="a", depends_on=["b"], output_artifact_kind="out_a"),
                Stage(stage_id="b", agent_id="b", depends_on=["a"], output_artifact_kind="out_b"),
            ],
        )
        results = await orch.executor.execute(deadlock_plan)
        assert results["a"].success
        assert results["b"].success
        assert store.read("out_a") is not None
        assert store.read("out_b") is not None


def test_scheduler_concurrent_fifo():
    """多线程并发 submit 不突破槽位上限，FIFO 保持顺序。"""
    backend = InMemorySchedulerBackend()
    init_config(max_parallel_per_session=2, concurrency_policy="fifo")
    sched = SessionScheduler(backend=backend)
    session = "stress_sess"
    lock = threading.Lock()
    results: list[str] = []
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            r = sched.submit(session, intent=f"run-{i}")
            with lock:
                results.append(r.status)
        except Exception as exc:
            with lock:
                errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(results) == 12
    assert backend.get_active(session) <= 2
    accepted = sum(1 for s in results if s == "accepted")
    queued = sum(1 for s in results if s == "queued")
    assert accepted + queued == 12
    assert accepted <= 2

    active_runs = [
        backend.get_run(rid)
        for rid in backend.list_runs(session)
        if backend.get_run(rid) and backend.get_run(rid).status in ("accepted", "running")
    ]
    for run in active_runs:
        sched.release(session, run.run_id)

    while backend.get_active(session) > 0:
        for rid in backend.list_runs(session):
            run = backend.get_run(rid)
            if run and run.status in ("accepted", "running"):
                sched.release(session, run.run_id)

    assert backend.get_active(session) == 0


def test_scheduler_slot_never_exceeds_cap_under_load():
    backend = InMemorySchedulerBackend()
    init_config(max_parallel_per_session=3, concurrency_policy="reject")
    sched = SessionScheduler(backend=backend)
    session = "cap_sess"
    peak_active = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal peak_active
        r = sched.submit(session)
        if r.status == "accepted" and r.run:
            with lock:
                peak_active = max(peak_active, backend.get_active(session))
            time.sleep(0.02)
            sched.release(session, r.run.run_id)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert peak_active <= 3
