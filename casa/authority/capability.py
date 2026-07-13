"""CapabilityRow / CapabilityMatrix。"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from .grants import DataGrant, Surface, ToolGrant

logger = logging.getLogger("casa.authority")


@dataclass(kw_only=True)
class CapabilityRow:
    """
    Capability Matrix 中的一行。
    设计新 Agent 时填这张表，一目了然（20 字段完整模板）：

        CapabilityRow(
            # ── 标识 ──
            agent_id="my_analyst",          # [必填] 全局唯一 ID
            display_name="我的分析师",
            surface=Surface.HARNESS,        # dialogue | harness | deterministic（运行时枚举）

            # ── 许可（三重正交校验）──
            tool_ids=["read_artifact"],
            data_read=["raw_data"],
            data_write="my_output",         # 可写产物 kind（须唯一）
            kb_read=["global_kb"],

            # ── 模型 ──
            model_preference="claude-sonnet-4-6",
            context_limit_tokens=128000,

            # ── 角色 + 沙箱 ──
            is_evaluator=False,
            sandbox_memory_mb=512,
            sandbox_network="restricted",   # restricted | none | full（运行时枚举）
            sandbox_filesystem="read_only", # read_only | read_write（运行时枚举）

            # ── Harness 参数 ──
            max_iterations=5,
            task_template="",
            native_fallback=False,
            role="worker",                  # worker | evaluator | coordinator（运行时枚举）
            scope_tags=["analysis"],
            is_required=False,
            priority=100,                   # 数值越小越优先（波次内排序）
        )
    """

    agent_id: str
    display_name: str = ""
    surface: str = Surface.HARNESS
    execution_profile: str = "harness"  # deterministic | structured | harness | report_chapter

    # 许可
    tool_ids: list[str] = field(default_factory=list)
    data_read: list[str] = field(default_factory=list)
    data_write: str = ""
    kb_read: list[str] = field(default_factory=list)

    # 模型偏好
    model_preference: str = ""
    context_limit_tokens: int = 128000

    # 评估者 + 沙箱约束
    is_evaluator: bool = False
    sandbox_memory_mb: int = 512
    sandbox_network: str = "restricted"
    sandbox_filesystem: str = "read_only"

    # Harness 参数
    max_iterations: int = 5
    task_template: str = ""
    native_fallback: bool = False

    # 分类
    role: str = "worker"
    scope_tags: list[str] = field(default_factory=list)
    is_required: bool = False
    # 调度优先级（数值越小越优先，默认 100）
    priority: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "surface": self.surface,
            "execution_profile": self.execution_profile,
            "tool_ids": list(self.tool_ids),
            "data_read": list(self.data_read),
            "data_write": self.data_write,
            "kb_read": list(self.kb_read),
            "model_preference": self.model_preference,
            "context_limit_tokens": self.context_limit_tokens,
            "is_evaluator": self.is_evaluator,
            "sandbox_memory_mb": self.sandbox_memory_mb,
            "sandbox_network": self.sandbox_network,
            "sandbox_filesystem": self.sandbox_filesystem,
            "max_iterations": self.max_iterations,
            "role": self.role,
            "is_required": self.is_required,
            "priority": self.priority,
        }


class CapabilityMatrix:
    """
    Agent 能力矩阵：注册所有 Agent 的工具许可 + 数据许可。

    使用方式：
        matrix = CapabilityMatrix()
        matrix.register(CapabilityRow(
            agent_id="my_analyst",
            surface=Surface.HARNESS,
            tool_ids=["read_artifact"],
            data_read=["raw_data", "theme_analytics"],
            data_write="my_perspective",
        ))

        # 查询
        tools = matrix.tool_ids_for("my_analyst")       # ["read_artifact"]
        data = matrix.data_grants_for("my_analyst")     # (["raw_data", "theme_analytics"], "my_perspective")
    """

    def __init__(self):
        self._rows: dict[str, CapabilityRow] = {}
        self._tool_index: dict[str, set[str]] = {}  # tool_id → {agent_ids}
        self._data_producer_index: dict[str, str] = {}  # artifact_kind → agent_id
        self._lock = threading.RLock()

    def register(self, row: CapabilityRow) -> None:
        with self._lock:
            if row.agent_id in self._rows:
                logger.warning("Agent %r 已在 CapabilityMatrix 中，覆盖", row.agent_id)

            self._rows[row.agent_id] = row

            # 更新索引
            for tid in row.tool_ids:
                self._tool_index.setdefault(tid, set()).add(row.agent_id)

            if row.data_write:
                existing = self._data_producer_index.get(row.data_write)
                if existing and existing != row.agent_id:
                    logger.warning(
                        "Artifact kind %r 有多个 producer: %s, %s",
                        row.data_write, existing, row.agent_id,
                    )
                self._data_producer_index[row.data_write] = row.agent_id

    def deregister(self, agent_id: str) -> None:
        """移除 Agent（软删除）。"""
        with self._lock:
            row = self._rows.pop(agent_id, None)
            if row:
                for tid in row.tool_ids:
                    agents = self._tool_index.get(tid, set())
                    agents.discard(agent_id)
                    if not agents:
                        del self._tool_index[tid]
                if row.data_write:
                    self._data_producer_index.pop(row.data_write, None)

    # --- 查询 ---
    def get(self, agent_id: str) -> CapabilityRow | None:
        with self._lock:
            return self._rows.get(agent_id)

    def tool_ids_for(self, agent_id: str) -> list[str]:
        with self._lock:
            row = self._rows.get(agent_id)
            return list(row.tool_ids) if row else []

    def data_grants_for(self, agent_id: str) -> tuple[list[str], str, list[str]]:
        with self._lock:
            row = self._rows.get(agent_id)
            if row:
                return (list(row.data_read), row.data_write, list(row.kb_read))
            return ([], "", [])

    def producer_of(self, artifact_kind: str) -> str:
        with self._lock:
            return self._data_producer_index.get(artifact_kind, "")

    def all_agent_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._rows.keys())

    def list_rows(self) -> list[CapabilityRow]:
        with self._lock:
            return list(self._rows.values())

    def to_rows(self) -> list[CapabilityRow]:
        """list_rows() 的别名 — 供 IntentRouter 工厂方法使用。"""
        return self.list_rows()

    # --- 许可校验 ---
    def check_tool_grant(self, agent_id: str, tool_id: str) -> tuple[bool, str]:
        """校验工具许可。"""
        row = self._rows.get(agent_id)
        if not row:
            return (False, f"Agent {agent_id} 未注册")
        if tool_id not in row.tool_ids:
            return (False, f"工具 {tool_id} 未授权给 {agent_id}")
        return (True, "")

    def check_data_read_grant(self, agent_id: str, artifact_kind: str) -> tuple[bool, str]:
        """校验数据读许可。"""
        row = self._rows.get(agent_id)
        if not row:
            return (False, f"Agent {agent_id} 未注册")
        if artifact_kind not in row.data_read:
            return (False, f"数据 {artifact_kind} 不在 {agent_id} 的 read 白名单中")
        return (True, "")

    def check_data_write_grant(self, agent_id: str, artifact_kind: str) -> tuple[bool, str]:
        """校验数据写许可。单 Writer 原则。"""
        row = self._rows.get(agent_id)
        if not row:
            return (False, f"Agent {agent_id} 未注册")
        if row.data_write != artifact_kind:
            return (False, f"数据（写）许可拒绝: {artifact_kind}（允许: {row.data_write}）")
        return (True, "")

    def to_table(self) -> list[dict[str, Any]]:
        """导出为 Markdown 表格可用的行列表。"""
        return [
            {
                "agent_id": r.agent_id,
                "display": r.display_name,
                "surface": r.surface,
                "tools": len(r.tool_ids),
                "data_read": len(r.data_read),
                "data_write": r.data_write,
                "role": r.role,
            }
            for r in self._rows.values()
        ]

