"""RefCatalog。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Scope
from .ref import RefID, parse_ref

logger = logging.getLogger("casa.scope")

# ============================================================================
# 当前可见 ref 列表（RefCatalog）
# ============================================================================


@dataclass
class RefCatalog:
    """
    当前会话或 run 的可见 ref 列表。
    供 Agent 发现数据，而非硬编码路径。

    使用方式：
        cat = RefCatalog.build(
            session_id="s001",
            job_id="j001",
            artifact_store=store,
            agent_id="my_worker",
        )
        for entry in cat.refs:
            print(entry["ref_id"], entry["kind"])
    """

    session_id: str = ""
    job_id: str = ""
    refs: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        job_id: str = "",
        artifact_store: Any | None = None,
        agent_id: str = "",
        data_grants_read: list[str] | None = None,
        intake_fields: list[str] | None = None,
        kb_registry: Any | None = None,
        kb_read: list[str] | None = None,
    ) -> RefCatalog:
        """构建当前上下文可见的 ref 列表。

        参数:
            intake_fields: session intake 字段列表（领域项目定义，如 ["subject_ids", "mode"]）。
                           不传则跳过 intake ref。
        """
        catalog = cls(session_id=session_id, job_id=job_id)
        refs: list[dict[str, str]] = []

        # session 域
        if session_id:
            refs.append({
                "ref_id": f"session:{session_id}:doc:brief",
                "kind": "session_brief",
                "scope": Scope.SESSION,
            })
            for field in (intake_fields or []):
                refs.append({
                    "ref_id": f"session:{session_id}:intake:{field}",
                    "kind": f"intake_{field}",
                    "scope": Scope.SESSION,
                })

        # job scope（受数据许可约束）
        if job_id and artifact_store:
            allowed: set[str] | None = (
                set(data_grants_read) if data_grants_read is not None else None
            )
            for kind in artifact_store.list_artifacts():
                if allowed is None or kind in allowed:
                    refs.append({
                        "ref_id": f"job:{job_id}:artifact:{kind}",
                        "kind": kind,
                        "scope": Scope.JOB,
                    })

        # 知识库（动态）
        from ..knowledge import get_kb_registry
        registry = kb_registry or get_kb_registry()
        refs.extend(registry.list_ref_entries(agent_id, kb_read=kb_read))

        catalog.refs = refs
        return catalog

    def to_list(self) -> list[dict[str, str]]:
        return list(self.refs)

    def ref_ids(self) -> list[str]:
        return [r["ref_id"] for r in self.refs]

    def find_by_kind(self, kind: str) -> list[dict[str, str]]:
        return [r for r in self.refs if r.get("kind") == kind]

    def find_by_scope(self, scope: str) -> list[dict[str, str]]:
        return [r for r in self.refs if r.get("scope") == scope]
