"""
CASA Artifact 生命周期 — TTL、保留策略与归档。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("casa.lifecycle")


class RetentionTier(str, Enum):
    EPHEMERAL = "ephemeral"
    SESSION = "session"
    JOB = "job"
    PERMANENT = "permanent"


@dataclass(kw_only=True)
class ArtifactRetentionPolicy:
    default_tier: RetentionTier = RetentionTier.JOB
    overrides: dict[str, RetentionTier] = field(default_factory=dict)
    ephemeral_ttl_hours: int = 24
    job_ttl_days: int = 7
    max_artifacts_per_plan: int = 500

    def tier_for(self, artifact_kind: str) -> RetentionTier:
        return self.overrides.get(artifact_kind, self.default_tier)


class ArtifactLifecycleManager:
    """管理 artifact 保留策略与 plan 级清理。"""

    def __init__(self, policy: ArtifactRetentionPolicy | None = None):
        self._policy = policy or ArtifactRetentionPolicy()
        self._kind_tiers: dict[str, RetentionTier] = {}

    def register_kind(self, artifact_kind: str, tier: RetentionTier | str) -> None:
        if isinstance(tier, str):
            tier = RetentionTier(tier)
        self._kind_tiers[artifact_kind] = tier

    def tier_for(self, artifact_kind: str) -> RetentionTier:
        if artifact_kind in self._kind_tiers:
            return self._kind_tiers[artifact_kind]
        return self._policy.tier_for(artifact_kind)

    def cleanup_plan(
        self,
        store: Any,
        plan_id: str,
        job_id: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        removed: list[str] = []
        kinds = store.list_artifacts()
        if len(kinds) > self._policy.max_artifacts_per_plan:
            logger.warning(
                "plan %s exceeds max_artifacts_per_plan (%d > %d)",
                plan_id, len(kinds), self._policy.max_artifacts_per_plan,
            )
        for kind in kinds:
            if self.tier_for(kind) == RetentionTier.EPHEMERAL:
                if not dry_run:
                    store.delete(kind)
                removed.append(kind)
        return {"plan_id": plan_id, "job_id": job_id, "removed": removed, "dry_run": dry_run}


def _looks_like_tenant_root(path: str) -> bool:
    """子目录为 tenant 根：自身不含 plans/，且至少一个子 job 含 plans/。"""
    if not os.path.isdir(path) or os.path.isdir(os.path.join(path, "plans")):
        return False
    children = [
        name for name in os.listdir(path)
        if os.path.isdir(os.path.join(path, name))
    ]
    if not children:
        return False
    return any(os.path.isdir(os.path.join(path, name, "plans")) for name in children)


def scan_plan_dirs(
    base_dir: str,
    *,
    tenant_id: str = "",
) -> list[tuple[str, str, str]]:
    """扫描 base_dir 下所有 (tenant_id, job_id, plan_id) 目录。"""
    found: list[tuple[str, str, str]] = []

    def _scan_jobs(root: str, tid: str) -> None:
        if not os.path.isdir(root):
            return
        for job_id in os.listdir(root):
            job_path = os.path.join(root, job_id)
            if not os.path.isdir(job_path):
                continue
            plans_dir = os.path.join(job_path, "plans")
            if not os.path.isdir(plans_dir):
                continue
            for plan_id in os.listdir(plans_dir):
                art_dir = os.path.join(plans_dir, plan_id, "artifacts")
                if os.path.isdir(art_dir):
                    found.append((tid, job_id, plan_id))

    if tenant_id:
        _scan_jobs(os.path.join(base_dir, tenant_id), tenant_id)
    else:
        _scan_jobs(base_dir, "")
        if os.path.isdir(base_dir):
            for entry in os.listdir(base_dir):
                tenant_path = os.path.join(base_dir, entry)
                if _looks_like_tenant_root(tenant_path):
                    _scan_jobs(tenant_path, entry)
    return found


class LifecycleCleanupScheduler:
    """
    后台定时清理 scheduler（cron-like）。

    按 ``lifecycle_cleanup_interval_seconds`` 扫描 artifact 目录，
    对每个 plan 调用 ``ArtifactLifecycleManager.cleanup_plan()``。
    """

    def __init__(
        self,
        manager: ArtifactLifecycleManager,
        *,
        interval_seconds: float | None = None,
        base_dir: str | None = None,
        tenant_id: str = "",
        on_cleanup: Callable[[dict[str, Any]], None] | None = None,
    ):
        from .config import get_config

        cfg = get_config()
        self._manager = manager
        self._interval = interval_seconds or float(
            getattr(cfg, "lifecycle_cleanup_interval_seconds", 3600),
        )
        self._base_dir = base_dir or cfg.artifact_base_dir
        self._tenant_id = tenant_id or cfg.tenant_id
        self._on_cleanup = on_cleanup
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[dict[str, Any]]:
        """执行一轮全量扫描清理。"""
        from .artifact import ArtifactStore

        reports: list[dict[str, Any]] = []
        for tid, job_id, plan_id in scan_plan_dirs(self._base_dir, tenant_id=self._tenant_id):
            store = ArtifactStore(job_id, base_dir=self._base_dir, tenant_id=tid or self._tenant_id)
            store.init_plan(plan_id)
            report = self._manager.cleanup_plan(store, plan_id, job_id)
            if report.get("removed"):
                reports.append(report)
                logger.info(
                    "lifecycle scheduled cleanup: job=%s plan=%s removed=%d",
                    job_id, plan_id, len(report["removed"]),
                )
            if self._on_cleanup:
                self._on_cleanup(report)
        return reports

    def start(self) -> None:
        """启动后台守护线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="casa-lifecycle-cleanup", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        """停止后台线程。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout or self._interval + 5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("lifecycle scheduled cleanup failed")
            self._stop.wait(self._interval)


class ArtifactBackupManager:
    """Artifact 单 plan / 全 job 备份与恢复（本地文件镜像）。"""

    def backup_plan(self, store: Any, dest_root: str) -> dict[str, Any]:
        """将当前 plan 的 artifacts 目录镜像到 dest_root。"""
        import shutil

        if not store.plan_dir:
            raise ValueError("store 未 init_plan，无法备份")
        if store.tenant_id:
            rel = os.path.join(store.tenant_id, store.job_id, "plans", store.plan_id)
        else:
            rel = os.path.join(store.job_id, "plans", store.plan_id)
        dest = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copytree(store.plan_dir, dest, dirs_exist_ok=True)
        kinds = store.list_artifacts()
        return {
            "job_id": store.job_id,
            "plan_id": store.plan_id,
            "dest": dest,
            "artifact_kinds": kinds,
            "file_count": len(kinds),
        }

    def restore_plan(self, store: Any, src_root: str) -> dict[str, Any]:
        """从备份目录恢复 artifacts 到 store。"""
        import shutil

        if not store.plan_dir:
            store.init_plan(store.plan_id or "restored")
        if store.tenant_id:
            rel = os.path.join(store.tenant_id, store.job_id, "plans", store.plan_id)
        else:
            rel = os.path.join(store.job_id, "plans", store.plan_id)
        src = os.path.join(src_root, rel)
        if not os.path.isdir(src):
            raise FileNotFoundError(f"备份目录不存在: {src}")
        os.makedirs(store.plan_dir, exist_ok=True)
        for name in os.listdir(src):
            if name.endswith(".json"):
                shutil.copy2(os.path.join(src, name), os.path.join(store.plan_dir, name))
        kinds = store.list_artifacts()
        return {
            "job_id": store.job_id,
            "plan_id": store.plan_id,
            "src": src,
            "restored_kinds": kinds,
        }

    def backup_job(
        self,
        base_dir: str,
        job_id: str,
        dest_root: str,
        *,
        tenant_id: str = "",
    ) -> list[dict[str, Any]]:
        """备份 job 下所有 plan。"""
        from .artifact import ArtifactStore

        reports: list[dict[str, Any]] = []
        for tid, jid, pid in scan_plan_dirs(base_dir, tenant_id=tenant_id):
            if jid != job_id:
                continue
            store = ArtifactStore(job_id, base_dir=base_dir, tenant_id=tid or tenant_id)
            store.init_plan(pid)
            reports.append(self.backup_plan(store, dest_root))
        return reports
