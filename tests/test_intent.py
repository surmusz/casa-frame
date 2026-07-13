"""意图路由测试。"""
import pytest

from casa.authority import CapabilityMatrix, CapabilityRow
from casa.intent import AgentCapability, IntentRouter


def _catalog() -> dict[str, AgentCapability]:
    return {
        "fetcher": AgentCapability(
            agent_id="fetcher",
            display_name="Fetcher",
            description="采集原始数据",
            tags=["data_collection"],
        ),
        "analyst": AgentCapability(
            agent_id="analyst",
            display_name="Analyst",
            description="分析数据",
            tags=["analysis"],
        ),
    }


@pytest.mark.asyncio
async def test_route_with_valid_catalog():
    async def llm_call(system: str, user: str) -> dict:
        return {"agent_ids": ["fetcher", "analyst"], "policy": "for_user_start"}

    router = IntentRouter(catalog=_catalog(), llm_call=llm_call)
    result = await router.route("做一份分析报告")
    assert result.agent_ids == ["fetcher", "analyst"]
    assert result.policy == "for_user_start"
    assert not result.warnings


@pytest.mark.asyncio
async def test_route_rejects_unknown_agents():
    async def llm_call(system: str, user: str) -> dict:
        return {"agent_ids": ["fetcher", "ghost"], "policy": "for_user_start"}

    router = IntentRouter(catalog=_catalog(), llm_call=llm_call)
    result = await router.route("分析")
    assert result.agent_ids == ["fetcher"]
    assert any("未知 agent_id" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_route_fallback_without_llm():
    router = IntentRouter(catalog=_catalog())
    result = await router.route("任意意图")
    assert set(result.agent_ids) == {"fetcher", "analyst"}
    assert any("无 LLM" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_route_invalid_policy_defaults():
    async def llm_call(system: str, user: str) -> dict:
        return {"agent_ids": ["analyst"], "policy": "invalid_policy"}

    router = IntentRouter(catalog=_catalog(), llm_call=llm_call)
    result = await router.route("预览")
    assert result.policy == "for_user_start"


def test_from_capability_matrix():
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(
        agent_id="writer",
        display_name="Report Writer",
        task_template="撰写报告",
        scope_tags=["reporting"],
    ))
    agent_io = {"writer": (["analytics"], "report")}
    router = IntentRouter.from_capability_matrix(matrix, agent_io)
    cap = router.catalog["writer"]
    assert cap.output_artifact == "report"
    assert "reporting" in cap.tags


@pytest.mark.asyncio
async def test_route_llm_exception_fallback():
    async def boom(system: str, user: str) -> dict:
        raise RuntimeError("llm down")

    router = IntentRouter(catalog=_catalog(), llm_call=boom)
    result = await router.route("failover")
    assert len(result.agent_ids) == 2
    assert any("无 LLM" in w for w in result.warnings)
