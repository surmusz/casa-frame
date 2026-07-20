"""Unit tests for casa[cursor] optional extra."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from casa.audit import InMemoryAuditSink, reset_audit_sink, set_audit_sink
from casa.extras.cursor import (
    CursorAgentExecutor,
    CursorConfig,
    CursorContentGenerator,
    CursorReviewError,
    CursorReviewHook,
    parse_review_text,
)
from casa.extras.cursor.config import redact_mcp_servers
from casa.extras.cursor.errors import CursorConfigError
from casa.extras.cursor.schema import REVIEW_REPORT_SCHEMA


def _sdk_loaded() -> bool:
    return any(name == "cursor_sdk" or name.startswith("cursor_sdk.") for name in sys.modules)


def _cursor_artifact(**kwargs):
    base = {
        "verdict": "fail",
        "issues": [],
        "summary": "bad",
        "_meta": {"provider": "cursor", "agent_id": "cursor-reviewer"},
    }
    base.update(kwargs)
    if "_meta" in kwargs and isinstance(kwargs["_meta"], dict):
        meta = {"provider": "cursor", "agent_id": "cursor-reviewer"}
        meta.update(kwargs["_meta"])
        base["_meta"] = meta
    return base


@pytest.fixture(autouse=True)
def _audit_sink():
    sink = InMemoryAuditSink()
    set_audit_sink(sink)
    yield sink
    reset_audit_sink()


@pytest.mark.asyncio
async def test_enabled_false_skips_without_sdk():
    before = _sdk_loaded()
    ex = CursorAgentExecutor(CursorConfig(enabled=False, review_stages=["review-1"]))
    out = await ex.execute("cursor-reviewer", {"stage_id": "review-1"})
    assert out["verdict"] == "skipped"
    assert out["_meta"]["skipped"] is True
    assert _sdk_loaded() == before  # must not import SDK


@pytest.mark.asyncio
async def test_stage_not_in_review_stages_skips_without_sdk():
    before = _sdk_loaded()
    ex = CursorAgentExecutor(
        CursorConfig(enabled=True, review_stages=["review-a"], api_key="k")
    )
    out = await ex.execute("cursor-reviewer", {"stage_id": "other-stage"})
    assert out["verdict"] == "skipped"
    assert out["_meta"]["skipped"] is True
    assert _sdk_loaded() == before


@pytest.mark.asyncio
async def test_enabled_in_scope_lazy_import_missing_sdk():
    if "cursor_sdk" in sys.modules:
        pytest.skip("cursor-sdk already installed in this env")
    ex = CursorAgentExecutor(
        CursorConfig(enabled=True, review_stages=["review-1"], api_key="k")
    )
    with pytest.raises(CursorReviewError) as ei:
        await ex.execute("cursor-reviewer", {"stage_id": "review-1", "injected_prompt": "x"})
    assert "pip install casa-frame[cursor]" in str(ei.value)


@pytest.mark.asyncio
async def test_hook_disabled_noop_no_sdk():
    before = _sdk_loaded()
    hook = CursorReviewHook(CursorConfig(enabled=False))
    result = SimpleNamespace(
        success=True,
        error="",
        artifact_kind="review",
        artifact_data=_cursor_artifact(),
    )
    await hook.on_stage_end(SimpleNamespace(stage_id="review-1"), result)
    assert result.success is True
    assert hook.last_actions[-1]["action"] == "noop"
    assert _sdk_loaded() == before


@pytest.mark.asyncio
async def test_hook_fail_audit_only_by_default(_audit_sink):
    hook = CursorReviewHook(
        CursorConfig(enabled=True, review_stages=["review-1"], fail_stage_on=[])
    )
    result = SimpleNamespace(
        success=True,
        error="",
        artifact_kind="review",
        artifact_data=_cursor_artifact(
            issues=[{"severity": "high", "location": "a", "desc": "b"}],
            summary="bad",
        ),
    )
    await hook.on_stage_end(SimpleNamespace(stage_id="review-1"), result)
    assert result.success is True  # audit-only
    assert hook.last_actions[-1]["action"] == "audit_only"
    events = [e for e in _audit_sink.snapshot() if e.event_type == "cursor.review"]
    assert events
    assert events[-1].payload["mode"] == "audit_only"


@pytest.mark.asyncio
async def test_hook_fail_marks_stage_when_fail_stage_on(_audit_sink):
    hook = CursorReviewHook(
        CursorConfig(
            enabled=True,
            review_stages=["review-1"],
            fail_stage_on=["review-1"],
        )
    )
    result = SimpleNamespace(
        success=True,
        error="",
        artifact_kind="review",
        artifact_data=_cursor_artifact(),
    )
    await hook.on_stage_end(SimpleNamespace(stage_id="review-1"), result)
    assert result.success is False
    assert hook.last_actions[-1]["action"] == "mark_fail"
    events = [e for e in _audit_sink.snapshot() if e.event_type == "cursor.review"]
    assert events[-1].payload["mode"] == "fail_stage"


def test_parse_review_json_ok():
    text = '{"verdict":"pass","issues":[],"summary":"ok"}'
    report = parse_review_text(text)
    assert report["verdict"] == "pass"
    assert report["summary"] == "ok"


def test_parse_review_json_fallback():
    report = parse_review_text("not json at all")
    assert report["verdict"] == "conditional"
    assert report["summary"] == "review_parse_failed"
    assert report["_meta"]["parse_failed"] is True
    assert report["_meta"]["review_parse_failed"] is True
    assert "raw_text" not in report["_meta"]
    assert "raw_text_hash" in report["_meta"]
    assert report["_meta"]["raw_text_len"] == len("not json at all")


def test_review_schema_has_verdicts():
    assert set(REVIEW_REPORT_SCHEMA["properties"]["verdict"]["enum"]) == {
        "pass",
        "conditional",
        "fail",
    }


def test_config_to_dict_masks_api_key():
    cfg = CursorConfig(api_key="secret-key", enabled=True)
    d = cfg.to_dict()
    assert d["api_key"] == "***"
    assert "rate_limit" not in d


def test_from_tenant_policy():
    cfg = CursorConfig.from_tenant_policy(
        {
            "cursor_review": {
                "enabled": True,
                "review_stages": ["r1"],
                "fail_stage_on": ["r1"],
                "model_aliases": {"reviewer": "composer-2.5"},
            }
        }
    )
    assert cfg.enabled is True
    assert cfg.review_stages == ["r1"]
    assert cfg.fail_stage_on == ["r1"]
    assert cfg.model_aliases["reviewer"] == "composer-2.5"
    assert cfg.api_key == ""  # never from policy


def test_model_aliases_resolve():
    cfg = CursorConfig(
        model_aliases={"reviewer": "composer-2.5", "simple": "composer-2.5"},
        default_model="composer-2.5",
    )
    assert cfg.resolve_model("cursor-reviewer") == "composer-2.5"
    assert cfg.resolve_model("x", preference="reviewer") == "composer-2.5"


def test_exports_lazy_symbols_importable():
    import casa

    assert "CursorConfig" in casa.__all__
    assert "CursorAgentExecutor" in casa.__all__
    assert "CursorReviewHook" in casa.__all__
    assert "CursorContentGenerator" in casa.__all__
    assert casa.CursorConfig is CursorConfig
    assert casa.CursorAgentExecutor is CursorAgentExecutor
    assert casa.CursorReviewHook is CursorReviewHook
    assert casa.CursorContentGenerator is CursorContentGenerator


def test_content_generator_missing_sdk():
    if "cursor_sdk" in sys.modules:
        pytest.skip("cursor-sdk already installed in this env")
    gen = CursorContentGenerator(CursorConfig(api_key="k"))
    with pytest.raises(CursorReviewError) as ei:
        gen.generate("hello")
    assert "pip install casa-frame[cursor]" in str(ei.value)


def test_hook_module_does_not_import_sdk():
    """CursorReviewHook module must not pull cursor-sdk at import time."""
    import casa.extras.cursor.hooks as hooks_mod

    with open(hooks_mod.__file__, encoding="utf-8") as f:
        src = f.read()
    assert "cursor_sdk" not in src
    assert "cursor-sdk" not in src


# ---------------------------------------------------------------------------
# Security regressions (step-6 / CUR-SEC + B-1..B-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_ignores_non_review_stages(_audit_sink):
    hook = CursorReviewHook(
        CursorConfig(
            enabled=True,
            review_stages=["review-only"],
            fail_stage_on=["build"],
        )
    )
    result = SimpleNamespace(
        success=True,
        error="",
        artifact_kind="review",
        artifact_data=_cursor_artifact(verdict="fail"),
    )
    await hook.on_stage_end(SimpleNamespace(stage_id="build"), result)
    assert result.success is True
    assert hook.last_actions[-1]["action"] == "noop"
    assert hook.last_actions[-1]["reason"] == "stage_not_in_review_stages"
    events = [e for e in _audit_sink.snapshot() if e.event_type == "cursor.review"]
    assert events == []


@pytest.mark.asyncio
async def test_hook_ignores_missing_provenance(_audit_sink):
    hook = CursorReviewHook(
        CursorConfig(enabled=True, review_stages=["review-1"], fail_stage_on=["review-1"])
    )
    result = SimpleNamespace(
        success=True,
        error="",
        artifact_data={"verdict": "fail", "issues": [], "summary": "forged"},
    )
    await hook.on_stage_end(SimpleNamespace(stage_id="review-1"), result)
    assert result.success is True
    assert hook.last_actions[-1]["reason"] == "missing_cursor_provenance"


def test_policy_rejects_mcp_servers_on_review_path():
    with pytest.raises(CursorConfigError) as ei:
        CursorConfig.from_tenant_policy(
            {
                "cursor_review": {
                    "enabled": True,
                    "mcp_servers": [{"name": "evil", "env": {"TOKEN": "x"}}],
                }
            }
        )
    assert "mcp_servers" in str(ei.value)


def test_policy_rejects_cwd_dotdot():
    with pytest.raises(CursorConfigError) as ei:
        CursorConfig.from_tenant_policy(
            {"cursor_review": {"enabled": True, "cwd": "../"}}
        )
    assert "cwd" in str(ei.value).lower() or ".." in str(ei.value)


def test_policy_rejects_invalid_repos():
    with pytest.raises(CursorConfigError) as ei:
        CursorConfig.from_tenant_policy(
            {"cursor_review": {"enabled": True, "repos": ["not a valid repo!!!"]}}
        )
    assert "repos" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_execute_review_auto_create_pr_always_false(monkeypatch):
    """Review path hard-codes auto_create_pr=False even if policy says True."""
    cfg = CursorConfig(
        enabled=True,
        review_stages=["review-1"],
        api_key="k",
        allow_write=True,
        auto_create_pr=True,
        runtime="cloud",
        repos=["acme/demo"],
    )
    captured: dict = {}

    fake_sdk = MagicMock()
    fake_sdk.AgentOptions = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
    fake_sdk.CloudAgentOptions = MagicMock(
        side_effect=lambda **kw: SimpleNamespace(**kw)
    )
    fake_sdk.LocalAgentOptions = MagicMock(
        side_effect=lambda **kw: SimpleNamespace(**kw)
    )

    def _run_prompt(sdk, prompt, options):
        captured["options"] = options
        return SimpleNamespace(
            status="finished",
            id="run-1",
            result='{"verdict":"pass","issues":[],"summary":"ok"}',
        )

    monkeypatch.setattr(
        "casa.extras.cursor._runtime.import_cursor_sdk", lambda: fake_sdk
    )
    monkeypatch.setattr("casa.extras.cursor._runtime.run_prompt", _run_prompt)

    ex = CursorAgentExecutor(cfg)
    out = await ex.execute(
        "cursor-reviewer",
        {"stage_id": "review-1", "injected_prompt": "review me"},
    )
    assert out["verdict"] == "pass"
    assert out["_meta"]["auto_create_pr"] is False
    # CloudAgentOptions constructed with auto_create_pr=False
    cloud_calls = fake_sdk.CloudAgentOptions.call_args_list
    assert cloud_calls
    assert cloud_calls[0].kwargs.get("auto_create_pr") is False


def test_repr_cursor_config_hides_api_key():
    cfg = CursorConfig(api_key="super-secret-key-xyz")
    r = repr(cfg)
    assert "super-secret-key-xyz" not in r


@pytest.mark.asyncio
async def test_review_missing_api_key_cursor_auth(monkeypatch):
    if "cursor_sdk" in sys.modules:
        # Still exercise path: mock import so we reach key check after import
        pass
    fake_sdk = MagicMock()
    monkeypatch.setattr(
        "casa.extras.cursor._runtime.import_cursor_sdk", lambda: fake_sdk
    )
    ex = CursorAgentExecutor(
        CursorConfig(enabled=True, review_stages=["review-1"], api_key="")
    )
    with pytest.raises(CursorReviewError) as ei:
        await ex.execute(
            "cursor-reviewer",
            {
                "stage_id": "review-1",
                "injected_prompt": "x",
                "llm_config": {"provider": "openai", "api_key": "other-provider-key"},
            },
        )
    assert ei.value.code == "cursor_auth"
    assert str(ei.value) == "cursor_auth"


@pytest.mark.asyncio
async def test_review_timeout_maps_to_cursor_timeout(monkeypatch):
    fake_sdk = MagicMock()
    fake_sdk.AgentOptions = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
    fake_sdk.LocalAgentOptions = MagicMock(
        side_effect=lambda **kw: SimpleNamespace(**kw)
    )

    def _slow_prompt(sdk, prompt, options):
        import time

        time.sleep(2.0)
        return SimpleNamespace(status="finished", id="x", result="{}")

    monkeypatch.setattr(
        "casa.extras.cursor._runtime.import_cursor_sdk", lambda: fake_sdk
    )
    monkeypatch.setattr("casa.extras.cursor._runtime.run_prompt", _slow_prompt)

    ex = CursorAgentExecutor(
        CursorConfig(
            enabled=True,
            review_stages=["review-1"],
            api_key="k",
            review_timeout_seconds=0.05,
            cwd="",  # validate "." via runtime
        )
    )
    with pytest.raises(CursorReviewError) as ei:
        await ex.execute(
            "cursor-reviewer",
            {"stage_id": "review-1", "injected_prompt": "x"},
        )
    assert ei.value.code == "cursor_timeout"


def test_parse_failed_artifact_has_hash_not_raw_text():
    secret = "api_key=sk-leak-me-please and more text"
    report = parse_review_text(secret)
    assert "raw_text" not in report.get("_meta", {})
    assert report["_meta"]["raw_text_hash"]
    assert report["_meta"]["raw_text_len"] == len(secret)
    # Ensure secret string itself is not embedded anywhere in artifact
    blob = str(report)
    assert "sk-leak-me-please" not in blob


def test_to_dict_redacts_mcp_servers():
    servers = [
        {
            "name": "svc",
            "env": {"API_TOKEN": "secret"},
            "headers": {"Authorization": "Bearer x"},
        }
    ]
    cfg = CursorConfig(allow_mcp=True, mcp_servers=servers, allow_write=True)
    d = cfg.to_dict()
    assert d["mcp_servers"][0]["env"] == "***"
    assert d["mcp_servers"][0]["headers"] == "***"
    # helper also redacts nested
    red = redact_mcp_servers(servers)
    assert red[0]["env"] == "***"


def test_api_key_not_written_back_to_shared_config():
    shared = CursorConfig(enabled=True, review_stages=["r"], api_key="")
    ex = CursorAgentExecutor(shared, api_key="runtime-secret")
    assert shared.api_key == ""
    assert ex._api_key == "runtime-secret"


@pytest.mark.asyncio
async def test_execute_rejects_mcp_on_review_even_if_allow_mcp():
    cfg = CursorConfig(
        enabled=True,
        review_stages=["review-1"],
        api_key="k",
        allow_mcp=True,
        mcp_servers=[{"name": "x"}],
    )
    ex = CursorAgentExecutor(cfg)
    with pytest.raises(CursorConfigError) as ei:
        await ex.execute(
            "cursor-reviewer",
            {"stage_id": "review-1", "injected_prompt": "x"},
        )
    assert "mcp_servers" in str(ei.value)


def test_from_tenant_policy_auto_create_pr_requires_allow_write():
    with pytest.raises(CursorConfigError):
        CursorConfig.from_tenant_policy(
            {"cursor_review": {"auto_create_pr": True, "allow_write": False}}
        )


def test_content_generator_default_auto_create_pr_false():
    gen = CursorContentGenerator(CursorConfig())
    assert gen.config.auto_create_pr is False
