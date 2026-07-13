"""调度后端 ABC 与内存实现。"""
from __future__ import annotations

import abc
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import get_config, ConcurrencyPolicy
from ..observability import bind_run_context, log_event, record_metric
from ..audit import emit_audit

logger = logging.getLogger("casa.scheduler")

# ============================================================================
# 调度后端抽象 — 领域项目注入 Redis/PG 实现
# ============================================================================


class SchedulerBackend(abc.ABC):
    """
    调度状态持久化抽象。默认用内存实现；多副本需替换。

    生产级实现契约（Redis/PG）：
      ``submit()`` 中的 ``try_acquire_slot`` → ``save_run`` → ``record_heartbeat``
      三步应在同一原子事务内完成（Lua 脚本 / 行级锁），避免进程崩溃导致槽位泄漏。
      可选覆写 ``atomic_submit_run()`` 由 ``SessionScheduler`` 优先调用。
    """

    @abc.abstractmethod
    def try_acquire_slot(self, session_id: str, slots_needed: int, cap: int) -> tuple[bool, int]:
        """
        原子操作：若 active + slots_needed <= cap，分配槽位。

        返回:
            (acquired, active_after) — acquired 表示是否成功，active_after 是新 active 值
        """
        ...

    @abc.abstractmethod
    def set_active(self, session_id: str, count: int) -> None: ...

    def adjust_active(self, session_id: str, delta: int) -> int:
        """原子调整 active 槽位，返回调整后计数。默认非原子实现。"""
        current = self.get_active(session_id)
        new_count = max(0, current + delta)
        self.set_active(session_id, new_count)
        return new_count

    @abc.abstractmethod
    def list_runs(self, session_id: str, tenant_id: str = "") -> list[str]: ...

    @abc.abstractmethod
    def list_queued_runs(self, session_id: str, tenant_id: str = "") -> list[str]: ...

    @abc.abstractmethod
    def save_run(self, run: RunRecord) -> None: ...

    @abc.abstractmethod
    def get_run(self, run_id: str) -> RunRecord | None: ...

    @abc.abstractmethod
    def mark_status(self, run_id: str, status: str, error: str = "") -> None: ...

    @abc.abstractmethod
    def record_heartbeat(self, run_id: str) -> None: ...

    @abc.abstractmethod
    def find_zombies(self, stale_seconds: float) -> list[str]: ...

    @abc.abstractmethod
    def get_active(self, session_id: str) -> int: ...

    def find_by_idempotency(self, session_id: str, idempotency_key: str) -> RunRecord | None:
        """按幂等键查找活跃 run；自定义 backend 可覆写。"""
        return None

    def health_snapshot(self) -> dict[str, Any]:
        """返回调度器状态快照（供 health_check 使用）。"""
        ...

    def atomic_accept_run(self, run: "RunRecord", cap: int) -> tuple[bool, int] | None:
        """
        原子 accept：``try_acquire_slot`` + ``save_run`` + ``record_heartbeat`` 一步完成。

        生产级 Redis/PG 实现应覆写此方法（Lua / 行级锁）。
        不支持时返回 ``None``，由 ``SessionScheduler`` 回退三步调用。
        """
        return None


# ============================================================================
# 内存后端（单副本）
# ============================================================================


class InMemorySchedulerBackend(SchedulerBackend):
    """进程内内存调度状态（开发/测试/单副本用）。"""

    def __init__(self):
        self._active: dict[str, int] = {}
        self._runs: dict[str, RunRecord] = {}
        self._heartbeats: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_acquire_slot(self, session_id: str, slots_needed: int, cap: int) -> tuple[bool, int]:
        with self._lock:
            active = self._active.get(session_id, 0)
            if active + slots_needed <= cap:
                new_active = active + slots_needed
                self._active[session_id] = new_active
                return True, new_active
            return False, active

    def get_active(self, session_id: str) -> int:
        with self._lock:
            return self._active.get(session_id, 0)

    def set_active(self, session_id: str, count: int) -> None:
        with self._lock:
            if count <= 0:
                self._active.pop(session_id, None)
            else:
                self._active[session_id] = count

    def adjust_active(self, session_id: str, delta: int) -> int:
        with self._lock:
            active = self._active.get(session_id, 0)
            new_active = max(0, active + delta)
            if new_active <= 0:
                self._active.pop(session_id, None)
            else:
                self._active[session_id] = new_active
            return new_active

    def list_runs(self, session_id: str, tenant_id: str = "") -> list[str]:
        with self._lock:
            return [
                r.run_id for r in self._runs.values()
                if r.session_id == session_id
                and (not tenant_id or r.tenant_id == tenant_id)
                and r.status in ("accepted", "queued", "running")
            ]

    def list_queued_runs(self, session_id: str, tenant_id: str = "") -> list[str]:
        """返回仅排队状态的 run_id（FIFO 出队用）。"""
        with self._lock:
            queued = [
                r for r in self._runs.values()
                if r.session_id == session_id
                and (not tenant_id or r.tenant_id == tenant_id)
                and r.status == "queued"
            ]
            queued.sort(key=lambda r: r.created_at)
            return [r.run_id for r in queued]

    def find_by_idempotency(self, session_id: str, idempotency_key: str) -> RunRecord | None:
        if not idempotency_key:
            return None
        with self._lock:
            for r in self._runs.values():
                if (
                    r.session_id == session_id
                    and r.idempotency_key == idempotency_key
                    and r.status in ("accepted", "queued", "running")
                ):
                    return r
        return None

    def save_run(self, run: RunRecord) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def mark_status(self, run_id: str, status: str, error: str = "") -> None:
        with self._lock:
            r = self._runs.get(run_id)
            if r:
                r.status = status
                if error:
                    r.error = error
                # 终态时清理心跳条目，防止内存泄漏和僵尸误报
                if status in ("done", "failed"):
                    self._heartbeats.pop(run_id, None)

    def record_heartbeat(self, run_id: str) -> None:
        with self._lock:
            self._heartbeats[run_id] = time.monotonic()

    def find_zombies(self, stale_seconds: float) -> list[str]:
        with self._lock:
            now = time.monotonic()
            return [
                rid for rid, ts in self._heartbeats.items()
                if now - ts > stale_seconds
                and self._runs.get(rid) is not None
                and self._runs[rid].status in ("accepted", "running")
            ]

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            total_active = sum(self._active.values())
            queued = sum(1 for r in self._runs.values() if r.status == "queued")
            accepted = sum(1 for r in self._runs.values() if r.status == "accepted")
            return {
                "status": "ok",
                "active_sessions": len(self._active),
                "total_active_slots": total_active,
                "queued_runs": queued,
                "accepted_runs": accepted,
                "total_runs": len(self._runs),
            }

    def atomic_accept_run(self, run: RunRecord, cap: int) -> tuple[bool, int] | None:
        with self._lock:
            active = self._active.get(run.session_id, 0)
            if active + run.slots_used > cap:
                return False, active
            new_active = active + run.slots_used
            self._active[run.session_id] = new_active
            self._runs[run.run_id] = run
            self._heartbeats[run.run_id] = time.monotonic()
            return True, new_active


