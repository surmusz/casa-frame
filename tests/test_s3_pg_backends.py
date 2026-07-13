"""参考后端与协调提示测试。"""
import json
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from casa.artifact import ArtifactStore, LocalArtifactBackend, S3ArtifactBackend, reset_backend_cache
from casa.config import init_config
from casa.orchestration import MockAgentExecutor, Plan, Stage, StageRunner


def test_coordination_hint_in_context():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j1")
        store.init_plan("p1")
        store.write(
            "raw_data",
            {"items": [1, 2]},
            coordination_hint={"format": "weekly", "timezone": "UTC"},
        )

        runner = StageRunner(store=store, executor=MockAgentExecutor({}))
        ctx = runner._build_execute_context(
            Stage(
                stage_id="analyst",
                agent_id="analyst",
                input_refs=["job:j1:artifact:raw_data"],
            ),
            Plan(),
            fresh_session=False,
        )
        assert ctx["coordination_hints"]["raw_data"]["format"] == "weekly"


def test_read_coordination_hint_standalone():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j1")
        store.init_plan("p1")
        store.write("out", {"v": 1}, coordination_hint={"note": "ok"})
        assert store.read_coordination_hint("out") == {"note": "ok"}
        assert store.read_coordination_hint("missing") is None


def test_s3_backend_write_read_roundtrip():
    mock_client = MagicMock()
    stored: dict[str, bytes] = {}

    def put_object(Bucket, Key, Body, **kwargs):
        stored[Key] = Body

    def get_object(Bucket, Key):
        return {"Body": MagicMock(read=lambda: stored[Key])}

    mock_client.put_object.side_effect = put_object
    mock_client.get_object.side_effect = get_object
    mock_client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

    backend = S3ArtifactBackend(bucket="test-bucket")
    backend._client = mock_client

    data = {"hello": "world"}
    backend.write(
        "artifacts/j1/p1/raw",
        data,
        "/tmp/j1/plans/p1/artifacts",
        "raw",
    )
    result = backend.read(
        "artifacts/j1/p1/raw",
        "/tmp/j1/plans/p1/artifacts",
        "raw",
    )
    assert result == data


def test_s3_presigned_url():
    mock_client = MagicMock()
    mock_client.generate_presigned_url.return_value = "https://signed.example/url"
    backend = S3ArtifactBackend(bucket="b")
    backend._client = mock_client
    url = backend.presigned_url("artifacts/j1/p1/report")
    assert url.startswith("https://")
    mock_client.generate_presigned_url.assert_called_once()


def test_s3_resolve_from_config():
    reset_backend_cache()
    with tempfile.TemporaryDirectory() as tmp:
        init_config(
            artifact_base_dir=tmp,
            artifact_storage_backend="s3",
            s3_bucket="my-bucket",
            s3_access_key="key",
            s3_secret_key="secret",
        )
        store = ArtifactStore("j")
        assert type(store._backend).__name__ == "S3ArtifactBackend"
    reset_backend_cache()


def test_pg_scheduler_import():
    pytest.importorskip("psycopg", reason="psycopg not installed")
    from casa.scheduler import PgSchedulerBackend
    assert PgSchedulerBackend.__doc__


def test_pg_grant_store_import():
    pytest.importorskip("psycopg", reason="psycopg not installed")
    from casa.authority import PgGrantStore
    assert PgGrantStore.__doc__
