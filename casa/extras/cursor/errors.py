"""Cursor extra errors."""

from __future__ import annotations

# Stable public error codes (no SDK message leakage).
CURSOR_AUTH = "cursor_auth"
CURSOR_TIMEOUT = "cursor_timeout"
CURSOR_FAILED = "cursor_failed"

_KNOWN_CODES = frozenset({CURSOR_AUTH, CURSOR_TIMEOUT, CURSOR_FAILED})


class CursorConfigError(ValueError):
    """Invalid CursorConfig / tenant policy injection."""


class CursorReviewError(RuntimeError):
    """
    Cursor review path failed (missing SDK, auth, timeout, or run failure).

    Public ``str(exc)`` / ``exc.code`` use fixed codes only for mapped failures;
    SDK/exception detail belongs in debug logs, not the public message.
    """

    def __init__(
        self,
        message_or_code: str = CURSOR_FAILED,
        *,
        code: str | None = None,
    ):
        if code is not None:
            self.code = code
            super().__init__(message_or_code)
        elif message_or_code in _KNOWN_CODES:
            self.code = message_or_code
            super().__init__(message_or_code)
        else:
            self.code = CURSOR_FAILED
            super().__init__(message_or_code)
