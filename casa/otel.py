"""
CASA OpenTelemetry 桥接 — 订阅 EventBus 并发出 span/指标。
"""

from __future__ import annotations

import logging
from typing import Any

from .events import Event, EventBus, get_event_bus

logger = logging.getLogger("casa.otel")


class OTelEventBridge:
    """将 CASA 事件桥接到 OpenTelemetry（可选依赖）。"""

    def __init__(self, bus: EventBus | None = None) -> None:
        self._bus = bus or get_event_bus()
        self._tracer: Any = None
        self._meter: Any = None
        self._installed = False

    def install(self) -> bool:
        try:
            from opentelemetry import trace, metrics
        except ImportError:
            logger.warning("OpenTelemetry 未安装：pip install 'casa-frame[otel]'")
            return False
        self._tracer = trace.get_tracer("casa")
        self._meter = metrics.get_meter("casa")
        self._bus.subscribe("*", self._on_event, async_handler=True)
        self._installed = True
        return True

    async def _on_event(self, event: Event) -> None:
        if not self._tracer:
            return
        with self._tracer.start_as_current_span(event.event_type) as span:
            for k, v in event.payload.items():
                span.set_attribute(f"casa.{k}", str(v))
            for k, v in event.run_context.items():
                span.set_attribute(f"casa.ctx.{k}", v)
