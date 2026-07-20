"""Thin cursor-sdk adapter — only imported from the enabled execute path."""

from __future__ import annotations

from typing import Any

from .config import validate_cwd
from .errors import CursorConfigError, CursorReviewError

_INSTALL_HINT = "pip install casa-frame[cursor]"


def import_cursor_sdk() -> Any:
    """Lazy-import cursor-sdk; raise CursorReviewError with install hint if missing."""
    try:
        import cursor_sdk  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CursorReviewError(
            f"cursor-sdk is required for Cursor review. Install with: {_INSTALL_HINT}",
            code="cursor_failed",
        ) from exc
    return cursor_sdk


def build_agent_options(
    sdk: Any,
    *,
    api_key: str,
    model: str,
    runtime: str,
    cwd: str,
    repos: list[Any],
    skip_reviewer_request: bool,
    auto_create_pr: bool,
    mcp_servers: list[Any] | None = None,
    cwd_policy: str = "readonly",
    allowed_cwd_roots: list[str] | None = None,
    for_review: bool = True,
) -> Any:
    """
    Build AgentOptions.

    Library path containment + ``cwd_policy`` gate only. True OS-level read-only
    (bind mount) is a **platform assembly** responsibility — this adapter does
    not mount filesystems.
    """
    AgentOptions = sdk.AgentOptions
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "model": model,
    }
    if mcp_servers:
        if for_review:
            raise CursorConfigError("mcp_servers are forbidden on the Cursor review path")
        kwargs["mcp_servers"] = mcp_servers
    if runtime == "cloud":
        CloudAgentOptions = sdk.CloudAgentOptions
        # Review path: never open PR regardless of caller flag.
        pr_flag = False if for_review else bool(auto_create_pr)
        try:
            kwargs["cloud"] = CloudAgentOptions(
                repos=list(repos),
                auto_create_pr=pr_flag,
                skip_reviewer_request=bool(skip_reviewer_request),
            )
        except TypeError:
            # Older SDK without skip_reviewer_request
            kwargs["cloud"] = CloudAgentOptions(
                repos=list(repos),
                auto_create_pr=pr_flag,
            )
    else:
        # local runtime
        effective_policy = cwd_policy or "readonly"
        if for_review:
            # Review path forces readonly policy (allow_write only on generate).
            effective_policy = "readonly"
        elif effective_policy != "readonly" and effective_policy not in (
            "scratch",
            "repo-subtree",
        ):
            raise CursorConfigError(f"invalid cwd_policy: {effective_policy!r}")

        # Containment: resolve + reject .. + optional allowlist.
        # Platform must bind-mount readonly when policy is readonly.
        safe_cwd = validate_cwd(cwd or ".", allowed_roots=allowed_cwd_roots or None)
        LocalAgentOptions = sdk.LocalAgentOptions
        kwargs["local"] = LocalAgentOptions(cwd=safe_cwd or ".")
        # cwd_policy is recorded for callers/audit; SDK has no native readonly flag.
        kwargs["_casa_cwd_policy"] = effective_policy  # may be stripped if AgentOptions strict
    try:
        return AgentOptions(**{k: v for k, v in kwargs.items() if not k.startswith("_casa_")})
    except TypeError:
        # Retry without unknown keys if SDK is strict (shouldn't include _casa_).
        clean = {k: v for k, v in kwargs.items() if not k.startswith("_casa_")}
        return AgentOptions(**clean)


def run_prompt(sdk: Any, prompt: str, options: Any) -> Any:
    """One-shot Agent.prompt (sync); caller should wrap with asyncio.to_thread."""
    return sdk.Agent.prompt(prompt, options)


def result_text(run_result: Any) -> str:
    """Extract assistant text from RunResult / prompt return value."""
    if run_result is None:
        return ""
    if isinstance(run_result, str):
        return run_result
    for attr in ("result", "text", "output"):
        val = getattr(run_result, attr, None)
        if isinstance(val, str) and val:
            return val
    status = getattr(run_result, "status", None)
    if status == "error":
        return ""
    # Some SDK shapes nest message content
    msg = getattr(run_result, "message", None)
    if isinstance(msg, str):
        return msg
    return str(run_result) if run_result else ""
