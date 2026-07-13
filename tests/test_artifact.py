"""产物存储测试。"""
import os
import tempfile

from casa.artifact import ArtifactStore, LocalArtifactBackend
from casa.config import init_config


def test_backend_exists_delete():
    backend = LocalArtifactBackend()
    with tempfile.TemporaryDirectory() as tmp:
        plan_dir = os.path.join(tmp, "artifacts")
        os.makedirs(plan_dir)
        assert not backend.exists(plan_dir, "kind_a")
        backend.write("", {"x": 1}, plan_dir, "kind_a")
        assert backend.exists(plan_dir, "kind_a")
        assert backend.delete(plan_dir, "kind_a")
        assert not backend.exists(plan_dir, "kind_a")


def test_artifact_store_exists_uses_backend():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j1")
        store.init_plan("p1")
        assert not store.exists("out")
        store.write("out", {"v": 1})
        assert store.exists("out")
        assert store.delete("out")
        assert not store.exists("out")
