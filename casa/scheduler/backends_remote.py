"""Redis / PG 调度后端。"""
from __future__ import annotations

import abc
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import get_config
from ..observability import log_event
from .backend import SchedulerBackend

logger = logging.getLogger("casa.scheduler")

# ============================================================================
# Redis 后端（多副本参考实现 — 需 redis 包）
# ============================================================================

_REDIS_ACCEPT_LUA = """
local active_key = KEYS[1]
local runs_key = KEYS[2]
local hb_key = KEYS[3]
local cap = tonumber(ARGV[1])
local slots = tonumber(ARGV[2])
local run_id = ARGV[3]
local run_json = ARGV[4]
local now = tonumber(ARGV[5])
local active = tonumber(redis.call('GET', active_key) or '0')
if active + slots > cap then
  return {0, active}
end
redis.call('SET', active_key, active + slots)
redis.call('HSET', runs_key, run_id, run_json)
redis.call('ZADD', hb_key, now, run_id)
return {1, active + slots}
"""


class RedisSchedulerBackend(SchedulerBackend):
    """
    Redis 调度状态后端（Lua 原子 accept 参考实现）。

    需 ``pip install redis`` 且可连接的 Redis 实例。
    """

    def __init__(self, redis_url: str, *, key_prefix: str = "casa:sched"):
        try:
            import redis
        except ImportError as exc:
            raise ImportError(
                "RedisSchedulerBackend 需要 redis 包：pip install 'casa-frame[redis]'"
            ) from exc
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = key_prefix
        self._accept_script = self._redis.register_script(_REDIS_ACCEPT_LUA)

    def _active_key(self, session_id: str) -> str:
        return f"{self._prefix}:active:{session_id}"

    def _runs_key(self) -> str:
        return f"{self._prefix}:runs"

    def _hb_key(self) -> str:
        return f"{self._prefix}:hb"

    def _run_from_json(self, data: str) -> RunRecord | None:
        import json
        try:
            d = json.loads(data)
            return RunRecord(**d)
        except (json.JSONDecodeError, TypeError):
            return None

    def _run_to_json(self, run: RunRecord) -> str:
        import json
        return json.dumps(run.to_dict())

    def try_acquire_slot(self, session_id: str, slots_needed: int, cap: int) -> tuple[bool, int]:
        active = int(self._redis.get(self._active_key(session_id)) or 0)
        if active + slots_needed <= cap:
            new_active = active + slots_needed
            self._redis.set(self._active_key(session_id), new_active)
            return True, new_active
        return False, active

    def set_active(self, session_id: str, count: int) -> None:
        key = self._active_key(session_id)
        if count <= 0:
            self._redis.delete(key)
        else:
            self._redis.set(key, count)

    def get_active(self, session_id: str) -> int:
        return int(self._redis.get(self._active_key(session_id)) or 0)

    def save_run(self, run: RunRecord) -> None:
        self._redis.hset(self._runs_key(), run.run_id, self._run_to_json(run))

    def get_run(self, run_id: str) -> RunRecord | None:
        data = self._redis.hget(self._runs_key(), run_id)
        return self._run_from_json(data) if data else None

    def mark_status(self, run_id: str, status: str, error: str = "") -> None:
        run = self.get_run(run_id)
        if not run:
            return
        run.status = status
        if error:
            run.error = error
        self.save_run(run)
        if status in ("done", "failed"):
            self._redis.zrem(self._hb_key(), run_id)

    def record_heartbeat(self, run_id: str) -> None:
        self._redis.zadd(self._hb_key(), {run_id: time.time()})

    def list_runs(self, session_id: str, tenant_id: str = "") -> list[str]:
        return [
            rid for rid, run in self._all_runs()
            if run.session_id == session_id
            and (not tenant_id or run.tenant_id == tenant_id)
            and run.status in ("accepted", "queued", "running")
        ]

    def list_queued_runs(self, session_id: str, tenant_id: str = "") -> list[str]:
        queued = [
            run for _, run in self._all_runs()
            if run.session_id == session_id
            and (not tenant_id or run.tenant_id == tenant_id)
            and run.status == "queued"
        ]
        queued.sort(key=lambda r: r.created_at)
        return [r.run_id for r in queued]

    def find_by_idempotency(self, session_id: str, idempotency_key: str) -> RunRecord | None:
        if not idempotency_key:
            return None
        for _, run in self._all_runs():
            if (
                run.session_id == session_id
                and run.idempotency_key == idempotency_key
                and run.status in ("accepted", "queued", "running")
            ):
                return run
        return None

    def find_zombies(self, stale_seconds: float) -> list[str]:
        cutoff = time.time() - stale_seconds
        candidates = self._redis.zrangebyscore(self._hb_key(), "-inf", cutoff)
        zombies: list[str] = []
        for rid in candidates:
            run = self.get_run(rid)
            if run and run.status in ("accepted", "running"):
                zombies.append(rid)
        return zombies

    def health_snapshot(self) -> dict[str, Any]:
        runs = list(self._all_runs())
        queued = sum(1 for _, r in runs if r.status == "queued")
        accepted = sum(1 for _, r in runs if r.status == "accepted")
        keys = self._redis.keys(f"{self._prefix}:active:*")
        total_active = sum(int(self._redis.get(k) or 0) for k in keys)
        return {
            "status": "ok",
            "backend": "redis",
            "active_sessions": len(keys),
            "total_active_slots": total_active,
            "queued_runs": queued,
            "accepted_runs": accepted,
            "total_runs": len(runs),
        }

    def atomic_accept_run(self, run: RunRecord, cap: int) -> tuple[bool, int] | None:
        result = self._accept_script(
            keys=[self._active_key(run.session_id), self._runs_key(), self._hb_key()],
            args=[cap, run.slots_used, run.run_id, self._run_to_json(run), time.time()],
        )
        acquired = bool(int(result[0]))
        active_after = int(result[1])
        return acquired, active_after

    def _all_runs(self) -> list[tuple[str, RunRecord]]:
        raw = self._redis.hgetall(self._runs_key())
        out: list[tuple[str, RunRecord]] = []
        for rid, data in raw.items():
            run = self._run_from_json(data)
            if run:
                out.append((rid, run))
        return out


# ============================================================================
# PostgreSQL 后端（多副本参考实现 — 需 psycopg）
# ============================================================================


class PgSchedulerBackend(SchedulerBackend):
    """
    PostgreSQL 调度后端——``SELECT ... FOR UPDATE`` 行级锁版 atomic_accept_run。

    用法::

        backend = PgSchedulerBackend("postgresql://user:pass@host/db")
        sched = SessionScheduler(backend=backend)

    Schema 见 ``casa/schemas/scheduler_pg.sql``。
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
                "PgSchedulerBackend 需要 psycopg：pip install 'casa-frame[postgres]'"
            ) from exc
        self._pool = ConnectionPool(self._dsn, min_size=1, max_size=8, open=True)

    def _run_to_row(self, run: RunRecord) -> dict[str, Any]:
        return run.to_dict()

    def _row_to_run(self, row: dict[str, Any]) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            session_id=row["session_id"],
            user_id=row.get("user_id") or "",
            tenant_id=row.get("tenant_id") or "",
            intent=row.get("intent") or "",
            status=row.get("status") or "pending",
            error=row.get("error") or "",
            idempotency_key=row.get("idempotency_key") or "",
            slots_used=int(row.get("slots_used") or 1),
            created_at=row.get("created_at") or "",
        )

    def atomic_accept_run(self, run: RunRecord, cap: int) -> tuple[bool, int] | None:
        self._ensure_pool()
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT active_slots FROM scheduler_sessions "
                    "WHERE session_id = %s FOR UPDATE",
                    (run.session_id,),
                ).fetchone()
                current = int(row[0]) if row else 0
                if current + run.slots_used > cap:
                    return False, current
                new_active = current + run.slots_used
                conn.execute(
                    "INSERT INTO scheduler_sessions (session_id, active_slots) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET active_slots = EXCLUDED.active_slots",
                    (run.session_id, new_active),
                )
                conn.execute(
                    "INSERT INTO scheduler_runs "
                    "(run_id, session_id, user_id, tenant_id, intent, status, "
                    "slots_used, idempotency_key, created_at, last_heartbeat) "
                    "VALUES (%s, %s, %s, %s, %s, 'accepted', %s, %s, %s, %s) "
                    "ON CONFLICT (run_id) DO UPDATE SET "
                    "status = EXCLUDED.status, last_heartbeat = EXCLUDED.last_heartbeat",
                    (
                        run.run_id, run.session_id, run.user_id, run.tenant_id,
                        run.intent, run.slots_used, run.idempotency_key,
                        run.created_at, time.time(),
                    ),
                )
                return True, new_active

    def try_acquire_slot(self, session_id: str, slots_needed: int, cap: int) -> tuple[bool, int]:
        self._ensure_pool()
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT active_slots FROM scheduler_sessions "
                    "WHERE session_id = %s FOR UPDATE",
                    (session_id,),
                ).fetchone()
                current = int(row[0]) if row else 0
                if current + slots_needed > cap:
                    return False, current
                new_active = current + slots_needed
                conn.execute(
                    "INSERT INTO scheduler_sessions (session_id, active_slots) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET active_slots = EXCLUDED.active_slots",
                    (session_id, new_active),
                )
                return True, new_active

    def set_active(self, session_id: str, count: int) -> None:
        self._ensure_pool()
        with self._pool.connection() as conn:
            if count <= 0:
                conn.execute(
                    "DELETE FROM scheduler_sessions WHERE session_id = %s",
                    (session_id,),
                )
            else:
                conn.execute(
                    "INSERT INTO scheduler_sessions (session_id, active_slots) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET active_slots = EXCLUDED.active_slots",
                    (session_id, count),
                )

    def get_active(self, session_id: str) -> int:
        self._ensure_pool()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT active_slots FROM scheduler_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            return int(row[0]) if row else 0

    def save_run(self, run: RunRecord) -> None:
        self._ensure_pool()
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO scheduler_runs "
                "(run_id, session_id, user_id, tenant_id, intent, status, error, "
                "slots_used, idempotency_key, created_at, last_heartbeat) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET "
                "status = EXCLUDED.status, error = EXCLUDED.error, "
                "last_heartbeat = EXCLUDED.last_heartbeat",
                (
                    run.run_id, run.session_id, run.user_id, run.tenant_id,
                    run.intent, run.status, run.error, run.slots_used,
                    run.idempotency_key, run.created_at, time.time(),
                ),
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        self._ensure_pool()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT run_id, session_id, user_id, tenant_id, intent, status, "
                "error, slots_used, idempotency_key, created_at "
                "FROM scheduler_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            cols = [
                "run_id", "session_id", "user_id", "tenant_id", "intent",
                "status", "error", "slots_used", "idempotency_key", "created_at",
            ]
            return self._row_to_run(dict(zip(cols, row)))

    def mark_status(self, run_id: str, status: str, error: str = "") -> None:
        self._ensure_pool()
        with self._pool.connection() as conn:
            if status in ("done", "failed"):
                conn.execute(
                    "UPDATE scheduler_runs SET status = %s, error = %s, last_heartbeat = 0 "
                    "WHERE run_id = %s",
                    (status, error, run_id),
                )
            else:
                conn.execute(
                    "UPDATE scheduler_runs SET status = %s, error = %s, last_heartbeat = %s "
                    "WHERE run_id = %s",
                    (status, error, time.time(), run_id),
                )

    def record_heartbeat(self, run_id: str) -> None:
        self._ensure_pool()
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE scheduler_runs SET last_heartbeat = %s WHERE run_id = %s",
                (time.time(), run_id),
            )

    def list_runs(self, session_id: str, tenant_id: str = "") -> list[str]:
        self._ensure_pool()
        with self._pool.connection() as conn:
            if tenant_id:
                rows = conn.execute(
                    "SELECT run_id FROM scheduler_runs "
                    "WHERE session_id = %s AND tenant_id = %s "
                    "AND status IN ('accepted', 'queued', 'running')",
                    (session_id, tenant_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id FROM scheduler_runs "
                    "WHERE session_id = %s AND status IN ('accepted', 'queued', 'running')",
                    (session_id,),
                ).fetchall()
            return [r[0] for r in rows]

    def list_queued_runs(self, session_id: str, tenant_id: str = "") -> list[str]:
        self._ensure_pool()
        with self._pool.connection() as conn:
            if tenant_id:
                rows = conn.execute(
                    "SELECT run_id FROM scheduler_runs "
                    "WHERE session_id = %s AND tenant_id = %s AND status = 'queued' "
                    "ORDER BY created_at",
                    (session_id, tenant_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id FROM scheduler_runs "
                    "WHERE session_id = %s AND status = 'queued' ORDER BY created_at",
                    (session_id,),
                ).fetchall()
            return [r[0] for r in rows]

    def find_by_idempotency(self, session_id: str, idempotency_key: str) -> RunRecord | None:
        if not idempotency_key:
            return None
        self._ensure_pool()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT run_id, session_id, user_id, tenant_id, intent, status, "
                "error, slots_used, idempotency_key, created_at "
                "FROM scheduler_runs "
                "WHERE session_id = %s AND idempotency_key = %s "
                "AND status IN ('accepted', 'queued', 'running') LIMIT 1",
                (session_id, idempotency_key),
            ).fetchone()
            if not row:
                return None
            cols = [
                "run_id", "session_id", "user_id", "tenant_id", "intent",
                "status", "error", "slots_used", "idempotency_key", "created_at",
            ]
            return self._row_to_run(dict(zip(cols, row)))

    def find_zombies(self, stale_seconds: float) -> list[str]:
        self._ensure_pool()
        cutoff = time.time() - stale_seconds
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT run_id FROM scheduler_runs "
                "WHERE status IN ('accepted', 'running') "
                "AND last_heartbeat > 0 AND last_heartbeat < %s",
                (cutoff,),
            ).fetchall()
            return [r[0] for r in rows]

    def health_snapshot(self) -> dict[str, Any]:
        self._ensure_pool()
        with self._pool.connection() as conn:
            active_row = conn.execute(
                "SELECT COALESCE(SUM(active_slots), 0), COUNT(*) FROM scheduler_sessions",
            ).fetchone()
            total_active = int(active_row[0]) if active_row else 0
            active_sessions = int(active_row[1]) if active_row else 0
            counts = conn.execute(
                "SELECT status, COUNT(*) FROM scheduler_runs GROUP BY status",
            ).fetchall()
            by_status = {r[0]: int(r[1]) for r in counts}
            return {
                "status": "ok",
                "backend": "postgresql",
                "active_sessions": active_sessions,
                "total_active_slots": total_active,
                "queued_runs": by_status.get("queued", 0),
                "accepted_runs": by_status.get("accepted", 0),
                "total_runs": sum(by_status.values()),
            }


