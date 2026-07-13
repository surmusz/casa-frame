"""
CASA Audit — 多 Agent 系统审计追踪。

默认 NullAuditSink（零开销）；接入方可注入 PG/ES 等后端。
"""

from __future__ import annotations

import abc
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from .observability import get_run_context
from .events import publish_event


@dataclass(kw_only=True)
class AuditEvent:
    """一条审计事件。"""

    event_type: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    run_id: str = ""
    session_id: str = ""
    actor: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditSink(abc.ABC):
    """审计事件接收器抽象。"""

    @abc.abstractmethod
    def emit(self, event: AuditEvent) -> None:
        ...


class NullAuditSink(AuditSink):
    """无操作审计接收器（默认）。"""

    def emit(self, event: AuditEvent) -> None:
        pass


class InMemoryAuditSink(AuditSink):
    """内存审计存储（测试/调试）。"""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


_audit_sink: AuditSink = NullAuditSink()
_audit_lock = threading.Lock()


def get_audit_sink() -> AuditSink:
    return _audit_sink


def set_audit_sink(sink: AuditSink) -> None:
    global _audit_sink
    with _audit_lock:
        _audit_sink = sink


def reset_audit_sink() -> None:
    global _audit_sink
    with _audit_lock:
        _audit_sink = NullAuditSink()


def emit_audit(
    event_type: str,
    *,
    actor: str = "",
    run_id: str = "",
    session_id: str = "",
    **payload: Any,
) -> None:
    """发射审计事件，自动从 RunContext 补全 run_id/session_id。"""
    ctx = get_run_context()
    get_audit_sink().emit(AuditEvent(
        event_type=event_type,
        run_id=run_id or (ctx.run_id if ctx else ""),
        session_id=session_id or (ctx.session_id if ctx else ""),
        actor=actor,
        payload=dict(payload),
    ))
    publish_event(f"audit.{event_type}", actor=actor, **payload)
