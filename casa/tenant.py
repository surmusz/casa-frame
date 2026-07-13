"""
CASA 租户 — 租户管理、配额与配置覆盖。
"""

from __future__ import annotations

import abc
import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field
from typing import Any

from .config import CASAConfig


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _usage_key(resource: str) -> str:
    if resource in ("daily_tokens", "daily_cost_cents", "daily_runs"):
        return f"{resource}:{_today()}"
    return resource

_sync_executor: concurrent.futures.ThreadPoolExecutor | None = None
_sync_executor_lock = threading.Lock()


def _get_sync_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _sync_executor
    if _sync_executor is None:
        with _sync_executor_lock:
            if _sync_executor is None:
                _sync_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=2,
                    thread_name_prefix="casa-tenant",
                )
    return _sync_executor


@dataclass(kw_only=True)
class Tenant:
    tenant_id: str
    display_name: str = ""
    config_overrides: dict[str, Any] = field(default_factory=dict)
    quotas: dict[str, int] = field(default_factory=dict)
    enabled: bool = True


class TenantManager(abc.ABC):
    @abc.abstractmethod
    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        ...

    @abc.abstractmethod
    async def check_quota(self, tenant_id: str, resource: str) -> bool:
        ...


class InMemoryTenantManager(TenantManager):
    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._usage: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def register(self, tenant: Tenant) -> None:
        with self._lock:
            self._tenants[tenant.tenant_id] = tenant
            self._usage.setdefault(tenant.tenant_id, {})

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self.get_tenant_sync(tenant_id)

    def get_tenant_sync(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            return self._tenants.get(tenant_id)

    async def check_quota(self, tenant_id: str, resource: str) -> bool:
        return self._check_quota_locked(tenant_id, resource)

    def _check_quota_locked(self, tenant_id: str, resource: str) -> bool:
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant is None:
                return True
            if not tenant.enabled:
                return False
            limit = tenant.quotas.get(resource)
            if limit is None:
                return True
            key = _usage_key(resource)
            used = self._usage.get(tenant_id, {}).get(key, 0)
            return used < limit

    def try_reserve_quota(self, tenant_id: str, resource: str, amount: int = 1) -> bool:
        """原子地检查配额并递增用量。"""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant is None:
                return True
            if not tenant.enabled:
                return False
            limit = tenant.quotas.get(resource)
            if limit is None:
                return True
            key = _usage_key(resource)
            used = self._usage.get(tenant_id, {}).get(key, 0)
            if used + amount > limit:
                return False
            self._usage.setdefault(tenant_id, {})
            self._usage[tenant_id][key] = used + amount
            return True

    def release_quota(self, tenant_id: str, resource: str, amount: int = 1) -> None:
        with self._lock:
            key = _usage_key(resource)
            usage = self._usage.setdefault(tenant_id, {})
            usage[key] = max(0, usage.get(key, 0) - amount)

    def record_token_usage(self, tenant_id: str, tokens: int) -> bool:
        """记录 token 消耗。返回 False 表示已超过 daily_tokens 配额。"""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant is None:
                return True
            limit = tenant.quotas.get("daily_tokens")
            if limit is None:
                return True
            key = _usage_key("daily_tokens")
            current = self._usage.setdefault(tenant_id, {}).get(key, 0)
            new_total = current + tokens
            self._usage[tenant_id][key] = new_total
            return new_total <= limit

    def record_cost_usage(self, tenant_id: str, cost_cents: int) -> bool:
        """记录成本消耗（美分）。返回 False 表示已超过 daily_cost_cents 配额。"""
        with self._lock:
            tenant = self._tenants.get(tenant_id)
            if tenant is None:
                return True
            limit = tenant.quotas.get("daily_cost_cents")
            if limit is None:
                return True
            key = _usage_key("daily_cost_cents")
            current = self._usage.setdefault(tenant_id, {}).get(key, 0)
            new_total = current + cost_cents
            self._usage[tenant_id][key] = new_total
            return new_total <= limit

    def get_token_usage(self, tenant_id: str) -> dict[str, int]:
        with self._lock:
            key = _usage_key("daily_tokens")
            tenant = self._tenants.get(tenant_id)
            limit = tenant.quotas.get("daily_tokens", 0) if tenant else 0
            return {
                "used": self._usage.get(tenant_id, {}).get(key, 0),
                "limit": limit or 0,
            }

    def check_quota_sync(self, tenant_id: str, resource: str) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._check_quota_locked(tenant_id, resource)
        return _get_sync_executor().submit(
            self._check_quota_locked, tenant_id, resource,
        ).result()

    def increment_usage(self, tenant_id: str, resource: str, amount: int = 1) -> None:
        with self._lock:
            self._usage.setdefault(tenant_id, {})
            self._usage[tenant_id][resource] = self._usage[tenant_id].get(resource, 0) + amount


def tenant_config(base: CASAConfig, tenant: Tenant) -> CASAConfig:
    """将全局配置与租户覆盖项合并。"""
    from .config_loader import ConfigLoader
    overrides = {k: v for k, v in tenant.config_overrides.items()}
    override_cfg = CASAConfig(**{**base.to_dict_safe(), **overrides})
    return ConfigLoader.merge(base, override_cfg)


_default_manager: InMemoryTenantManager | None = None
_manager_lock = threading.Lock()


def get_tenant_manager() -> InMemoryTenantManager:
    global _default_manager
    if _default_manager is None:
        with _manager_lock:
            if _default_manager is None:
                _default_manager = InMemoryTenantManager()
    return _default_manager


def reset_tenant_manager() -> None:
    global _default_manager
    with _manager_lock:
        _default_manager = InMemoryTenantManager()
