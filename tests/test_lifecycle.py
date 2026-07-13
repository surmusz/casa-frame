"""产物生命周期测试。"""
import tempfile

from casa.artifact import ArtifactStore
from casa.lifecycle import ArtifactLifecycleManager, RetentionTier, ArtifactRetentionPolicy


def test_lifecycle_ephemeral_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore("j1")
        store.init_plan("p1")
        store.write("scratch", {"temp": True})
        store.write("report", {"final": True})

        mgr = ArtifactLifecycleManager(
            ArtifactRetentionPolicy(overrides={"scratch": RetentionTier.EPHEMERAL}),
        )
        result = mgr.cleanup_plan(store, "p1", "j1")
        assert "scratch" in result["removed"]
        assert store.read("report") is not None
        assert store.read("scratch") is None


def test_lifecycle_dry_run_keeps_artifacts():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore("j1")
        store.init_plan("p1")
        store.write("scratch", {"temp": True})

        mgr = ArtifactLifecycleManager(
            ArtifactRetentionPolicy(overrides={"scratch": RetentionTier.EPHEMERAL}),
        )
        result = mgr.cleanup_plan(store, "p1", "j1", dry_run=True)
        assert "scratch" in result["removed"]
        assert store.read("scratch") is not None


def test_lifecycle_register_kind():
    mgr = ArtifactLifecycleManager()
    mgr.register_kind("cache_blob", RetentionTier.EPHEMERAL)
    assert mgr.tier_for("cache_blob") == RetentionTier.EPHEMERAL
    assert mgr.tier_for("unknown") == RetentionTier.JOB
