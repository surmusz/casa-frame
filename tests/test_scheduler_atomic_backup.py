"""原子调度器与产物备份测试。"""
import tempfile

import pytest

from casa.config import init_config
from casa.artifact import ArtifactStore
from casa.lifecycle import ArtifactBackupManager
from casa.scheduler import (
    InMemorySchedulerBackend, RunRecord, SessionScheduler,
)


def test_atomic_accept_run_memory():
    backend = InMemorySchedulerBackend()
    init_config(max_parallel_per_session=2)
    sched = SessionScheduler(backend=backend)
    r1 = sched.submit("atomic_sess")
    assert r1.status == "accepted"
    assert backend.get_active("atomic_sess") == 1

    run2 = RunRecord(session_id="atomic_sess", status="accepted", slots_used=1)
    acquired, active = backend.atomic_accept_run(run2, cap=2)
    assert acquired is True
    assert active == 2
    assert backend.get_run(run2.run_id) is not None


def test_atomic_accept_respects_cap():
    backend = InMemorySchedulerBackend()
    backend._active["s"] = 2
    run = RunRecord(session_id="s", status="accepted", slots_used=1)
    acquired, active = backend.atomic_accept_run(run, cap=2)
    assert acquired is False
    assert active == 2


def test_artifact_backup_and_restore():
    with tempfile.TemporaryDirectory() as tmp:
        backup_root = tmp + "/backups"
        init_config(artifact_base_dir=tmp + "/data")
        store = ArtifactStore("job1")
        store.init_plan("plan_a")
        store.write("raw", {"v": 1})
        store.write("report", {"title": "R"})

        mgr = ArtifactBackupManager()
        report = mgr.backup_plan(store, backup_root)
        assert report["file_count"] == 2

        store.delete("raw")
        store.delete("report")
        assert store.read("raw") is None

        restore = mgr.restore_plan(store, backup_root)
        assert "raw" in restore["restored_kinds"]
        assert store.read("raw") == {"v": 1}
        assert store.read("report") == {"title": "R"}


def test_backup_job_all_plans():
    with tempfile.TemporaryDirectory() as tmp:
        data_root = tmp + "/data"
        backup_root = tmp + "/backups"
        init_config(artifact_base_dir=data_root)
        for pid in ("p1", "p2"):
            s = ArtifactStore("job_x")
            s.init_plan(pid)
            s.write("out", {"plan": pid})

        mgr = ArtifactBackupManager()
        reports = mgr.backup_job(data_root, "job_x", backup_root)
        assert len(reports) == 2


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("redis") is None,
    reason="redis not installed",
)
def test_redis_scheduler_atomic_accept():
    try:
        import redis
        client = redis.Redis.from_url("redis://localhost:6379/15", decode_responses=True)
        client.ping()
    except Exception:
        pytest.skip("redis server not available")

    from casa.scheduler import RedisSchedulerBackend

    client.flushdb()
    backend = RedisSchedulerBackend("redis://localhost:6379/15")
    init_config(max_parallel_per_session=2)
    sched = SessionScheduler(backend=backend)

    r1 = sched.submit("redis_sess")
    assert r1.status == "accepted"
    assert backend.get_active("redis_sess") == 1

    r2 = sched.submit("redis_sess")
    assert r2.status == "accepted"
    assert backend.get_active("redis_sess") == 2

    r3 = sched.submit("redis_sess")
    assert r3.status in ("queued", "rejected")

    client.flushdb()
