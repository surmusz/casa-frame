"""
CASA 可观测性 — 运行上下文、结构化日志与指标。

零外部依赖：MetricsSink ABC + InMemory 实现。
"""

from __future__ import annotations

import abc
import contextvars
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Any, Iterator

logger = logging.getLogger("casa.observability")

_run_context: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "casa_run_context", default=None
)


@dataclass
class RunContext:
    """单次 Run 的 correlation 上下文，经 contextvars 在 async/sync 调用链传播。"""

    run_id: str = ""
    session_id: str = ""
    plan_id: str = ""
    job_id: str = ""
    stage_id: str = ""
    user_id: str = ""
    tenant_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}

    def merge(self, **kwargs: str) -> RunContext:
        data = asdict(self)
        for key, value in kwargs.items():
            if value:
                data[key] = value
        return RunContext(**data)


def reset_run_context() -> None:
    """清除 RunContext（测试清理用）。"""
    _run_context.set(None)


def get_run_context() -> RunContext | None:
    return _run_context.get()


@contextmanager
def run_context(**kwargs: str) -> Iterator[RunContext]:
    """绑定/合并 RunContext；退出时恢复上一层上下文。"""
    current = get_run_context()
    if current:
        merged = current.merge(**kwargs)
    else:
        merged = RunContext(**{k: v for k, v in kwargs.items() if v})
    token = _run_context.set(merged)
    try:
        yield merged
    finally:
        _run_context.reset(token)


def bind_run_context(**kwargs: str) -> None:
    """非上下文管理器场景下更新当前 RunContext。"""
    current = get_run_context()
    if current:
        _run_context.set(current.merge(**kwargs))
    else:
        _run_context.set(RunContext(**{k: v for k, v in kwargs.items() if v}))


class ContextLogFilter(logging.Filter):
    """将 RunContext 字段注入每条 log record（供结构化日志消费）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_run_context()
        if ctx:
            for key, value in ctx.to_dict().items():
                setattr(record, key, value)
        return True


_logging_configured = False


def configure_casa_logging(level: int = logging.INFO) -> None:
    """为 casa.* logger 安装 ContextLogFilter（幂等）。"""
    global _logging_configured
    if _logging_configured:
        return
    filt = ContextLogFilter()
    for name in ("casa", "casa.orchestration", "casa.artifact", "casa.scheduler",
                 "casa.contract", "casa.observability"):
        log = logging.getLogger(name)
        log.addFilter(filt)
        if log.level == logging.NOTSET:
            log.setLevel(level)
    _logging_configured = True


def configure_casa_observability(level: int = logging.INFO) -> None:
    """一键配置 logging + 默认 InMemory metrics（幂等）。"""
    configure_casa_logging(level)
    if not isinstance(get_metrics_sink(), InMemoryMetricsSink):
        set_metrics_sink(InMemoryMetricsSink())


# ============================================================================
# 指标
# ============================================================================


class MetricsSink(abc.ABC):
    """指标接收器抽象。接入方可替换为 Prometheus/StatsD 等。"""

    @abc.abstractmethod
    def record(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        ...


class InMemoryMetricsSink(MetricsSink):
    """内存指标存储（测试/调试）。"""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        with self._lock:
            self._records.append({
                "name": name,
                "value": value,
                "tags": dict(tags or {}),
                "timestamp": time.time(),
            })

    def snapshot_for_agent(self, agent_id: str, *, name: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for rec in self._records:
                if rec.get("tags", {}).get("agent_id") != agent_id:
                    continue
                if name and rec.get("name") != name:
                    continue
                out.append(dict(rec))
            return out

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_metrics_sink: MetricsSink = InMemoryMetricsSink()
_metrics_lock = threading.Lock()


def get_metrics_sink() -> MetricsSink:
    return _metrics_sink


def set_metrics_sink(sink: MetricsSink) -> None:
    global _metrics_sink
    with _metrics_lock:
        _metrics_sink = sink


def reset_metrics_sink() -> None:
    global _metrics_sink
    with _metrics_lock:
        _metrics_sink = InMemoryMetricsSink()


def record_metric(name: str, value: float, **tags: str) -> None:
    get_metrics_sink().record(name, value, tags or None)


@contextmanager
def timed_stage(stage_id: str, agent_id: str) -> Iterator[None]:
    """记录 stage 执行耗时（stage.duration_ms）。"""
    start = time.perf_counter()
    with run_context(stage_id=stage_id):
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            record_metric(
                "stage.duration_ms",
                elapsed_ms,
                stage_id=stage_id,
                agent_id=agent_id,
            )
            ctx = get_run_context()
            logger.info(
                "Stage %s (%s) completed in %.1fms",
                stage_id,
                agent_id,
                elapsed_ms,
                extra={"event": "stage.completed", **(ctx.to_dict() if ctx else {})},
            )


def log_event(event: str, level: int = logging.INFO, **extra: Any) -> None:
    """结构化事件日志，自动附带 RunContext。"""
    from .events import publish_event

    ctx = get_run_context()
    payload: dict[str, Any] = {"event": event}
    if ctx:
        payload.update(ctx.to_dict())
    payload.update(extra)
    logger.log(level, "casa_event %s", event, extra=payload)
    publish_event(f"log.{event}", level=level, **extra)
