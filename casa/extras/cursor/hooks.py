"""CursorReviewHook — non-LLM pipeline signal (no SDK import / no API key)."""

from __future__ import annotations

import logging
from typing import Any, Callable

from ...audit import emit_audit
from ...hooks import PipelineHook
from .config import CursorConfig

logger = logging.getLogger("casa.extras.cursor")

ArtifactReader = Callable[[str], dict[str, Any] | None]

# Recognized Cursor reviewer agent ids (provenance).
_CURSOR_REVIEWER_IDS = frozenset(
    {
        "cursor-reviewer",
        "cursor_reviewer",
        "cursor-review",
    }
)


class CursorReviewHook(PipelineHook):
    """
    Optional, non-LLM hook.

    Reads review artifact on ``on_stage_end``, emits audit for fail/conditional,
    and optionally marks the stage failed when ``fail_stage_on`` contains the
    stage. Never imports the Cursor SDK and never holds CURSOR_API_KEY.

    Gates
    -----
    * ``enabled`` must be true
    * ``stage_id`` must be in ``review_stages`` (else strict no-op)
    * Artifact must carry Cursor provenance (``_meta.provider=="cursor"`` or
      reviewer ``agent_id``); fail decisions only trust provenance-backed artifacts.

    Security note: when ``fail_stage_on`` is used as a gate, the platform must
    layer non-LLM hard rules / human approval — LLM verdicts are not injection-safe.
    """

    def __init__(
        self,
        config: CursorConfig | None = None,
        *,
        store: Any | None = None,
        artifact_reader: ArtifactReader | None = None,
    ):
        self.config = config or CursorConfig()
        self._store = store
        self._artifact_reader = artifact_reader
        # Test / observability: last actions taken
        self.last_actions: list[dict[str, Any]] = []

    def _read_artifact(self, stage: Any, result: Any) -> dict[str, Any] | None:
        # Prefer explicit artifact_data on result (tests / thin adapters)
        data = getattr(result, "artifact_data", None)
        if isinstance(data, dict):
            return data
        ak = (
            getattr(result, "artifact_kind", "")
            or getattr(stage, "output_artifact_kind", "")
            or ""
        )
        if self._artifact_reader and ak:
            got = self._artifact_reader(str(ak))
            if isinstance(got, dict):
                return got
        if self._store is not None and ak and hasattr(self._store, "read"):
            got = self._store.read(str(ak))
            if isinstance(got, dict):
                return got
        # Executor may have left verdict on a dict-like result
        if isinstance(result, dict) and "verdict" in result:
            return result
        return None

    @staticmethod
    def _has_cursor_provenance(
        artifact: dict[str, Any],
        stage: Any,
        result: Any,
    ) -> bool:
        meta = artifact.get("_meta")
        if isinstance(meta, dict) and str(meta.get("provider") or "").lower() == "cursor":
            return True
        agent_id = ""
        if isinstance(meta, dict):
            agent_id = str(meta.get("agent_id") or "")
        if not agent_id:
            agent_id = str(
                getattr(result, "agent_id", "")
                or getattr(stage, "agent_id", "")
                or ""
            )
        return agent_id in _CURSOR_REVIEWER_IDS

    async def on_stage_end(self, stage: Any, result: Any) -> None:
        cfg = self.config
        # enabled=false → no-op even if registered
        if not cfg.enabled:
            self.last_actions.append({"action": "noop", "reason": "disabled"})
            return

        stage_id = str(getattr(stage, "stage_id", "") or "")
        # B-1 / CUR-SEC-004: only act on configured review stages
        if not cfg.should_review_stage(stage_id):
            self.last_actions.append(
                {
                    "action": "noop",
                    "reason": "stage_not_in_review_stages",
                    "stage_id": stage_id,
                }
            )
            return

        artifact = self._read_artifact(stage, result)
        if not artifact:
            self.last_actions.append(
                {"action": "noop", "reason": "no_artifact", "stage_id": stage_id}
            )
            return

        # Provenance: fail/audit only for Cursor-produced review artifacts
        if not self._has_cursor_provenance(artifact, stage, result):
            self.last_actions.append(
                {
                    "action": "noop",
                    "reason": "missing_cursor_provenance",
                    "stage_id": stage_id,
                }
            )
            return

        verdict = str(artifact.get("verdict", "") or "").lower()
        if verdict in ("", "skipped", "pass"):
            if verdict == "pass":
                emit_audit(
                    "cursor.review",
                    actor="cursor-review-hook",
                    stage_id=stage_id,
                    verdict=verdict,
                    mode="audit",
                    summary=str(artifact.get("summary", "")),
                    inputs_fingerprint=(
                        (artifact.get("_meta") or {}).get("inputs_fingerprint")
                        if isinstance(artifact.get("_meta"), dict)
                        else None
                    ),
                )
                self.last_actions.append(
                    {"action": "audit", "stage_id": stage_id, "verdict": verdict}
                )
            else:
                self.last_actions.append(
                    {"action": "noop", "reason": f"verdict={verdict}", "stage_id": stage_id}
                )
            return

        # fail / conditional → always audit; fail stage only if fail_stage_on matches
        mark_fail = cfg.should_fail_stage(stage_id, verdict)
        mode = "fail_stage" if mark_fail else "audit_only"
        meta = artifact.get("_meta") if isinstance(artifact.get("_meta"), dict) else {}
        emit_audit(
            "cursor.review",
            actor="cursor-review-hook",
            stage_id=stage_id,
            verdict=verdict,
            mode=mode,
            summary=str(artifact.get("summary", "")),
            issues=list(artifact.get("issues") or []),
            inputs_fingerprint=meta.get("inputs_fingerprint"),
        )
        if mark_fail and hasattr(result, "success"):
            result.success = False
            if hasattr(result, "error"):
                result.error = (
                    getattr(result, "error", "")
                    or f"cursor review verdict={verdict}"
                )
            self.last_actions.append(
                {
                    "action": "mark_fail",
                    "stage_id": stage_id,
                    "verdict": verdict,
                    "mode": mode,
                }
            )
        else:
            self.last_actions.append(
                {
                    "action": "audit_only",
                    "stage_id": stage_id,
                    "verdict": verdict,
                    "mode": mode,
                }
            )
