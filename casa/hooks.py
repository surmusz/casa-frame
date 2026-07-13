"""
CASA Pipeline 钩子 — compile/execute/stage 生命周期扩展点。
"""

from __future__ import annotations

import abc
import asyncio
import inspect
import logging
import threading
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestration import CompileRequest, CompileResult, Plan, Stage, StageResult

logger = logging.getLogger("casa.hooks")


class PipelineHook(abc.ABC):
    async def on_compile_start(self, request: Any) -> None:
        pass

    async def on_compile_end(self, result: Any) -> None:
        pass

    async def on_normalize_start(self, plan: Any) -> None:
        pass

    async def on_normalize_end(self, plan: Any) -> None:
        pass

    async def on_execute_start(self, plan: Any) -> None:
        pass

    async def on_execute_end(self, plan: Any, results: dict[str, Any]) -> None:
        pass

    async def on_stage_start(self, stage: Any, plan: Any) -> None:
        pass

    async def on_stage_end(self, stage: Any, result: Any) -> None:
        pass

    async def on_stage_error(self, stage: Any, error: Exception) -> None:
        pass

    async def on_wave_start(self, wave_stages: list[Any]) -> None:
        pass

    async def on_wave_end(self, wave_stages: list[Any], results: dict[str, Any]) -> None:
        pass


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: list[tuple[int, PipelineHook]] = []
        self._lock = threading.Lock()

    def register(self, hook: PipelineHook, *, priority: int = 100) -> None:
        with self._lock:
            self._hooks.append((priority, hook))
            self._hooks.sort(key=lambda x: x[0])

    async def fire(self, event: str, **kwargs: Any) -> None:
        method_map = {
            "compile_start": "on_compile_start",
            "compile_end": "on_compile_end",
            "normalize_start": "on_normalize_start",
            "normalize_end": "on_normalize_end",
            "execute_start": "on_execute_start",
            "execute_end": "on_execute_end",
            "stage_start": "on_stage_start",
            "stage_end": "on_stage_end",
            "stage_error": "on_stage_error",
            "wave_start": "on_wave_start",
            "wave_end": "on_wave_end",
        }
        method_name = method_map.get(event)
        if not method_name:
            return
        with self._lock:
            hooks = list(self._hooks)
        for _, hook in hooks:
            method = getattr(hook, method_name)
            try:
                if event in ("compile_start",):
                    result = method(kwargs["request"])
                elif event in ("compile_end",):
                    result = method(kwargs["result"])
                elif event in ("normalize_start", "normalize_end"):
                    result = method(kwargs["plan"])
                elif event in ("execute_start",):
                    result = method(kwargs["plan"])
                elif event in ("execute_end",):
                    result = method(kwargs["plan"], kwargs["results"])
                elif event in ("stage_start",):
                    result = method(kwargs["stage"], kwargs["plan"])
                elif event in ("stage_end",):
                    result = method(kwargs["stage"], kwargs["result"])
                elif event in ("stage_error",):
                    result = method(kwargs["stage"], kwargs["error"])
                elif event in ("wave_start",):
                    result = method(kwargs["wave_stages"])
                elif event in ("wave_end",):
                    result = method(kwargs["wave_stages"], kwargs["results"])
                else:
                    result = None
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Hook %s.%s failed", type(hook).__name__, method_name, exc_info=True)
