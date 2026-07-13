"""会话调度器与模块级 API。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..config import get_config, ConcurrencyPolicy
from ..observability import bind_run_context, log_event, record_metric
from ..audit import emit_audit
from .backend import InMemorySchedulerBackend, SchedulerBackend

logger = logging.getLogger("casa.scheduler")

# ============================================================================
# 调度器实例（SessionScheduler）
# ============================================================================


@dataclass(kw_only=True)
class RunRecord:
    """一条 Run 记录。"""

    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex}")
    session_id: str
    user_id: str = ""
    tenant_id: str = ""
    intent: str = ""
    status: str = "pending"  # pending | accepted | queued | running | done | failed
    error: str = ""
    idempotency_key: str = ""
    slots_used: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "intent": self.intent,
            "status": self.status,
            "error": self.error,
            "idempotency_key": self.idempotency_key,
            "slots_used": self.slots_used,
            "created_at": self.created_at,
        }


@dataclass(kw_only=True)
class SubmitResult:
    """Run 提交结果。"""

    status: str  # accepted | queued | rejected
    run: RunRecord | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.run:
            d["run"] = self.run.to_dict()
        return d


class SessionScheduler:
    """
    会话级并发调度器。

    使用方式：
        sched = SessionScheduler(backend=InMemorySchedulerBackend())
        result = sched.submit("session_1", intent="full_report")
        sched.heartbeat("run_abc123")
        sched.release("session_1", "run_abc123")
    """

    def __init__(
        self,
        *,
        backend: SchedulerBackend | None = None,
        zombie_timeout_seconds: float = 1800.0,  # 30 min
        dispatch_callback: Callable[[str, str], None] | None = None,
    ):
        self._backend = backend or InMemorySchedulerBackend()
        self._zombie_timeout = zombie_timeout_seconds
        self._dispatch_callback = dispatch_callback

    @property
    def backend(self) -> SchedulerBackend:
        return self._backend

    def _try_accept_slot(
        self,
        session_id: str,
        slots_needed: int,
        cap: int,
        run: RunRecord,
    ) -> tuple[bool, int]:
        """优先原子 accept，不支持时回退三步非事务调用。"""
        atomic = self._backend.atomic_accept_run(run, cap)
        if atomic is not None:
            return atomic
        acquired, active_after = self._backend.try_acquire_slot(session_id, slots_needed, cap)
        if acquired:
            self._backend.save_run(run)
            self._backend.record_heartbeat(run.run_id)
        return acquired, active_after

    def submit(
        self,
        session_id: str,
        *,
        intent: str = "",
        user_id: str = "",
        tenant_id: str = "",
        idempotency_key: str = "",
        slots_needed: int = 1,
    ) -> SubmitResult:
        """提交一个 Run 请求。"""
        if slots_needed < 1:
            raise ValueError(f"slots_needed 必须 >= 1: {slots_needed}")

        if idempotency_key:
            existing = self._backend.find_by_idempotency(session_id, idempotency_key)
            if existing:
                return SubmitResult(
                    status=existing.status,
                    run=existing,
                    message="idempotent replay",
                )

        config = get_config()
        cap = config.max_parallel_per_session

        run = RunRecord(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            intent=intent,
            idempotency_key=idempotency_key,
            status="accepted",
            slots_used=slots_needed,
        )
        acquired, active_after = self._try_accept_slot(session_id, slots_needed, cap, run)
        if acquired:
            if tenant_id:
                from ..tenant import get_tenant_manager
                mgr = get_tenant_manager()
                tenant = mgr.get_tenant_sync(tenant_id)
                quota_checks = (
                    ("max_parallel", slots_needed),
                    ("daily_runs", 1),
                )
                reserved_quotas: list[tuple[str, int]] = []
                for resource, amount in quota_checks:
                    if tenant and resource in tenant.quotas:
                        if not mgr.try_reserve_quota(tenant_id, resource, amount):
                            self._rollback_accept(
                                session_id, run, slots_needed, active_after,
                                f"租户配额已超限: {resource}",
                                tenant_id=tenant_id,
                                reserved_quotas=reserved_quotas,
                            )
                            return SubmitResult(
                                status="rejected",
                                run=None,
                                message=f"租户配额已超限: {resource}",
                            )
                        reserved_quotas.append((resource, amount))
                if tenant and "daily_tokens" in tenant.quotas:
                    if not mgr.check_quota_sync(tenant_id, "daily_tokens"):
                        self._rollback_accept(
                            session_id, run, slots_needed, active_after,
                            "租户配额已超限: daily_tokens",
                            tenant_id=tenant_id,
                            reserved_quotas=reserved_quotas,
                        )
                        return SubmitResult(
                            status="rejected",
                            run=None,
                            message="租户配额已超限: daily_tokens",
                        )
                if tenant and "daily_cost_cents" in tenant.quotas:
                    if not mgr.check_quota_sync(tenant_id, "daily_cost_cents"):
                        self._rollback_accept(
                            session_id, run, slots_needed, active_after,
                            "租户配额已超限: daily_cost_cents",
                            tenant_id=tenant_id,
                            reserved_quotas=reserved_quotas,
                        )
                        return SubmitResult(
                            status="rejected",
                            run=None,
                            message="租户配额已超限: daily_cost_cents",
                        )
            bind_run_context(
                run_id=run.run_id,
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            record_metric(
                "scheduler.slots.active",
                float(active_after),
                session_id=session_id,
                run_id=run.run_id,
            )
            record_metric(
                "scheduler.slots.utilization",
                active_after / cap if cap else 0.0,
                session_id=session_id,
            )
            log_event(
                "run.status_changed",
                run_id=run.run_id,
                old_status="",
                new_status="accepted",
            )
            emit_audit(
                "run.status_changed",
                run_id=run.run_id,
                session_id=session_id,
                old_status="",
                new_status="accepted",
            )
            self._try_dequeue(session_id, tenant_id)
            return SubmitResult(status="accepted", run=run, message="run 已接受")

        policy = config.concurrency_policy
        if policy == ConcurrencyPolicy.FIFO:
            run = RunRecord(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                intent=intent,
                idempotency_key=idempotency_key,
                status="queued",
                slots_used=slots_needed,
            )
            self._backend.save_run(run)
            log_event(
                "run.status_changed",
                run_id=run.run_id,
                old_status="",
                new_status="queued",
            )
            emit_audit(
                "run.status_changed",
                run_id=run.run_id,
                session_id=session_id,
                old_status="",
                new_status="queued",
            )
            self._try_dequeue(session_id, tenant_id)
            promoted = self._backend.get_run(run.run_id)
            if promoted and promoted.status == "accepted":
                return SubmitResult(status="accepted", run=promoted, message="已出队并接受")
            return SubmitResult(status="queued", run=run, message="已排队，等待空闲槽位")

        return SubmitResult(
            status="rejected",
            run=None,
            message=f"会话并发已满（{active_after}/{cap}）",
        )

    def _rollback_accept(
        self,
        session_id: str,
        run: RunRecord,
        slots_needed: int,
        active_after: int,
        error: str,
        *,
        tenant_id: str = "",
        reserved_quotas: list[tuple[str, int]] | None = None,
    ) -> None:
        """quota 拒绝时回滚已 accept 的 run，避免 ghost run 与 idempotency 污染。"""
        self._backend.set_active(session_id, max(0, active_after - slots_needed))
        self._backend.mark_status(run.run_id, "failed", error)
        if tenant_id and reserved_quotas:
            from ..tenant import get_tenant_manager
            mgr = get_tenant_manager()
            for resource, amount in reserved_quotas:
                mgr.release_quota(tenant_id, resource, amount)

    def _restore_dequeued_run(
        self,
        session_id: str,
        run: RunRecord,
        slots_needed: int,
        active_after: int,
        *,
        tenant_id: str = "",
        reserved_quotas: list[tuple[str, int]] | None = None,
    ) -> None:
        """出队配额不足时释放槽位，run 保持 queued。"""
        self._backend.set_active(session_id, max(0, active_after - slots_needed))
        self._backend.mark_status(run.run_id, "queued", "")
        if tenant_id and reserved_quotas:
            from ..tenant import get_tenant_manager
            mgr = get_tenant_manager()
            for resource, amount in reserved_quotas:
                mgr.release_quota(tenant_id, resource, amount)

    def release(self, session_id: str, run_id: str, slots_used: int | None = None) -> None:
        """释放一个 Run 的并发槽位。"""
        run = self._backend.get_run(run_id)
        if run is None:
            return
        if run.session_id != session_id:
            logger.warning(
                "release session mismatch: expected %s, got %s for run %s",
                session_id, run.session_id, run_id,
            )
            return
        if run.status not in ("accepted", "running"):
            return
        if slots_used is None:
            slots_used = run.slots_used if run and run.slots_used > 0 else 1
        old_status = run.status if run else "unknown"
        active_after = self._backend.adjust_active(session_id, -slots_used)
        self._backend.mark_status(run_id, "done")
        config = get_config()
        cap = config.max_parallel_per_session
        record_metric("scheduler.slots.active", float(active_after), session_id=session_id, run_id=run_id)
        record_metric(
            "scheduler.slots.utilization",
            active_after / cap if cap else 0.0,
            session_id=session_id,
        )
        log_event(
            "run.status_changed",
            run_id=run_id,
            old_status=old_status,
            new_status="done",
        )
        emit_audit(
            "run.status_changed",
            run_id=run_id,
            session_id=session_id,
            old_status=old_status,
            new_status="done",
        )
        self._try_dequeue(session_id, run.tenant_id if run else "")
        if run and run.tenant_id:
            from ..tenant import get_tenant_manager
            get_tenant_manager().release_quota(
                run.tenant_id, "max_parallel", run.slots_used or slots_used,
            )

    def heartbeat(self, run_id: str) -> None:
        """更新 run 的心跳时间。执行中的 run 应定期调用。"""
        self._backend.record_heartbeat(run_id)

    def _reserve_tenant_quotas(
        self,
        tenant_id: str,
        slots_needed: int,
    ) -> tuple[bool, list[tuple[str, int]], str]:
        """预留 tenant 配额（max_parallel + daily_runs）。"""
        from ..tenant import get_tenant_manager
        mgr = get_tenant_manager()
        tenant = mgr.get_tenant_sync(tenant_id)
        if not tenant:
            return True, [], ""
        reserved: list[tuple[str, int]] = []
        checks = (("max_parallel", slots_needed), ("daily_runs", 1))
        for resource, amount in checks:
            if resource in tenant.quotas:
                if not mgr.try_reserve_quota(tenant_id, resource, amount):
                    for res, amt in reversed(reserved):
                        mgr.release_quota(tenant_id, res, amt)
                    return False, [], f"租户配额已超限: {resource}"
                reserved.append((resource, amount))
        return True, reserved, ""

    def _check_tenant_soft_quotas(self, tenant_id: str) -> str:
        """检查 daily_tokens / daily_cost_cents（与 submit 一致）。"""
        from ..tenant import get_tenant_manager
        mgr = get_tenant_manager()
        tenant = mgr.get_tenant_sync(tenant_id)
        if not tenant:
            return ""
        if "daily_tokens" in tenant.quotas and not mgr.check_quota_sync(tenant_id, "daily_tokens"):
            return "租户配额已超限: daily_tokens"
        if "daily_cost_cents" in tenant.quotas and not mgr.check_quota_sync(tenant_id, "daily_cost_cents"):
            return "租户配额已超限: daily_cost_cents"
        return ""

    def _try_dequeue(self, session_id: str, tenant_id: str = "") -> None:
        """尝试从 FIFO 队列出队一个排队 run（原子分配槽位）。"""
        queued = self._backend.list_queued_runs(session_id, tenant_id)
        while queued:
            next_run_id = queued[0]
            run = self._backend.get_run(next_run_id)
            if not run or run.status != "queued":
                queued.pop(0)
                continue
            config = get_config()
            cap = config.max_parallel_per_session
            atomic = self._backend.atomic_accept_run(run, cap)
            if atomic is not None:
                acquired, active_after = atomic
            else:
                acquired, active_after = self._backend.try_acquire_slot(
                    session_id, run.slots_used, cap,
                )
                if acquired:
                    self._backend.save_run(run)
                    self._backend.record_heartbeat(run.run_id)
            if not acquired:
                run.status = "queued"
                return
            if run.tenant_id:
                ok, reserved, msg = self._reserve_tenant_quotas(run.tenant_id, run.slots_used)
                if not ok:
                    self._restore_dequeued_run(
                        session_id, run, run.slots_used, active_after,
                        tenant_id=run.tenant_id,
                    )
                    logger.warning("dequeue quota rejected: %s", msg)
                    return
                soft_msg = self._check_tenant_soft_quotas(run.tenant_id)
                if soft_msg:
                    self._restore_dequeued_run(
                        session_id, run, run.slots_used, active_after,
                        tenant_id=run.tenant_id, reserved_quotas=reserved,
                    )
                    logger.warning("dequeue quota rejected: %s", soft_msg)
                    return
            self._backend.mark_status(next_run_id, "accepted")
            self._backend.record_heartbeat(next_run_id)
            if self._dispatch_callback:
                self._dispatch_callback(session_id, next_run_id)
            return

    def mark_failed(self, session_id: str, run_id: str, error: str = "", slots_used: int | None = None) -> None:
        """标记 run 失败并释放槽位。"""
        run = self._backend.get_run(run_id)
        if run is None:
            return
        if run.session_id != session_id:
            logger.warning(
                "mark_failed session mismatch: expected %s, got %s for run %s",
                session_id, run.session_id, run_id,
            )
            return
        if slots_used is None:
            slots_used = run.slots_used if run and run.slots_used > 0 else 1
        old_status = run.status if run else "unknown"
        if old_status in ("accepted", "running"):
            active_after = self._backend.adjust_active(session_id, -slots_used)
        elif old_status != "queued":
            return
        self._backend.mark_status(run_id, "failed", error)
        config = get_config()
        cap = config.max_parallel_per_session
        if old_status not in ("accepted", "running"):
            active_after = self._backend.get_active(session_id)
        record_metric("scheduler.slots.active", float(active_after), session_id=session_id, run_id=run_id)
        record_metric(
            "scheduler.slots.utilization",
            active_after / cap if cap else 0.0,
            session_id=session_id,
        )
        log_event(
            "run.status_changed",
            run_id=run_id,
            old_status=old_status,
            new_status="failed",
            error=error,
        )
        emit_audit(
            "run.status_changed",
            run_id=run_id,
            session_id=session_id,
            old_status=old_status,
            new_status="failed",
            error=error,
        )
        if run.tenant_id and old_status in ("accepted", "running"):
            from ..tenant import get_tenant_manager
            get_tenant_manager().release_quota(
                run.tenant_id, "max_parallel", run.slots_used or slots_used,
            )
        self._try_dequeue(session_id, run.tenant_id if run else "")

    def active_count(self, session_id: str) -> int:
        return self._backend.get_active(session_id)

    def find_and_release_zombies(self) -> list[str]:
        """查找并释放僵尸 run（超时未心跳的 accepted/running run）。"""
        zombies = self._backend.find_zombies(self._zombie_timeout)
        for rid in zombies:
            run = self._backend.get_run(rid)
            if run and run.status in ("accepted", "running"):
                logger.warning("Releasing zombie run: %s (session=%s, status=%s)",
                               rid, run.session_id, run.status)
                self.mark_failed(run.session_id, rid, "heartbeat timeout", run.slots_used)
        return zombies

    def preview_zombies(self) -> list[str]:
        """只读预览僵尸 run 候选（不释放）。"""
        return self._backend.find_zombies(self._zombie_timeout)

    def health_summary(self) -> dict[str, Any]:
        """调度器健康摘要。"""
        return self._backend.health_snapshot()


# ============================================================================
# 全局默认实例（保持向后兼容）
# ============================================================================

_default_scheduler: SessionScheduler | None = None
_default_lock = threading.Lock()


def _get_default_scheduler() -> SessionScheduler:
    global _default_scheduler
    if _default_scheduler is None:
        with _default_lock:
            if _default_scheduler is None:
                _default_scheduler = SessionScheduler()
    return _default_scheduler


def set_default_scheduler(sched: SessionScheduler) -> None:
    """设置全局默认调度器实例。"""
    global _default_scheduler
    with _default_lock:
        _default_scheduler = sched


def reset_scheduler() -> None:
    """重置全局调度器（测试清理用）。"""
    global _default_scheduler
    with _default_lock:
        _default_scheduler = None


def submit_run(
    session_id: str,
    *,
    intent: str = "",
    user_id: str = "",
    tenant_id: str = "",
    idempotency_key: str = "",
    slots_needed: int = 1,
) -> SubmitResult:
    """提交一个 Run 请求（委托给全局默认调度器）。"""
    return _get_default_scheduler().submit(
        session_id,
        intent=intent,
        user_id=user_id,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        slots_needed=slots_needed,
    )


def release_run(session_id: str, run_id: str, slots_used: int | None = None) -> None:
    """释放一个 Run 的并发槽位（委托给全局默认调度器）。"""
    _get_default_scheduler().release(session_id, run_id, slots_used)


def active_count(session_id: str) -> int:
    return _get_default_scheduler().active_count(session_id)
