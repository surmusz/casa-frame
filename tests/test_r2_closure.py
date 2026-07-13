"""REVIEW-V1.4.1-R2 闭环回归测试。"""
import abc
import asyncio
import inspect
import threading

import pytest

from casa.artifact import ArtifactBackend
from casa.authority import CapabilityMatrix, CapabilityRow
from casa.events import EventBus, InProcessEventBus
from casa.intent import IntentRouter
from casa.knowledge import KBEntry
from casa.tenant import InMemoryTenantManager, Tenant


def test_kb_entry_permissions_removed():
    assert "permissions" not in KBEntry.__dataclass_fields__
    entry = KBEntry(entry_id="a", content="x", tags=["public"])
    assert entry.tags == ["public"]


def test_artifact_backend_write_report_not_abstract():
    assert not getattr(ArtifactBackend.write_report, "__isabstractmethod__", False)
    assert not getattr(ArtifactBackend.read_report, "__isabstractmethod__", False)
    assert getattr(ArtifactBackend.write_deliverable_file, "__isabstractmethod__", False)
    assert getattr(ArtifactBackend.read_deliverable_file, "__isabstractmethod__", False)


def test_minimal_artifact_backend_without_write_report():
    class MinimalBackend(ArtifactBackend):
        def write(self, storage_key, data, plan_dir, artifact_kind):
            return None

        def read(self, storage_key, plan_dir, artifact_kind):
            return None

        def list_keys(self, plan_dir):
            return []

        def exists(self, plan_dir, artifact_kind):
            return False

        def delete(self, plan_dir, artifact_kind):
            return False

        def write_deliverable_file(self, data, base_dir, tenant_id, job_id, plan_id, filename):
            return "/tmp/out.html"

        def read_deliverable_file(self, base_dir, tenant_id, job_id, plan_id, filename):
            return b"ok"

    backend = MinimalBackend()
    path = backend.write_report(b"x", "/b", "", "j", "p", "f.html")
    assert path == "/tmp/out.html"
    assert backend.read_report("/b", "", "j", "p", "f.html") == b"ok"


def test_event_bus_subscribe_returns_str():
    sig = inspect.signature(EventBus.subscribe)
    ret = sig.return_annotation
    assert ret is str or ret == "str"
    bus = InProcessEventBus()
    sub_id = bus.subscribe("*", lambda e: None)
    assert isinstance(sub_id, str)
    assert bus.unsubscribe(sub_id)


def test_check_quota_locked_exists_and_runs_under_lock():
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"max_parallel": 1}))
    assert hasattr(mgr, "_check_quota_locked")
    assert asyncio.run(mgr.check_quota("t1", "max_parallel"))
    assert mgr.check_quota_sync("t1", "max_parallel")
    mgr.try_reserve_quota("t1", "max_parallel")
    assert not mgr._check_quota_locked("t1", "max_parallel")


def test_intent_router_from_matrix_list_rows():
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="a1", display_name="Agent One"))
    router = IntentRouter.from_capability_matrix(matrix, {"a1": ([], "out")})
    assert "a1" in router.catalog


def test_intent_router_matrix_without_to_rows_uses_list_rows():
    class MatrixShim:
        def list_rows(self):
            return [CapabilityRow(agent_id="shim", display_name="Shim")]

    router = IntentRouter.from_capability_matrix(MatrixShim(), {"shim": ([], "x")})
    assert router.catalog["shim"].display_name == "Shim"


def test_check_quota_sync_thread_safe_under_concurrency():
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"max_parallel": 10}))
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(50):
                mgr.check_quota_sync("t1", "max_parallel")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
