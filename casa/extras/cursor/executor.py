"""CursorAgentExecutor (core, worker-side) + optional CursorContentGenerator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from ...orchestration.executor import AgentExecutor
from .config import CursorConfig, validate_cwd
from .errors import CURSOR_AUTH, CURSOR_FAILED, CURSOR_TIMEOUT, CursorConfigError, CursorReviewError
from .schema import parse_review_text
from . import _runtime

logger = logging.getLogger("casa.extras.cursor")

_REVIEW_PROMPT_SUFFIX = (
    "\n\nRespond with ONLY a JSON object matching this schema:\n"
    '{"verdict":"pass"|"conditional"|"fail","issues":[{"severity":"low|medium|high|critical",'
    '"location":"string","desc":"string"}],"summary":"string"}'
)


def _inputs_fingerprint(context: dict[str, Any]) -> str:
    payload = {
        "injected_prompt": context.get("injected_prompt"),
        "inputs": context.get("inputs"),
        "eval_targets_data": context.get("eval_targets_data"),
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CursorAgentExecutor(AgentExecutor):
    """
    Worker-side AgentExecutor for Cursor review (read-only).

    LLM calls and enabled/review_stages gating happen **here** (B1-A).
    cursor-sdk is imported only on the enabled execute path (lazy).

    True OS-level read-only for ``runtime=local`` is a **platform** duty
    (bind mount). This executor forces ``cwd_policy=readonly``, path
    containment, forbids ``mcp_servers`` / ``auto_create_pr`` on the review
    path, and records ``inputs_fingerprint`` for audit.

    When ``fail_stage_on`` is used as a security gate, the platform MUST add
    non-LLM hard rules and/or human approval — the library does not treat
    LLM verdicts as prompt-injection-resistant.
    """

    def __init__(
        self,
        config: CursorConfig | None = None,
        *,
        api_key: str = "",
    ):
        self.config = config or CursorConfig()
        # Executor-local only — never write back onto shared CursorConfig (hook-safe).
        self._api_key = str(api_key or "")
        # Advisory process-local cap for external API rate limits ONLY.
        # NOT admission control — never maps to lease reject / global queue without lease.
        self._sem: asyncio.Semaphore | None = None
        if self.config.per_provider_concurrency is not None:
            self._sem = asyncio.Semaphore(int(self.config.per_provider_concurrency))

    def _resolve_api_key(self, context: dict[str, Any]) -> str:
        """
        Resolve Cursor API key for the review path.

        Order: executor-local → ``CursorConfig.api_key``.
        Does **not** fall back to generic ``context["llm_config"]`` unless
        ``llm_config.provider == "cursor"`` (dev-only escape hatch).
        """
        if self._api_key:
            return self._api_key
        if self.config.api_key:
            return self.config.api_key
        llm = context.get("llm_config") or {}
        if isinstance(llm, dict) and str(llm.get("provider") or "").lower() == "cursor":
            # Dev-only: only when provider is explicitly cursor (not other LLM keys).
            key = llm.get("api_key") or ""
            if key:
                logger.debug(
                    "cursor api_key resolved from llm_config (provider=cursor, dev-only)"
                )
                return str(key)
        return ""

    @staticmethod
    def _skip_artifact(*, reason: str) -> dict[str, Any]:
        return {
            "_meta": {"skipped": True, "reason": reason},
            "verdict": "skipped",
            "issues": [],
            "summary": reason,
        }

    async def execute(self, agent_id: str, context: dict[str, Any]) -> dict:
        stage_id = str(context.get("stage_id", "") or "")
        cfg = self.config

        # Gate: enabled + review_stages — no SDK import on short-circuit paths
        if not cfg.enabled:
            return self._skip_artifact(reason="cursor_disabled")
        if not cfg.review_stages or stage_id not in cfg.review_stages:
            return self._skip_artifact(reason="stage_not_in_review_stages")

        # Review path: forbid MCP (even if allow_mcp for a shared config).
        if cfg.mcp_servers:
            raise CursorConfigError(
                "mcp_servers are forbidden on the Cursor review path"
            )

        # Review + local: force readonly policy + path containment.
        cwd_policy = cfg.effective_cwd_policy(for_review=True)
        if cfg.runtime == "local" and cwd_policy != "readonly":
            raise CursorConfigError(
                "review path with runtime=local requires cwd_policy=readonly "
                "(platform must bind-mount read-only; library enforces policy gate)"
            )
        if cfg.cwd:
            validate_cwd(cfg.cwd, allowed_roots=cfg.allowed_cwd_roots or None)

        # Enabled path: lazy-import SDK (raises CursorReviewError if not installed)
        sdk = _runtime.import_cursor_sdk()

        api_key = self._resolve_api_key(context)
        if not api_key:
            raise CursorReviewError(CURSOR_AUTH)

        preference = str(context.get("model_preference") or "")
        model = cfg.resolve_model(agent_id, preference=preference)
        prompt = str(context.get("injected_prompt") or "")
        if not prompt:
            # Fall back to a compact inputs summary for review stages
            inputs = context.get("inputs") or {}
            eval_data = context.get("eval_targets_data") or {}
            prompt = (
                "Review the following stage artifacts and produce a JSON review report.\n"
                f"inputs={inputs!r}\neval_targets_data={eval_data!r}"
            )
        prompt = prompt + _REVIEW_PROMPT_SUFFIX
        inputs_fp = _inputs_fingerprint(context)

        options = _runtime.build_agent_options(
            sdk,
            api_key=api_key,
            model=model,
            runtime=cfg.runtime,
            cwd=cfg.cwd,
            repos=list(cfg.repos),
            skip_reviewer_request=cfg.skip_reviewer_request,
            auto_create_pr=False,  # review path: never open PR
            mcp_servers=None,
            cwd_policy=cwd_policy,
            allowed_cwd_roots=list(cfg.allowed_cwd_roots) or None,
            for_review=True,
        )

        async def _call() -> Any:
            try:
                return await asyncio.to_thread(_runtime.run_prompt, sdk, prompt, options)
            except CursorReviewError:
                raise
            except Exception as exc:
                # Map SDK errors to fixed codes; log detail, never leak SDK text publicly.
                name = type(exc).__name__
                logger.debug(
                    "cursor review SDK failure type=%s detail=%r",
                    name,
                    exc,
                    exc_info=True,
                )
                raise CursorReviewError(CURSOR_FAILED) from exc

        timeout = float(cfg.review_timeout_seconds or 120.0)

        async def _run_with_sem() -> Any:
            if self._sem is not None:
                # Wait/backoff only (external rate-limit advisory) — not lease admission.
                async with self._sem:
                    return await _call()
            return await _call()

        try:
            run_result = await asyncio.wait_for(_run_with_sem(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            logger.debug(
                "cursor review timed out after %ss stage_id=%s", timeout, stage_id
            )
            raise CursorReviewError(CURSOR_TIMEOUT) from exc

        status = getattr(run_result, "status", None)
        if status == "error":
            logger.debug(
                "cursor run status=error run_id=%r", getattr(run_result, "id", "")
            )
            raise CursorReviewError(CURSOR_FAILED)

        text = _runtime.result_text(run_result)
        report = parse_review_text(text)
        report.setdefault("_meta", {})
        if isinstance(report["_meta"], dict):
            report["_meta"].update(
                {
                    "provider": "cursor",
                    "agent_id": agent_id,
                    "stage_id": stage_id,
                    "model": model,
                    "runtime": cfg.runtime,
                    "cwd_policy": cwd_policy,
                    "run_id": getattr(run_result, "id", "") or "",
                    "inputs_fingerprint": inputs_fp,
                    "auto_create_pr": False,
                }
            )
            report["_meta"].pop("raw_text", None)
        return report


class CursorContentGenerator:
    """
    Optional one-shot text helper (non-primary path).

    Thin Agent.prompt wrapper for worker-side generate-without-orchestration.
    Not part of the primary review acceptance path.

    Defaults: ``auto_create_pr=False``. PR creation is only allowed when the
    shared config has ``allow_write=True`` **and** ``auto_create_pr=True``.
    """

    def __init__(self, config: CursorConfig | None = None, *, api_key: str = ""):
        self.config = config or CursorConfig()
        # Executor-local — do not mutate shared config.
        self._api_key = str(api_key or "")

    def generate(self, prompt: str, *, model: str = "", api_key: str = "") -> str:
        sdk = _runtime.import_cursor_sdk()
        key = api_key or self._api_key or self.config.api_key
        if not key:
            raise CursorReviewError(CURSOR_AUTH)
        mid = model or self.config.default_model or "composer-2.5"
        # Generation path: auto_create_pr only when allow_write is explicit.
        auto_pr = bool(self.config.auto_create_pr) and bool(self.config.allow_write)
        mcp = list(self.config.mcp_servers) if self.config.allow_mcp else None
        if self.config.mcp_servers and not self.config.allow_mcp:
            raise CursorConfigError(
                "mcp_servers require allow_mcp=True on the generation path"
            )
        options = _runtime.build_agent_options(
            sdk,
            api_key=key,
            model=mid,
            runtime=self.config.runtime,
            cwd=self.config.cwd,
            repos=list(self.config.repos),
            skip_reviewer_request=self.config.skip_reviewer_request,
            auto_create_pr=auto_pr,
            mcp_servers=mcp or None,
            cwd_policy=self.config.effective_cwd_policy(for_review=False),
            allowed_cwd_roots=list(self.config.allowed_cwd_roots) or None,
            for_review=False,
        )
        try:
            result = _runtime.run_prompt(sdk, prompt, options)
        except CursorReviewError:
            raise
        except Exception as exc:
            logger.debug("cursor generate failed detail=%r", exc, exc_info=True)
            raise CursorReviewError(CURSOR_FAILED) from exc
        return _runtime.result_text(result)

    async def generate_async(self, prompt: str, *, model: str = "", api_key: str = "") -> str:
        return await asyncio.to_thread(
            self.generate, prompt, model=model, api_key=api_key
        )
