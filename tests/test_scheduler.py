"""调度器测试。"""
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler


def test_get_active_on_backend():
    backend = InMemorySchedulerBackend()
    sched = SessionScheduler(backend=backend)
    result = sched.submit("s2")
    assert backend.get_active("s2") == 1
    sched.release("s2", result.run.run_id)
    assert backend.get_active("s2") == 0


def test_fifo_dequeue():
    backend = InMemorySchedulerBackend()
    from casa.config import init_config
    init_config(max_parallel_per_session=1, concurrency_policy="fifo")
    sched = SessionScheduler(backend=backend)
    r1 = sched.submit("fifo_sess")
    r2 = sched.submit("fifo_sess")
    assert r1.status == "accepted"
    assert r2.status == "queued"
    sched.release("fifo_sess", r1.run.run_id)
    run2 = backend.get_run(r2.run.run_id)
    assert run2 is not None
