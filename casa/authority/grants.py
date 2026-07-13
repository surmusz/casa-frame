"""Surface 与 Tool/Data Grant。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("casa.authority")

# ============================================================================
# 工具运行的上下文平面（Surface）
# ============================================================================


class Surface:
    """工具运行的上下文平面，隔离对话与 Worker 工具的授权。"""

    DIALOGUE = "dialogue"
    HARNESS = "harness"
    DETERMINISTIC = "deterministic"

    ALL = frozenset({DIALOGUE, HARNESS, DETERMINISTIC})

    @classmethod
    def labels(cls) -> dict[str, str]:
        return {
            cls.DIALOGUE: "对话平面",
            cls.HARNESS: "Harness 工具循环",
            cls.DETERMINISTIC: "确定性执行",
        }


# ============================================================================
# 工具许可（ToolGrant）
# ============================================================================


@dataclass(kw_only=True)
class ToolGrant:
    """一个工具授权记录。"""

    tool_id: str
    agent_id: str
    surface: str = Surface.HARNESS
    implementation_key: str = ""
    adapter: str = "native"  # native | mcp
    default_config: dict[str, Any] = field(default_factory=dict)
    connection_config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "agent_id": self.agent_id,
            "surface": self.surface,
            "adapter": self.adapter,
            "enabled": self.enabled,
        }


# ============================================================================
# 数据许可（DataGrant）
# ============================================================================


@dataclass(kw_only=True)
class DataGrant:
    """一个 Agent 的数据许可记录。"""

    agent_id: str
    read_artifacts: list[str] = field(default_factory=list)
    write_artifact: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "read": list(self.read_artifacts),
            "write": self.write_artifact or None,
        }

