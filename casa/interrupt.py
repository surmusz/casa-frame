"""
CASA Interrupt Controller — 运行时暂停/恢复/终止信号。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InterruptSignal(Enum):
    NONE = "none"
    PAUSE_AFTER_WAVE = "pause_after_wave"
    PAUSE_AFTER_STAGE = "pause_after_stage"
    ABORT_GRACEFUL = "abort_graceful"
    ABORT_IMMEDIATE = "abort_immediate"


@dataclass
class InterruptState:
    signal: InterruptSignal = InterruptSignal.NONE
    reason: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class InterruptController:
    """
    轻量信号控制器——PlanExecutor 在波次/阶段边界轮询。
    """

    def __init__(self) -> None:
        self._state = InterruptState()
        self._resume_event = asyncio.Event()
        self._resume_event.set()

    def signal(self, sig: InterruptSignal, *, reason: str = "", source: str = "") -> None:
        self._state = InterruptState(signal=sig, reason=reason, source=source)
        if sig in (InterruptSignal.PAUSE_AFTER_WAVE, InterruptSignal.PAUSE_AFTER_STAGE):
            self._resume_event.clear()

    def pause(self, reason: str = "", *, after: str = "wave") -> None:
        sig = (
            InterruptSignal.PAUSE_AFTER_WAVE
            if after == "wave"
            else InterruptSignal.PAUSE_AFTER_STAGE
        )
        self.signal(sig, reason=reason)

    def resume(self) -> None:
        self._state = InterruptState()
        self._resume_event.set()

    def abort(self, reason: str = "", *, graceful: bool = True) -> None:
        sig = InterruptSignal.ABORT_GRACEFUL if graceful else InterruptSignal.ABORT_IMMEDIATE
        self.signal(sig, reason=reason)
        if not graceful:
            self._resume_event.set()

    def check(self) -> InterruptState:
        return self._state

    async def wait_if_paused(self) -> None:
        await self._resume_event.wait()

    @property
    def is_paused(self) -> bool:
        return self._state.signal in (
            InterruptSignal.PAUSE_AFTER_WAVE,
            InterruptSignal.PAUSE_AFTER_STAGE,
        )

    def should_abort_after_wave(self) -> bool:
        return self._state.signal == InterruptSignal.ABORT_GRACEFUL
