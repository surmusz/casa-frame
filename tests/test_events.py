"""事件总线测试。"""
import asyncio

import pytest

from casa.audit import InMemoryAuditSink, emit_audit, get_audit_sink, reset_audit_sink, set_audit_sink
from casa.events import Event, InProcessEventBus, get_event_bus, publish_event, reset_event_bus, set_event_bus
from casa.observability import log_event, reset_run_context, run_context


@pytest.fixture(autouse=True)
def _isolate():
    reset_event_bus()
    reset_audit_sink()
    reset_run_context()
    yield
    reset_event_bus()
    reset_audit_sink()
    reset_run_context()


def test_subscribe_and_publish():
    bus = InProcessEventBus()
    received: list[str] = []
    bus.subscribe("stage.*", lambda e: received.append(e.event_type))
    asyncio.run(bus.publish(Event(event_type="stage.completed", payload={"x": 1})))
    assert received == ["stage.completed"]


@pytest.mark.asyncio
async def test_async_handler():
    bus = InProcessEventBus()
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.event_type)

    bus.subscribe("run.*", handler, async_handler=True)
    await bus.publish(Event(event_type="run.accepted"))
    assert received == ["run.accepted"]


def test_emit_audit_publishes_to_bus():
    bus = InProcessEventBus()
    set_event_bus(bus)
    events: list[Event] = []
    bus.subscribe("audit.*", lambda e: events.append(e))

    sink = InMemoryAuditSink()
    set_audit_sink(sink)
    with run_context(run_id="r1", session_id="s1"):
        emit_audit("run.status_changed", new_status="accepted")

    assert len(sink.snapshot()) == 1
    assert any(e.event_type == "audit.run.status_changed" for e in events)


def test_log_event_publishes_to_bus():
    bus = InProcessEventBus()
    set_event_bus(bus)
    events: list[str] = []
    bus.subscribe("log.*", lambda e: events.append(e.event_type))
    log_event("plan.compiled", plan_id="p1")
    assert "log.plan.compiled" in events


def test_publish_event_includes_context():
    bus = InProcessEventBus()
    set_event_bus(bus)
    captured: list[Event] = []
    bus.subscribe("*", lambda e: captured.append(e))
    with run_context(run_id="r99"):
        publish_event("test.event", foo="bar")
    assert captured[0].run_context.get("run_id") == "r99"
    assert captured[0].payload["foo"] == "bar"
