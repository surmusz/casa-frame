"""
CASA HTTP API — 用于 Orchestrator 集成的可选 FastAPI 路由。
"""

from __future__ import annotations

from typing import Any


def create_router(
    orchestrator: Any,
    scheduler: Any | None = None,
    *,
    agents_catalog: Any | None = None,
) -> Any:
    """
    创建封装 Orchestrator 与可选 Scheduler 的 FastAPI APIRouter。

    依赖：pip install 'casa-frame[api]'

    安全：本路由**默认无认证**。仅适合受信内网或本地调试；
    对公网暴露前必须自行叠加鉴权（API Key / OAuth / 反向代理 ACL 等）。
    """
    try:
        from fastapi import APIRouter, HTTPException
    except ImportError as exc:
        raise ImportError(
            "FastAPI 未安装：pip install 'casa-frame[api]'"
        ) from exc

    router = APIRouter(prefix="/casa", tags=["casa"])
    _runs: dict[str, dict[str, Any]] = {}
    _agents_cache = agents_catalog

    @router.get("/agents")
    async def list_agents() -> dict[str, Any]:
        """返回当前所有 Agent 的能力声明。"""
        if _agents_cache is None:
            return {"agents": [], "warning": "未配置 agents catalog"}
        catalog = getattr(_agents_cache, "catalog", None) or getattr(_agents_cache, "_catalog", {})
        return {
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "display_name": a.display_name,
                    "description": a.description,
                    "input_artifacts": a.input_artifacts,
                    "output_artifact": a.output_artifact,
                    "execution_profile": a.execution_profile,
                    "tags": a.tags,
                }
                for a in catalog.values()
            ],
        }

    @router.post("/loops")
    async def run_loop(body: dict[str, Any]) -> dict[str, Any]:
        """启动一个 Agent Loop。"""
        result = await orchestrator.run_in_loop(
            intent=body["intent"],
            max_iterations=body.get("max_iterations", 5),
            require_double_pass=body.get("require_double_pass", True),
            session_id=body.get("session_id", ""),
            job_id=body.get("job_id", ""),
            user_id=body.get("user_id", ""),
            tenant_id=body.get("tenant_id", ""),
        )
        return {
            "success": result.success,
            "total_iterations": result.total_iterations,
            "stop_reason": result.stop_reason,
            "summary": result.summary,
            "iterations": [
                {
                    "num": it.iteration_num,
                    "phase": it.phase.value,
                    "plan_summary": it.plan_summary,
                    "verification_passed": it.verification_passed,
                    "issues": it.verification_issues,
                }
                for it in result.iterations
            ],
        }

    @router.get("/presets")
    async def list_presets() -> dict[str, Any]:
        """返回所有 preset 及可导出模板。"""
        presets = getattr(orchestrator.compiler, "presets", {})
        exported = [
            orchestrator.compiler.export_preset(pid)
            for pid in presets
        ]
        return {"presets": [p for p in exported if p is not None]}

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return orchestrator.health_check()

    @router.post("/runs")
    async def submit_run(body: dict[str, Any]) -> dict[str, Any]:
        from .orchestration import CompileRequest
        request = CompileRequest(
            preset_id=body.get("preset_id", ""),
            deliverable_type=body.get("deliverable_type", "full"),
        )
        result = await orchestrator.run(
            request,
            session_id=body.get("session_id", ""),
            job_id=body.get("job_id", ""),
            user_id=body.get("user_id", ""),
            tenant_id=body.get("tenant_id", ""),
        )
        _runs[result.plan.plan_id] = {
            "plan_id": result.plan.plan_id,
            "stage_count": len(result.plan.stages),
            "success_count": sum(1 for r in result.stage_results.values() if r.success),
        }
        return _runs[result.plan.plan_id]

    @router.get("/runs/{plan_id}")
    async def get_run(plan_id: str) -> dict[str, Any]:
        if plan_id not in _runs:
            raise HTTPException(status_code=404, detail="未找到 run")
        return _runs[plan_id]

    @router.get("/plans/{plan_id}/graph.mermaid")
    async def plan_graph(plan_id: str) -> dict[str, str]:
        if plan_id not in _runs:
            raise HTTPException(status_code=404, detail="未找到 plan")
        return {"plan_id": plan_id, "mermaid": "flowchart TD\n    placeholder[plan graph]"}

    if scheduler is not None:

        @router.get("/scheduler/health")
        async def scheduler_health() -> dict[str, Any]:
            return scheduler.health_summary()

    return router
