"""GrantStore 持久化。"""
from __future__ import annotations

import abc
import logging
import threading
from typing import Any

from .grants import DataGrant, ToolGrant, Surface

logger = logging.getLogger("casa.authority")

class GrantStore(abc.ABC):
    """许可持久化抽象。领域项目可实现为 PG / JSON 文件 / Redis。"""

    @abc.abstractmethod
    def load_tool_grants(self, agent_id: str) -> list[ToolGrant]: ...

    @abc.abstractmethod
    def load_data_grants(self, agent_id: str) -> DataGrant | None: ...

    @abc.abstractmethod
    def save_tool_grant(self, grant: ToolGrant) -> None: ...

    @abc.abstractmethod
    def save_data_grant(self, grant: DataGrant) -> None: ...

    @abc.abstractmethod
    def delete_tool_grant(self, agent_id: str, tool_id: str) -> None: ...

    @abc.abstractmethod
    def list_all_agents(self) -> list[str]: ...

    def has_tool_grant_config(self, agent_id: str) -> bool:
        """是否已在 store 中配置过 tool grants（含显式空列表）。"""
        return False

    def has_data_grant_config(self, agent_id: str) -> bool:
        """是否已在 store 中配置过 data grant。"""
        return False


class InMemoryGrantStore(GrantStore):
    """内存许可存储（开发/测试用）。"""

    def __init__(self):
        self._tool_grants: dict[str, dict[str, ToolGrant]] = {}
        self._data_grants: dict[str, DataGrant] = {}
        self._lock = threading.Lock()

    def load_tool_grants(self, agent_id: str) -> list[ToolGrant]:
        with self._lock:
            return list(self._tool_grants.get(agent_id, {}).values())

    def load_data_grants(self, agent_id: str) -> DataGrant | None:
        with self._lock:
            return self._data_grants.get(agent_id)

    def save_tool_grant(self, grant: ToolGrant) -> None:
        with self._lock:
            self._tool_grants.setdefault(grant.agent_id, {})[grant.tool_id] = grant

    def save_data_grant(self, grant: DataGrant) -> None:
        with self._lock:
            self._data_grants[grant.agent_id] = grant

    def delete_tool_grant(self, agent_id: str, tool_id: str) -> None:
        with self._lock:
            grants = self._tool_grants.get(agent_id)
            if grants is None:
                return
            grants.pop(tool_id, None)
            if not grants:
                self._tool_grants.pop(agent_id, None)

    def list_all_agents(self) -> list[str]:
        with self._lock:
            agents = set(self._tool_grants.keys()) | set(self._data_grants.keys())
            return sorted(agents)

    def has_tool_grant_config(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._tool_grants

    def has_data_grant_config(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._data_grants


class PgGrantStore(GrantStore):
    """
    PostgreSQL 许可持久化参考实现。

    Schema 见 ``casa/schemas/grants_pg.sql``。
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None

    def _ensure_pool(self):
        if self._pool is not None:
            return
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise ImportError(
                "PgGrantStore 需要 psycopg：pip install 'casa-frame[postgres]'"
            ) from exc
        self._pool = ConnectionPool(self._dsn, min_size=1, max_size=4, open=True)

    def load_tool_grants(self, agent_id: str) -> list[ToolGrant]:
        import json
        self._ensure_pool()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT tool_id, surface, adapter, enabled, config_json "
                "FROM grant_tools WHERE agent_id = %s",
                (agent_id,),
            ).fetchall()
        grants: list[ToolGrant] = []
        for tool_id, surface, adapter, enabled, config_json in rows:
            cfg = json.loads(config_json) if config_json else {}
            grants.append(ToolGrant(
                tool_id=tool_id,
                agent_id=agent_id,
                surface=surface or Surface.HARNESS,
                adapter=adapter or "native",
                enabled=bool(enabled),
                default_config=cfg,
            ))
        return grants

    def load_data_grants(self, agent_id: str) -> DataGrant | None:
        import json
        self._ensure_pool()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT read_artifacts, write_artifact FROM grant_data WHERE agent_id = %s",
                (agent_id,),
            ).fetchone()
        if not row:
            return None
        reads = row[0]
        if isinstance(reads, str):
            reads = json.loads(reads)
        return DataGrant(
            agent_id=agent_id,
            read_artifacts=list(reads or []),
            write_artifact=row[1] or "",
        )

    def save_tool_grant(self, grant: ToolGrant) -> None:
        import json
        self._ensure_pool()
        with self._pool.connection() as conn:
            with conn.transaction():
                try:
                    conn.execute(
                        "INSERT INTO grant_tool_agents (agent_id) VALUES (%s) "
                        "ON CONFLICT (agent_id) DO NOTHING",
                        (grant.agent_id,),
                    )
                except Exception as exc:
                    logger.debug(
                        "grant_tool_agents unavailable, legacy PG schema: %s", exc,
                    )
                conn.execute(
                    "INSERT INTO grant_tools "
                    "(agent_id, tool_id, surface, adapter, enabled, config_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (agent_id, tool_id) DO UPDATE SET "
                    "surface = EXCLUDED.surface, adapter = EXCLUDED.adapter, "
                    "enabled = EXCLUDED.enabled, config_json = EXCLUDED.config_json",
                    (
                        grant.agent_id, grant.tool_id, grant.surface, grant.adapter,
                        grant.enabled, json.dumps(grant.default_config),
                    ),
                )

    def save_data_grant(self, grant: DataGrant) -> None:
        import json
        self._ensure_pool()
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO grant_data (agent_id, read_artifacts, write_artifact) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (agent_id) DO UPDATE SET "
                    "read_artifacts = EXCLUDED.read_artifacts, "
                    "write_artifact = EXCLUDED.write_artifact",
                    (
                        grant.agent_id,
                        json.dumps(list(grant.read_artifacts)),
                        grant.write_artifact,
                    ),
                )

    def delete_tool_grant(self, agent_id: str, tool_id: str) -> None:
        self._ensure_pool()
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM grant_tools WHERE agent_id = %s AND tool_id = %s",
                    (agent_id, tool_id),
                )
                remaining = conn.execute(
                    "SELECT 1 FROM grant_tools WHERE agent_id = %s LIMIT 1",
                    (agent_id,),
                ).fetchone()
                if not remaining:
                    try:
                        conn.execute(
                            "DELETE FROM grant_tool_agents WHERE agent_id = %s",
                            (agent_id,),
                        )
                    except Exception as exc:
                        logger.debug("grant_tool_agents cleanup skipped: %s", exc)

    def list_all_agents(self) -> list[str]:
        self._ensure_pool()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT agent_id FROM grant_tools "
                "UNION SELECT agent_id FROM grant_data",
            ).fetchall()
        return sorted({r[0] for r in rows})

    def has_tool_grant_config(self, agent_id: str) -> bool:
        self._ensure_pool()
        with self._pool.connection() as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM grant_tool_agents WHERE agent_id = %s "
                    "UNION SELECT 1 FROM grant_tools WHERE agent_id = %s LIMIT 1",
                    (agent_id, agent_id),
                ).fetchone()
            except Exception as exc:
                logger.debug("grant_tool_agents query failed, fallback: %s", exc)
                row = conn.execute(
                    "SELECT 1 FROM grant_tools WHERE agent_id = %s LIMIT 1",
                    (agent_id,),
                ).fetchone()
        return row is not None

    def has_data_grant_config(self, agent_id: str) -> bool:
        self._ensure_pool()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM grant_data WHERE agent_id = %s LIMIT 1",
                (agent_id,),
            ).fetchone()
        return row is not None

