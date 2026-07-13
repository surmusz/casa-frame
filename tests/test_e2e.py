"""端到端场景测试 — 最长链路覆盖。"""
import tempfile

import pytest

from casa.authority import CapabilityMatrix, CapabilityRow
from casa.config import init_config, LLMProviderConfig, get_config
from casa.contract import BaseRequired, ContractBuilder, ContractGate
from casa.artifact import ArtifactStore
from casa.intent import AgentCapability, IntentRouter
from casa.loop import AgentLoop
from casa.orchestration import (
    CompileRequest, MockAgentExecutor, Orchestrator, PlanCompiler,
    PlanExecutor, QualityGate, QualityGateRule, StageRunner, UsagePolicy,
)
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler
from casa.tenant import Tenant, reset_tenant_manager


def _agent_io() -> dict:
    return {
        "fetcher": ([], "raw_data"),
        "analyst": (["raw_data"], "analytics"),
        "writer": (["analytics"], "report"),
    }


def _catalog() -> dict[str, AgentCapability]:
    return {
        "fetcher": AgentCapability(agent_id="fetcher", display_name="Fetcher", description="采集"),
        "analyst": AgentCapability(agent_id="analyst", display_name="Analyst", description="分析"),
        "writer": AgentCapability(agent_id="writer", display_name="Writer", description="撰写"),
    }


@pytest.mark.asyncio
async def test_e2e_intent_to_deliverable(tmp_path):
    """完整链路：意图 → 路由 → 契约 → 编译 → 执行 → review_feedback。"""
    init_config(artifact_base_dir=str(tmp_path))
    intent_text = "生成一份主题分析报告"

    async def llm_call(system: str, user: str) -> dict:
        return {"agent_ids": ["fetcher", "analyst", "writer"], "policy": "for_user_start"}

    router = IntentRouter(catalog=_catalog(), llm_call=llm_call)
    route = await router.route(intent_text)

    contract = ContractBuilder(required_class=BaseRequired).build(
        session_id="s1", user_id="u1", intent_summary=intent_text,
    )
    run_req = ContractGate().submit(contract)
    assert run_req.run_id

    matrix = CapabilityMatrix()
    for aid, reads, write in [
        ("fetcher", [], "raw_data"),
        ("analyst", ["raw_data"], "analytics"),
        ("writer", ["analytics"], "report"),
    ]:
        matrix.register(CapabilityRow(
            agent_id=aid, data_read=list(reads), data_write=write,
            model_preference="openai" if aid == "fetcher" else "claude-sonnet-4-6",
        ))

    store = ArtifactStore("e2e_job")
    store.init_plan("e2e_plan")
    orch = Orchestrator(
        compiler=PlanCompiler(
            agent_io_map=_agent_io(),
            core_pipeline_ids={"fetcher"},
            capability_matrix=matrix,
        ),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(
                store=store,
                executor=MockAgentExecutor({
                    "fetcher": {"items": [1]},
                    "analyst": {"themes": ["A"]},
                    "writer": {"title": "Report"},
                }),
            ),
        ),
    )

    result = await orch.run(CompileRequest(
        seed_stages=[{"agent_id": a} for a in route.agent_ids],
        deliverable_type="full",
        policy=UsagePolicy.for_user_start(),
        intent_summary=intent_text,
    ))

    assert all(r.success for r in result.stage_results.values())
    assert set(store.list_artifacts()) >= {"raw_data", "analytics", "report"}
    feedback = result.review_feedback()
    assert not feedback["actionable"]


@pytest.mark.asyncio
async def test_e2e_loop_with_quality_gate(tmp_path):
    """Loop + EvalStage：首轮验证失败 → 迭代 → 最终成功。"""
    init_config(artifact_base_dir=str(tmp_path))
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="worker", data_write="out"))
    matrix.register(CapabilityRow(agent_id="judge", is_evaluator=True, data_write="out_eval"))

    agent_io = {"worker": ([], "out"), "judge": (["out"], "out_eval")}
    store = ArtifactStore("loop_job")
    store.init_plan("loop_plan")

    gate = QualityGate([
        QualityGateRule(rule_id="low_q", condition="quality_score < 0.7", action="pause"),
    ])
    orch = Orchestrator(
        compiler=PlanCompiler(
            agent_io_map=agent_io,
            core_pipeline_ids={"worker"},
            capability_matrix=matrix,
        ),
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(
                store=store,
                executor=MockAgentExecutor({"worker": {"ok": True}, "judge": {"score": 0.9}}),
            ),
        ),
        quality_gate=gate,
    )

    attempts = {"n": 0}

    async def fail_once_verifier(result, ctx):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return ["首轮质量未达标，需要修正"]
        return []

    loop = AgentLoop(
        orchestrator=orch,
        verifier=fail_once_verifier,
        max_iterations=3,
        require_double_pass=False,
    )
    result = await loop.run("完善分析报告")

    assert result.success
    assert result.total_iterations == 2
    assert attempts["n"] == 2


def test_e2e_multi_tenant_quota(tmp_path):
    """多租户配额：租户 A 有配额通过，租户 B 无配额被拒绝。"""
    init_config(max_parallel_per_session=4)
    reset_tenant_manager()
    from casa.tenant import get_tenant_manager

    mgr = get_tenant_manager()
    mgr.register(Tenant(tenant_id="tenant_a", quotas={"max_parallel": 2}))
    mgr.register(Tenant(tenant_id="tenant_b", quotas={"max_parallel": 0}))

    sched = SessionScheduler(backend=InMemorySchedulerBackend())
    r_a = sched.submit("sess", tenant_id="tenant_a")
    assert r_a.status == "accepted"

    r_b = sched.submit("sess", tenant_id="tenant_b")
    assert r_b.status == "rejected"
    assert "quota" in r_b.message

    sched.release("sess", r_a.run.run_id)


def test_llm_config_injected_to_context(tmp_path):
    """StageRunner 自动注入 per-provider llm_config。"""
    init_config(
        artifact_base_dir=str(tmp_path),
        llm_api_key="global-key",
        llm_providers={
            "anthropic": LLMProviderConfig(
                provider="anthropic", api_key="ant-key", base_url="https://anthropic.example",
            ),
        },
    )
    store = ArtifactStore("j")
    runner = StageRunner(store=store, executor=MockAgentExecutor({}))
    from casa.orchestration import Stage, Plan
    ctx = runner._build_execute_context(
        Stage(stage_id="w", agent_id="w", model_preference="claude-sonnet-4-6"),
        Plan(),
        fresh_session=False,
    )
    assert ctx["llm_config"]["api_key"] == "ant-key"
    assert ctx["llm_config"]["provider"] == "anthropic"

    cfg = get_config().get_llm_config("openai")
    assert cfg["api_key"] == "global-key"
