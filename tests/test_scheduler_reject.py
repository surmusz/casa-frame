"""调度器拒绝路径与幂等性测试。"""
from casa.config import init_config, reset_config
from casa.config import ConcurrencyPolicy
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler


def test_reject_policy_returns_rejected_not_error():
    init_config(max_parallel_per_session=1, concurrency_policy=ConcurrencyPolicy.REJECT)
    sched = SessionScheduler(backend=InMemorySchedulerBackend())
    r1 = sched.submit("rej_sess")
    r2 = sched.submit("rej_sess")
    assert r1.status == "accepted"
    assert r2.status == "rejected"
    assert r2.run is None
    assert "1" in r2.message


def test_idempotency_key_replay():
    init_config(max_parallel_per_session=4)
    sched = SessionScheduler(backend=InMemorySchedulerBackend())
    r1 = sched.submit("idem", idempotency_key="key-1")
    r2 = sched.submit("idem", idempotency_key="key-1")
    assert r1.run is not None
    assert r2.run is not None
    assert r1.run.run_id == r2.run.run_id
    assert r2.message == "idempotent replay"


def test_tenant_filter_list_runs():
    backend = InMemorySchedulerBackend()
    sched = SessionScheduler(backend=backend)
    sched.submit("s1", tenant_id="t-a")
    sched.submit("s1", tenant_id="t-b")
    assert len(backend.list_runs("s1", tenant_id="t-a")) == 1


def test_mark_failed_dequeues_fifo():
    init_config(max_parallel_per_session=1, concurrency_policy=ConcurrencyPolicy.FIFO)
    dispatched: list[str] = []
    sched = SessionScheduler(
        backend=InMemorySchedulerBackend(),
        dispatch_callback=lambda _s, run_id: dispatched.append(run_id),
    )
    r1 = sched.submit("fifo_fail")
    r2 = sched.submit("fifo_fail")
    assert r1.status == "accepted"
    assert r2.status == "queued"
    assert r1.run is not None
    sched.mark_failed("fifo_fail", r1.run.run_id)
    assert r2.run is not None
    assert r2.run.run_id in dispatched
    assert sched.backend.get_run(r2.run.run_id).status == "accepted"


def test_release_uses_run_slots_used():
    init_config(max_parallel_per_session=3)
    sched = SessionScheduler(backend=InMemorySchedulerBackend())
    r = sched.submit("multi_slot", slots_needed=2)
    assert r.status == "accepted"
    assert sched.active_count("multi_slot") == 2
    sched.release("multi_slot", r.run.run_id)
    assert sched.active_count("multi_slot") == 0
