"""AuthorityResolver。"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .capability import CapabilityMatrix
from .grants import DataGrant, ToolGrant
from .store import GrantStore, InMemoryGrantStore

logger = logging.getLogger("casa.authority")

class AuthorityResolver:
    """
    许可解析器：合并 Code Default + DB Override。

    Effective Spec 原则：
      - 原生 Agent 有代码默认值（CapabilityMatrix 中的注册）
      - DB/外部 GrantStore 可覆盖 I/O 而不改代码
      - 授权变更通过 invalidate_cache 热更新

    Grant 空值语义（tool / data 一致）：
      - 从未写入 store → 使用 CapabilityMatrix 代码默认
      - store 中已有配置标记（has_*_grant_config 为 true）→ 以 DB 为准，
        含显式空列表（read=[]、tools 无 enabled 项）
      - delete_tool_grant 删光最后一个 tool → 清除配置标记，回退代码默认
        （与「显式空 tool 列表」不同；后者需保留 agent 配置标记）
      - save_data_grant 写入空字段 → 显式拒绝对应 artifact 读写

    使用方式：
        resolver = AuthorityResolver(matrix=matrix, grant_store=store)
        tools = resolver.resolve_tools("my_analyst")
        data = resolver.resolve_data_grants("my_analyst")
    """

    def __init__(
        self,
        *,
        matrix: CapabilityMatrix | None = None,
        grant_store: GrantStore | None = None,
    ):
        self.matrix = matrix or CapabilityMatrix()
        self.grant_store = grant_store or InMemoryGrantStore()
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def resolve_tools(self, agent_id: str) -> list[str]:
        """返回 Agent 的最终 tool_id 列表（code + DB merge）。"""
        with self._lock:
            cached = self._cache.get(agent_id, {}).get("tools")
            if cached is not None:
                return list(cached)  # 防御性拷贝

        # 代码默认 → DB 覆盖
        code_tools = set(self.matrix.tool_ids_for(agent_id))
        db_grants = self.grant_store.load_tool_grants(agent_id)
        db_tools = {g.tool_id for g in db_grants if g.enabled}

        if self.grant_store.has_tool_grant_config(agent_id):
            resolved = list(db_tools)
        else:
            resolved = list(code_tools)

        with self._lock:
            # 二次检查：其他线程可能已解析
            existing = self._cache.get(agent_id, {}).get("tools")
            if existing is not None:
                return list(existing)
            self._cache.setdefault(agent_id, {})["tools"] = resolved
        return resolved

    def resolve_data_grants(self, agent_id: str) -> dict[str, Any]:
        """返回 {read: [...], write: str}。"""
        with self._lock:
            cached = self._cache.get(agent_id, {}).get("data")
            if cached is not None:
                return {
                    "read": list(cached["read"]),
                    "write": cached["write"],
                    "kb_read": list(cached.get("kb_read", [])),
                }

        code_read, code_write, code_kb = self.matrix.data_grants_for(agent_id)
        db_grant = self.grant_store.load_data_grants(agent_id)

        if self.grant_store.has_data_grant_config(agent_id) and db_grant is not None:
            resolved = {
                "read": list(db_grant.read_artifacts),
                "write": db_grant.write_artifact,
                "kb_read": list(code_kb),
            }
        else:
            resolved = {"read": list(code_read), "write": code_write, "kb_read": list(code_kb)}

        with self._lock:
            # 二次检查：其他线程可能已解析
            existing = self._cache.get(agent_id, {}).get("data")
            if existing is not None:
                return {"read": list(existing["read"]), "write": existing["write"], "kb_read": list(existing.get("kb_read", []))}
            self._cache.setdefault(agent_id, {})["data"] = resolved
        return resolved

    def invalidate_cache(self, agent_id: str | None = None) -> None:
        """热更新：使缓存失效。"""
        with self._lock:
            if agent_id:
                self._cache.pop(agent_id, None)
            else:
                self._cache.clear()
        logger.info("Authority cache invalidated: %s", agent_id or "all")

    # --- 统一校验入口 ---
    def check_access(
        self,
        agent_id: str,
        *,
        tool_id: str | None = None,
        artifact_read: str | None = None,
        artifact_write: str | None = None,
        kb_id: str | None = None,
    ) -> tuple[bool, str]:
        """
        统一权限校验：tool × data 交集。

        返回:
            (allowed, error_message)
        """
        if tool_id:
            tools = self.resolve_tools(agent_id)
            if tool_id not in tools:
                return (False, f"工具许可拒绝: {tool_id}")

        data = self.resolve_data_grants(agent_id)

        if artifact_read:
            if artifact_read not in data.get("read", []):
                return (False, f"数据读许可拒绝: {artifact_read}")

        if artifact_write:
            if data.get("write") != artifact_write:
                return (False, f"数据写许可拒绝: {artifact_write}")

        if kb_id:
            kb_read = data.get("kb_read")
            if kb_read is not None and kb_id not in kb_read:
                return (False, f"KB 读许可拒绝: {kb_id}")

        return (True, "")

