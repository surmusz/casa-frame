"""CursorConfig — runtime carrier for Cursor optional extra (no secret persistence)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import CursorConfigError

# owner/name (GitHub-style); optional leading host stripped in validator.
_REPO_OWNER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "auth",
    "credential",
    "header",
)


def _path_has_dotdot(cwd: str) -> bool:
    """True if any path component is ``..`` (before resolve)."""
    try:
        parts = Path(cwd).parts
    except (TypeError, ValueError):
        return True
    return ".." in parts


def validate_cwd(
    cwd: str,
    *,
    allowed_roots: list[str] | None = None,
) -> str:
    """
    Normalize ``cwd`` with resolve + reject ``..`` escape.

    When ``allowed_roots`` is non-empty, resolved path must be under one root.
    Without allowlist, only non-escape containment is enforced.

    Note: true OS-level read-only is a **platform** responsibility (bind mount).
    The library only does path containment and policy gates.
    """
    if not cwd:
        return ""
    raw = str(cwd)
    if _path_has_dotdot(raw):
        raise CursorConfigError(f"cwd must not contain '..' components: {cwd!r}")
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise CursorConfigError(f"cwd could not be resolved: {cwd!r}") from exc
    # After resolve, reject if still somehow outside (symlink races are platform concern)
    if allowed_roots:
        roots = []
        for root in allowed_roots:
            if not root:
                continue
            if _path_has_dotdot(str(root)):
                raise CursorConfigError(f"allowed_cwd_roots entry invalid: {root!r}")
            roots.append(Path(str(root)).expanduser().resolve())
        if roots and not any(_is_under(resolved, root) for root in roots):
            raise CursorConfigError(
                f"cwd {str(resolved)!r} is outside allowed_cwd_roots"
            )
    return str(resolved)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_repo_ref(item: Any) -> str:
    if isinstance(item, dict):
        owner = str(item.get("owner") or "").strip()
        name = str(item.get("name") or "").strip()
        if owner and name:
            return f"{owner}/{name}"
        for key in ("repo", "repository", "url", "slug"):
            val = item.get(key)
            if val:
                return _strip_repo_host(str(val).strip())
        raise CursorConfigError(f"invalid repos entry (need owner/name): {item!r}")
    return _strip_repo_host(str(item).strip())


def _strip_repo_host(ref: str) -> str:
    s = ref
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
        "github.com/",
    ):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
            break
    s = s.removesuffix(".git").strip("/")
    return s


def validate_repos(
    repos: list[Any],
    *,
    allowlist: list[str] | None = None,
) -> list[Any]:
    """Validate each repo as owner/name (+ optional allowlist). Returns original list."""
    if not repos:
        return []
    normalized_allow: set[str] | None = None
    if allowlist:
        normalized_allow = {_strip_repo_host(str(x)) for x in allowlist if x}
    out: list[Any] = []
    for item in repos:
        ref = _normalize_repo_ref(item)
        if not _REPO_OWNER_NAME_RE.match(ref):
            raise CursorConfigError(
                f"invalid repos format (expected owner/name): {item!r}"
            )
        if normalized_allow is not None and ref not in normalized_allow:
            raise CursorConfigError(f"repos entry not in allowlist: {ref!r}")
        out.append(item)
    return out


def redact_mcp_servers(servers: list[Any]) -> list[Any]:
    """Recursively redact env/headers/token-like keys from mcp_servers for to_dict/audit."""

    def _sensitive(key: str) -> bool:
        kl = key.lower()
        if kl in ("env", "headers", "token", "api_key", "authorization", "password"):
            return True
        return any(frag in kl for frag in _SENSITIVE_KEY_FRAGMENTS)

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: ("***" if _sensitive(str(k)) else _walk(v)) for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_walk(x) for x in obj]
        return obj

    return _walk(list(servers))  # type: ignore[return-value]


@dataclass
class CursorConfig:
    """
    Library-side Cursor review config.

    Credentials are runtime-injected only (``api_key``); this object does not
    persist secrets. Tenant policy is injected by the platform worker via
    :meth:`from_tenant_policy` — the library never reads mgmt.

    Path / write policy
    -------------------
    True OS-level read-only for local review is a **platform assembly** duty
    (e.g. bind-mount the workspace read-only). The library enforces path
    containment (resolve, reject ``..``, optional root allowlist) and policy
    gates (``cwd_policy`` / ``allow_write`` / ``allow_mcp``).

    ``fail_stage_on`` as a security gate
    ------------------------------------
    When using ``fail_stage_on`` as a security control, the platform MUST layer
    non-LLM hard rules and/or human approval. The library does **not** treat
    LLM verdicts as resistant to prompt injection.
    """

    enabled: bool = False
    review_stages: list[str] = field(default_factory=list)
    # Stages where verdict fail/conditional should mark StageResult.success=False.
    # Empty (default) = audit-only for all stages.
    # Security: do not rely on LLM verdict alone — platform must add hard rules.
    fail_stage_on: list[str] = field(default_factory=list)
    runtime: str = "local"  # "local" | "cloud"
    cwd: str = ""
    repos: list[Any] = field(default_factory=list)
    cwd_policy: str = "readonly"  # scratch | readonly | repo-subtree
    # Optional containment roots (empty = resolve + reject .. only).
    allowed_cwd_roots: list[str] = field(default_factory=list)
    # Optional repos allowlist (empty = schema-only).
    allowed_repos: list[str] = field(default_factory=list)
    model_aliases: dict[str, str] = field(default_factory=dict)
    default_model: str = "composer-2.5"
    # Runtime injection only — never persisted; excluded from repr.
    api_key: str = field(default="", repr=False)
    mcp_servers: list[Any] = field(default_factory=list)
    skip_reviewer_request: bool = True
    auto_create_pr: bool = False
    # Generation-path only flags (review path ignores / forbids).
    allow_write: bool = False
    allow_mcp: bool = False
    # Advisory process-local cap for external API rate limits ONLY.
    # NOT admission control — never lease reject / global queue without lease.
    per_provider_concurrency: int | None = None
    # Review execute timeout (seconds). Maps to CursorReviewError("cursor_timeout").
    review_timeout_seconds: float = 120.0
    # rate_limit: L0 deferred — not implemented; do not expose in to_dict.

    def resolve_model(self, agent_id: str, *, preference: str = "") -> str:
        """Resolve model id via aliases; ``default_model`` is fallback only."""
        if preference and preference in self.model_aliases:
            return self.model_aliases[preference]
        if preference and "/" not in preference and preference not in (
            "simple",
            "hard",
            "reviewer",
            "architect",
        ):
            # Treat concrete model ids as-is when not an alias key.
            if preference not in self.model_aliases:
                return preference
        for key in (agent_id, preference, "reviewer"):
            if key and key in self.model_aliases:
                return self.model_aliases[key]
        return self.default_model or "composer-2.5"

    def should_review_stage(self, stage_id: str) -> bool:
        if not self.enabled:
            return False
        if not self.review_stages:
            return False
        return stage_id in self.review_stages

    def should_fail_stage(self, stage_id: str, verdict: str) -> bool:
        if verdict not in ("fail", "conditional"):
            return False
        if not self.fail_stage_on:
            return False
        return stage_id in self.fail_stage_on

    def effective_cwd_policy(self, *, for_review: bool = True) -> str:
        """
        Review + local runtime forces ``readonly``.

        ``allow_write`` is only meaningful on the generation path.
        """
        if for_review and self.runtime == "local":
            return "readonly"
        if self.allow_write and not for_review:
            return self.cwd_policy or "scratch"
        return self.cwd_policy or "readonly"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "review_stages": list(self.review_stages),
            "fail_stage_on": list(self.fail_stage_on),
            "runtime": self.runtime,
            "cwd": self.cwd,
            "repos": list(self.repos),
            "cwd_policy": self.cwd_policy,
            "allowed_cwd_roots": list(self.allowed_cwd_roots),
            "allowed_repos": list(self.allowed_repos),
            "model_aliases": dict(self.model_aliases),
            "default_model": self.default_model,
            "api_key": "***" if self.api_key else "",
            "mcp_servers": redact_mcp_servers(list(self.mcp_servers)),
            "skip_reviewer_request": self.skip_reviewer_request,
            "auto_create_pr": self.auto_create_pr,
            "allow_write": self.allow_write,
            "allow_mcp": self.allow_mcp,
            "per_provider_concurrency": self.per_provider_concurrency,
            "review_timeout_seconds": self.review_timeout_seconds,
            # rate_limit omitted: L0 deferred / not implemented
        }

    @classmethod
    def from_tenant_policy(cls, policy: dict[str, Any]) -> CursorConfig:
        """
        Inject point for platform workers.

        Accepts either ``{"cursor_review": {...}}`` or a flat cursor_review dict.
        Library does not read mgmt — caller supplies the effective policy dict.

        Validates ``cwd`` / ``repos``; rejects ``mcp_servers`` unless
        ``allow_mcp=True`` (generation path); ``auto_create_pr`` only when
        ``allow_write=True``.
        """
        if not isinstance(policy, dict):
            raise CursorConfigError("tenant policy must be a dict")
        cr = policy.get("cursor_review", policy)
        if not isinstance(cr, dict):
            raise CursorConfigError("cursor_review policy must be a dict")
        runtime = str(cr.get("runtime", "local") or "local").lower()
        if runtime not in ("local", "cloud"):
            raise CursorConfigError(f"invalid runtime: {runtime!r}")
        ppc = cr.get("per_provider_concurrency", None)
        if ppc is not None:
            try:
                ppc = int(ppc)
            except (TypeError, ValueError) as exc:
                raise CursorConfigError(
                    f"per_provider_concurrency must be int|None: {ppc!r}"
                ) from exc
            if ppc < 1:
                raise CursorConfigError("per_provider_concurrency must be >= 1 when set")

        allow_write = bool(cr.get("allow_write", False))
        allow_mcp = bool(cr.get("allow_mcp", False))
        mcp_servers = list(cr.get("mcp_servers", []) or [])
        if mcp_servers and not allow_mcp:
            raise CursorConfigError(
                "mcp_servers forbidden unless allow_mcp=True (generation path only)"
            )

        auto_create_pr = bool(cr.get("auto_create_pr", False))
        if auto_create_pr and not allow_write:
            raise CursorConfigError(
                "auto_create_pr requires allow_write=True (generation path only)"
            )
        if not allow_write:
            auto_create_pr = False

        allowed_cwd_roots = [str(x) for x in list(cr.get("allowed_cwd_roots", []) or [])]
        allowed_repos = [str(x) for x in list(cr.get("allowed_repos", []) or [])]
        cwd_raw = str(cr.get("cwd", "") or "")
        cwd = validate_cwd(cwd_raw, allowed_roots=allowed_cwd_roots or None) if cwd_raw else ""
        repos = validate_repos(
            list(cr.get("repos", []) or []),
            allowlist=allowed_repos or None,
        )

        cwd_policy = str(cr.get("cwd_policy", "readonly") or "readonly")
        # Review-oriented default: local runtime stays readonly unless generation.
        if runtime == "local" and not allow_write:
            cwd_policy = "readonly"

        timeout = cr.get("review_timeout_seconds", 120.0)
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError) as exc:
            raise CursorConfigError(
                f"review_timeout_seconds must be float: {timeout!r}"
            ) from exc
        if timeout_f <= 0:
            raise CursorConfigError("review_timeout_seconds must be > 0")

        return cls(
            enabled=bool(cr.get("enabled", False)),
            review_stages=[str(x) for x in list(cr.get("review_stages", []) or [])],
            fail_stage_on=[str(x) for x in list(cr.get("fail_stage_on", []) or [])],
            runtime=runtime,
            cwd=cwd,
            repos=repos,
            cwd_policy=cwd_policy,
            allowed_cwd_roots=allowed_cwd_roots,
            allowed_repos=allowed_repos,
            model_aliases={
                str(k): str(v) for k, v in dict(cr.get("model_aliases", {}) or {}).items()
            },
            default_model=str(cr.get("default_model", "composer-2.5") or "composer-2.5"),
            # api_key intentionally NOT taken from persisted policy
            api_key="",
            mcp_servers=mcp_servers,
            skip_reviewer_request=bool(cr.get("skip_reviewer_request", True)),
            auto_create_pr=auto_create_pr,
            allow_write=allow_write,
            allow_mcp=allow_mcp,
            per_provider_concurrency=ppc,
            review_timeout_seconds=timeout_f,
        )
