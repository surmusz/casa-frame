"""JSON schema and parsers for Cursor review reports."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

logger = logging.getLogger("casa.extras.cursor")

VERDICTS = frozenset({"pass", "conditional", "fail"})

REVIEW_REPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "casa.extras.cursor.review_report",
    "title": "CursorReviewReport",
    "type": "object",
    "required": ["verdict", "issues", "summary"],
    "additionalProperties": True,
    "properties": {
        "verdict": {
            "type": "string",
            "enum": sorted(VERDICTS),
            "description": "Review outcome",
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "location", "desc"],
                "additionalProperties": True,
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                    "location": {"type": "string"},
                    "desc": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}

_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)

_SECRET_LIKE_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+"
)


def _redact_for_log(text: str) -> str:
    return _SECRET_LIKE_RE.sub(r"\1=***", text)


def _fallback_parse_failed(raw_text: str) -> dict[str, Any]:
    """
    Parse-failure artifact without persisting raw model text.

    ``raw_text`` is logged (redacted) for audit only; the durable artifact keeps
    hash + length for correlation.
    """
    text = raw_text or ""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    logger.debug(
        "cursor review parse_failed raw_text_hash=%s raw_text_len=%s preview=%r",
        digest,
        len(text),
        _redact_for_log(text[:500]),
    )
    return {
        "verdict": "conditional",
        "issues": [],
        "summary": "review_parse_failed",
        "_meta": {
            "parse_failed": True,
            "review_parse_failed": True,
            "raw_text_hash": digest,
            "raw_text_len": len(text),
        },
    }


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extract of a JSON object from model text."""
    if not text or not str(text).strip():
        return None
    raw = str(text).strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE_RE.search(raw)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def normalize_review_report(data: dict[str, Any], *, raw_text: str = "") -> dict[str, Any]:
    """Validate/normalize a review dict; invalid → conditional + parse_failed."""
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return _fallback_parse_failed(raw_text or json.dumps(data, ensure_ascii=False))
    issues_raw = data.get("issues", [])
    if not isinstance(issues_raw, list):
        return _fallback_parse_failed(raw_text or json.dumps(data, ensure_ascii=False))
    issues: list[dict[str, Any]] = []
    for item in issues_raw:
        if not isinstance(item, dict):
            continue
        issues.append(
            {
                "severity": str(item.get("severity", "medium")),
                "location": str(item.get("location", "")),
                "desc": str(item.get("desc", item.get("description", ""))),
            }
        )
    out: dict[str, Any] = {
        "verdict": verdict,
        "issues": issues,
        "summary": str(data.get("summary", "")),
    }
    meta = data.get("_meta")
    if isinstance(meta, dict):
        # Never persist raw_text if a caller smuggled it in.
        cleaned = {k: v for k, v in meta.items() if k != "raw_text"}
        out["_meta"] = cleaned
    return out


def parse_review_text(text: str) -> dict[str, Any]:
    """Parse assistant text into a review report; never raises on bad JSON."""
    obj = extract_json_object(text)
    if obj is None:
        return _fallback_parse_failed(text)
    return normalize_review_report(obj, raw_text=text)
