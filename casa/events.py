"""
CASA 事件总线 — 审计、日志与 pipeline 事件的发布/订阅。
"""

from __future__ import annotations

import abc
import asyncio
import fnmatch
import inspect
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .observability import get_run_context

EventHandler = Callable[["Event"], None | Awaitable[None]]


@dataclass(kw_only=True)
class Event:
    """可发布的框架事件。"""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    run_context: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus(abc.ABC):
    """事件总线：按模式订阅，向处理器发布。"""

    @abc.abstractmethod
    def subscribe(
        self,
        event_pattern: str,
        handler: EventHandler,
        *,
        async_handler: bool = False,
    ) -> str:
        ...

    @abc.abstractmethod
    def unsubscribe(self, subscription_id: str) -> bool:
        """按 subscribe() 返回的 id 移除订阅。"""
        ...

    @abc.abstractmethod
    async def publish(self, event: Event) -> None:
        ...

    def publish_sync(self, event: Event) -> None:
        """从同步代码发布（必要时在新事件循环上运行异步处理器）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.publish(event))
        else:
            loop.create_task(self.publish(event))


@dataclass
class _Subscription:
    sub_id: str
    pattern: str
    handler: EventHandler
    async_handler: bool


class InProcessEventBus(EventBus):
    """进程内事件总线，支持 glob 模式匹配。"""

    _TRACE_LIMIT = 2000

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []
        self._lock = threading.Lock()
        self._trace_events: list[Event] = []

    @property
    def trace_events(self) -> list[Event]:
        with self._lock:
            return list(self._trace_events)

    def subscribe(
        self,
        event_pattern: str,
        handler: EventHandler,
        *,
        async_handler: bool = False,
    ) -> str:
        import uuid
        sub_id = uuid.uuid4().hex
        with self._lock:
            self._subscriptions.append(
                _Subscription(
                    sub_id=sub_id,
                    pattern=event_pattern,
                    handler=handler,
                    async_handler=async_handler,
                )
            )
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [
                s for s in self._subscriptions if s.sub_id != subscription_id
            ]
            return len(self._subscriptions) < before

    def _matching(self, event_type: str) -> list[_Subscription]:
        with self._lock:
            return [
                s for s in self._subscriptions
                if fnmatch.fnmatch(event_type, s.pattern)
            ]

    async def publish(self, event: Event) -> None:
        with self._lock:
            self._trace_events.append(event)
            if len(self._trace_events) > self._TRACE_LIMIT:
                self._trace_events = self._trace_events[-self._TRACE_LIMIT // 2:]
        for sub in self._matching(event.event_type):
            try:
                if sub.async_handler:
                    result = sub.handler(event)
                    if inspect.isawaitable(result):
                        await result
                else:
                    result = sub.handler(event)
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                # 处理器不得中断 pipeline
                continue


_event_bus: EventBus = InProcessEventBus()
_event_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    return _event_bus


def set_event_bus(bus: EventBus) -> None:
    global _event_bus
    with _event_bus_lock:
        _event_bus = bus


def reset_event_bus() -> None:
    global _event_bus
    with _event_bus_lock:
        _event_bus = InProcessEventBus()


def publish_event(event_type: str, **payload: Any) -> None:
    """便捷方法：附带 RunContext 构建 Event 并同步发布。"""
    ctx = get_run_context()
    event = Event(
        event_type=event_type,
        payload=dict(payload),
        run_context=ctx.to_dict() if ctx else {},
    )
    get_event_bus().publish_sync(event)
