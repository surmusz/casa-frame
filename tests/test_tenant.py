"""多租户隔离测试 — Phase 3。"""
import os
import tempfile

from casa.artifact import ArtifactStore, _job_root, _plan_rel_path
from casa.config import init_config


def test_empty_tenant_backward_compatible_path():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        assert _job_root(tmp, "", "j1") == os.path.join(tmp, "j1")
        assert _plan_rel_path(tmp, "", "j1", "p1") == os.path.join(tmp, "j1", "plans", "p1", "artifacts")


def test_tenant_isolated_paths():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store_a = ArtifactStore("same_job", tenant_id="tenant_a", base_dir=tmp)
        store_b = ArtifactStore("same_job", tenant_id="tenant_b", base_dir=tmp)
        store_a.init_plan("plan1")
        store_b.init_plan("plan1")
        store_a.write("data", {"from": "a"})
        store_b.write("data", {"from": "b"})
        assert store_a.read("data") == {"from": "a"}
        assert store_b.read("data") == {"from": "b"}
        assert store_a._plan_dir != store_b._plan_dir
