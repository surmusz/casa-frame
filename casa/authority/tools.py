"""ToolContext / ToolRegistry。"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .grants import Surface

logger = logging.getLogger("casa.authority")

class ToolContext:
    """工具运行时上下文，携带 agent 身份与数据许可。"""

    surface: str = Surface.HARNESS
    agent_id: str = ""
    job_id: str = ""
    session_id: str = ""
    user_id: str = ""
    plan_id: str = ""

    # 运行时注入
    artifact_store: Any | None = None
    session: dict[str, Any] | None = None

    # 数据许可（由 AuthorityResolver 在 stage 启动时注入）
    data_grants_read: list[str] = field(default_factory=list)
    data_grants_write: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "agent_id": self.agent_id,
            "job_id": self.job_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "plan_id": self.plan_id,
        }


# ============================================================================
# 工具接口 — 领域项目实现
# ============================================================================


class ToolHandler(abc.ABC):
    """工具处理器接口。领域项目实现具体工具逻辑。"""

    tool_id: str = ""

    @abc.abstractmethod
    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any: ...


class ToolRegistry:
    """
    工具注册表。

    使用方式：
        reg = ToolRegistry()
        reg.register(ReadArtifactTool())
        reg.register(SearchKnowledgeTool())

        handler = reg.get("read_artifact")
        result = await handler.execute(ctx, {"ref_id": "..."})
    """

    def __init__(self):
        self._tools: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        self._tools[handler.tool_id] = handler

    def get(self, tool_id: str) -> ToolHandler | None:
        return self._tools.get(tool_id)

    def list_ids(self) -> list[str]:
        return sorted(self._tools.keys())
