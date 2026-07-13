"""Orchestrator 运维辅助（trace、交付物渲染、健康检查）。"""
from __future__ import annotations

from typing import Any

from ..artifact import ArtifactStore
from ..audit import get_audit_sink
from ..config import get_config
from ..observability import get_metrics_sink, InMemoryMetricsSink, log_event


async def debug_trace(orch: Any, run_id: str, *, include_io: bool = False) -> dict[str, Any]:
    """返回执行时间线 + 可选 IO 内容。"""
    from ..events import get_event_bus

    bus = get_event_bus()
    events = getattr(bus, "trace_events", None)
    if events is None:
        return {"error": "事件总线不支持 trace", "run_id": run_id}

    trace: list[dict[str, Any]] = []
    stage_starts: dict[str, str] = {}

    for evt in events:
        ctx = evt.run_context or {}
        if run_id and ctx.get("run_id") not in ("", run_id):
            continue
        if evt.event_type == "stage_start":
            stage_starts[evt.payload.get("stage_id", "")] = evt.timestamp
        elif evt.event_type in ("stage_end", "stage_error"):
            sid = evt.payload.get("stage_id", "")
            if sid in stage_starts:
                trace.append({
                    "stage_id": sid,
                    "agent_id": evt.payload.get("agent_id", ""),
                    "started_at": stage_starts.pop(sid),
                    "finished_at": evt.timestamp,
                    "status": "error" if evt.event_type == "stage_error" else "ok",
                })

    if include_io and orch.executor.store:
        store = orch.executor.store
        for stage_info in trace:
            sid = stage_info["stage_id"]
            agent_id = stage_info.get("agent_id", sid)
            for kind in store.list_artifacts():
                if kind.startswith(agent_id) or kind == agent_id:
                    data = store.read(kind)
                    if isinstance(data, dict):
                        stage_info["output_preview"] = {
                            k: str(v)[:500]
                            for k, v in list(data.items())[:10]
                        }
                    break

    return {"run_id": run_id, "stages": trace}


async def render_deliverable(orch: Any, deliverable_type: str, store: ArtifactStore) -> Any | None:
    from ..deliverable import get_deliverable_registry

    registry = get_deliverable_registry()
    kinds = registry.required_artifacts(deliverable_type) or set(store.list_artifacts())
    artifacts: dict[str, dict] = {}
    for kind in kinds:
        data = store.read(kind)
        if data is not None:
            artifacts[kind] = data
    output = await registry.render(deliverable_type, artifacts)
    if output is None:
        return None
    path = store.write_deliverable_file(output.content, filename=f"{deliverable_type}.{output.format}")
    log_event("deliverable.rendered", deliverable_type=deliverable_type, path=path)
    return {"output": output, "path": path}


def health_check(orch: Any) -> dict[str, Any]:
    """返回框架健康状态。"""
    from .._version import __version__

    status = "ok"
    report: dict[str, Any] = {
        "status": status,
        "version": __version__,
        "audit_sink": type(get_audit_sink()).__name__,
        "dry_run": get_config().dry_run,
    }

    store = orch.executor.store
    store_health = store.health()
    report["artifact_store"] = store_health
    if store_health.get("status") != "ok":
        status = "degraded"

    if orch.scheduler is not None:
        sched_health = orch.scheduler.health_summary()
        report["scheduler"] = sched_health
        if sched_health.get("status") != "ok":
            status = "degraded"
        report["zombie_candidates"] = orch.scheduler.preview_zombies()
    else:
        report["scheduler"] = {"status": "not_configured"}
        report["zombie_candidates"] = []

    sink = get_metrics_sink()
    report["metrics_sink"] = type(sink).__name__
    if isinstance(sink, InMemoryMetricsSink):
        report["metrics"] = sink.snapshot()

    report["status"] = status
    return report
