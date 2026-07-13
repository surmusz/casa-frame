"""
CASA 恢复 — stage 执行的可插拔错误恢复策略。
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestration import Stage, StageResult


@dataclass
class RecoveryContext:
    stage_id: str = ""
    agent_id: str = ""
    artifact_kind: str = ""
    attempt_num: int = 0
    fresh_session: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecoveryResult:
    action: str  # retry | fallback | skip | fail
    delay_seconds: float = 0.0
    modified_params: dict[str, Any] | None = None
    error: str = ""


class RecoveryStrategy(abc.ABC):
    @abc.abstractmethod
    async def attempt(
        self,
        stage: Any,
        error: Exception,
        attempt_num: int,
        context: RecoveryContext,
    ) -> RecoveryResult:
        ...


class SimpleRetryStrategy(RecoveryStrategy):
    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries

    async def attempt(self, stage: Any, error: Exception, attempt_num: int, context: RecoveryContext) -> RecoveryResult:
        if attempt_num < self.max_retries:
            return RecoveryResult(action="retry")
        return RecoveryResult(action="fail", error=str(error))


class ExponentialBackoffStrategy(RecoveryStrategy):
    def __init__(self, base_delay: float = 1.0, max_retries: int = 3):
        self.base_delay = base_delay
        self.max_retries = max_retries

    async def attempt(self, stage: Any, error: Exception, attempt_num: int, context: RecoveryContext) -> RecoveryResult:
        if attempt_num < self.max_retries:
            return RecoveryResult(action="retry", delay_seconds=self.base_delay * (2 ** attempt_num))
        return RecoveryResult(action="fail", error=str(error))


class FreshSessionStrategy(RecoveryStrategy):
    def __init__(self, max_attempts: int = 1):
        self.max_attempts = max_attempts

    async def attempt(self, stage: Any, error: Exception, attempt_num: int, context: RecoveryContext) -> RecoveryResult:
        tries = int(context.extra.get("fresh_session_tries", 0))
        if not context.fresh_session and tries < self.max_attempts:
            context.fresh_session = True
            context.extra["fresh_session_tries"] = tries + 1
            return RecoveryResult(action="retry", modified_params={"fresh_session": True})
        return RecoveryResult(action="fail", error=str(error))


class SkipStrategy(RecoveryStrategy):
    async def attempt(self, stage: Any, error: Exception, attempt_num: int, context: RecoveryContext) -> RecoveryResult:
        return RecoveryResult(action="skip", error=str(error))


ExecuteFn = Callable[[bool], Awaitable[dict[str, Any]]]
ValidateFn = Callable[[dict[str, Any]], list[str]]
WriteFn = Callable[[dict[str, Any]], None]


class RecoveryChain:
    def __init__(self, strategies: list[RecoveryStrategy] | None = None):
        self.strategies = list(strategies or [])

    async def execute(
        self,
        stage: Any,
        execute_fn: ExecuteFn,
        *,
        validate_fn: ValidateFn | None = None,
        write_fn: WriteFn | None = None,
        context: RecoveryContext | None = None,
    ) -> tuple[str, dict[str, Any] | None, str]:
        """
        使用恢复策略运行 execute_fn。

        返回:
            (outcome, data, error)，其中 outcome 为 success|skipped|failed
        """
        ctx = context or RecoveryContext(
            stage_id=getattr(stage, "stage_id", ""),
            agent_id=getattr(stage, "agent_id", ""),
        )
        attempt = 0
        last_error = "unknown"
        fresh = False

        while True:
            try:
                data = await execute_fn(fresh)
            except Exception as exc:
                last_error = str(exc)
                result = await self._consult_strategies(stage, exc, attempt, ctx)
                if result.action == "retry":
                    if result.delay_seconds:
                        await asyncio.sleep(result.delay_seconds)
                    if result.modified_params and result.modified_params.get("fresh_session"):
                        fresh = True
                    attempt += 1
                    continue
                if result.action == "skip":
                    return ("skipped", None, last_error)
                return ("failed", None, last_error)

            if validate_fn:
                errs = validate_fn(data)
                if errs:
                    last_error = f"schema 校验失败: {errs[:3]}"
                    exc = ValueError(last_error)
                    result = await self._consult_strategies(stage, exc, attempt, ctx)
                    if result.action == "retry":
                        attempt += 1
                        continue
                    if result.action == "skip":
                        return ("skipped", None, last_error)
                    return ("failed", None, last_error)

            if write_fn:
                write_fn(data)
            return ("success", data, "")

    async def _consult_strategies(
        self, stage: Any, error: Exception, attempt: int, context: RecoveryContext,
    ) -> RecoveryResult:
        for strategy in self.strategies:
            result = await strategy.attempt(stage, error, attempt, context)
            if result.action != "fail":
                return result
        return RecoveryResult(action="fail", error=str(error))


def default_recovery_chain(simple_retries: int = 2, fresh_session_retries: int = 1) -> RecoveryChain:
    """将旧版配置映射为默认策略链。"""
    strategies: list[RecoveryStrategy] = []
    if simple_retries > 0:
        strategies.append(SimpleRetryStrategy(max_retries=simple_retries))
    if fresh_session_retries > 0:
        strategies.append(FreshSessionStrategy(max_attempts=fresh_session_retries))
    return RecoveryChain(strategies)
