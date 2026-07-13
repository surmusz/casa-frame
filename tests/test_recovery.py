"""恢复链测试。"""
import pytest

from casa.orchestration import Stage
from casa.recovery import (
    RecoveryChain, RecoveryContext, SimpleRetryStrategy, SkipStrategy, default_recovery_chain,
)


@pytest.mark.asyncio
async def test_simple_retry_then_success():
    attempts = {"n": 0}

    async def execute_fn(fresh: bool) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return {"ok": True}

    chain = RecoveryChain([SimpleRetryStrategy(max_retries=3)])
    outcome, data, err = await chain.execute(Stage(stage_id="s1", agent_id="a1"), execute_fn)
    assert outcome == "success"
    assert data == {"ok": True}


@pytest.mark.asyncio
async def test_skip_strategy():
    async def execute_fn(fresh: bool) -> dict:
        raise RuntimeError("fail")

    chain = RecoveryChain([SkipStrategy()])
    outcome, data, err = await chain.execute(Stage(stage_id="s1", agent_id="a1"), execute_fn)
    assert outcome == "skipped"


def test_default_recovery_chain():
    chain = default_recovery_chain(simple_retries=1, fresh_session_retries=1)
    assert len(chain.strategies) == 2
