"""域 / DataStore 隔离测试。"""
import tempfile

import pytest

from casa.artifact import ArtifactStore
from casa.config import init_config
from casa.scope import DataStore, DataStoreAccessError, RefID


def test_job_mismatch_when_store_bound():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("job_a")
        store.init_plan("p1")
        store.write("data", {"v": 1})
        ds = DataStore(artifact_store=store, job_id="job_b")
        with pytest.raises(DataStoreAccessError):
            ds.resolve_read(RefID.job_artifact("job_a", "data"))


def test_plan_id_mismatch_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j1")
        store.init_plan("plan_a")
        store.write("out", {"ok": True})
        ds = DataStore(artifact_store=store, job_id="j1")
        with pytest.raises(DataStoreAccessError):
            ds.resolve_read(RefID.plan_artifact("plan_b", "out"))


def test_plan_id_match_ok():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j1")
        store.init_plan("plan_a")
        store.write("out", {"ok": True})
        ds = DataStore(artifact_store=store, job_id="j1")
        data = ds.resolve_read(RefID.plan_artifact("plan_a", "out"))
        assert data == {"ok": True}
