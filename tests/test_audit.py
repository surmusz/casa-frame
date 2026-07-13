"""审计追踪测试 — Phase 2。"""
from casa.audit import InMemoryAuditSink, set_audit_sink, reset_audit_sink
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler


def test_run_lifecycle_audit_events():
    sink = InMemoryAuditSink()
    set_audit_sink(sink)
    sched = SessionScheduler(backend=InMemorySchedulerBackend())
    result = sched.submit("sess_audit", user_id="u1")
    assert result.status == "accepted"
    run_id = result.run.run_id
    sched.release("sess_audit", run_id)
    events = sink.snapshot()
    types = [e.event_type for e in events]
    assert types.count("run.status_changed") >= 2
    assert events[0].timestamp <= events[-1].timestamp
    assert any(e.payload.get("new_status") == "accepted" for e in events)
    assert any(e.payload.get("new_status") == "done" for e in events)
    reset_audit_sink()


def test_null_audit_sink_zero_overhead():
    from casa.audit import NullAuditSink, emit_audit
    sink = NullAuditSink()
    set_audit_sink(sink)
    emit_audit("test.event", actor="test")  # 不应抛出异常
    reset_audit_sink()
