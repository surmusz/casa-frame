"""端到端场景：Intent → Contract → compile → run → review_feedback。"""
import tempfile

import pytest

from casa.authority import CapabilityMatrix, CapabilityRow
from casa.config import init_config
from casa.contract import BaseRequired, ContractBuilder, ContractGate
from casa.artifact import ArtifactStore
from casa.intent import AgentCapability, IntentRouter
from casa.orchestration import (
    CompileRequest, MockAgentExecutor, Orchestrator, PlanCompiler,
    PlanExecutor, StageRunner, UsagePolicy,
)


def _agent_io() -> dict:
    return {
        "fetcher": ([], "raw_data"),
        "analyst": (["raw_data"], "analytics"),
        "writer": (["analytics"], "report"),
    }


def _matrix() -> CapabilityMatrix:
    matrix = CapabilityMatrix()
    for agent_id, reads, write in [
        ("fetcher", [], "raw_data"),
        ("analyst", ["raw_data"], "analytics"),
        ("writer", ["analytics"], "report"),
    ]:
        matrix.register(CapabilityRow(
            agent_id=agent_id,
            data_read=list(reads),
            data_write=write,
            model_preference="openai" if agent_id == "fetcher" else "anthropic",
        ))
    return matrix


def _catalog() -> dict[str, AgentCapability]:
    return {
        "fetcher": AgentCapability(agent_id="fetcher", display_name="Fetcher", description="采集"),
        "analyst": AgentCapability(agent_id="analyst", display_name="Analyst", description="分析"),
        "writer": AgentCapability(agent_id="writer", display_name="Writer", description="撰写"),
    }


@pytest.mark.asyncio
async def test_intent_to_delivery_pipeline():
    """自然语言意图经路由、契约校验、编排执行到 review_feedback。"""
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        intent_text = "生成一份主题分析报告"

        async def llm_call(system: str, user: str) -> dict:
            return {
                "agent_ids": ["fetcher", "analyst", "writer"],
                "policy": "for_user_start",
            }

        router = IntentRouter(catalog=_catalog(), llm_call=llm_call)
        route = await router.route(intent_text)
        assert route.agent_ids == ["fetcher", "analyst", "writer"]

        builder = ContractBuilder(required_class=BaseRequired)
        contract = builder.build(
            session_id="e2e_sess",
            user_id="e2e_user",
            intent_summary=intent_text,
        )
        gate = ContractGate()
        run_req = gate.submit(contract)
        assert run_req.contract.version >= 1
        assert run_req.run_id

        agent_io = _agent_io()
        matrix = _matrix()
        compiler = PlanCompiler(
            agent_io_map=agent_io,
            core_pipeline_ids={"fetcher"},
            capability_matrix=matrix,
        )
        store = ArtifactStore(job_id="e2e_job")
        store.init_plan("e2e_plan")
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(
                store=store,
                stage_runner=StageRunner(
                    store=store,
                    executor=MockAgentExecutor({
                        "fetcher": {"items": [1, 2, 3]},
                        "analyst": {"themes": ["A"]},
                        "writer": {"title": "Report"},
                    }),
                ),
            ),
        )

        policy_cls = {
            "for_user_start": UsagePolicy.for_user_start,
            "for_patch": UsagePolicy.for_patch,
            "for_preview": UsagePolicy.for_preview,
        }
        policy = policy_cls.get(route.policy, UsagePolicy.for_user_start)()

        result = await orch.run(CompileRequest(
            seed_stages=[{"agent_id": aid} for aid in route.agent_ids],
            deliverable_type="full",
            policy=policy,
            intent_summary=intent_text,
        ))

        assert result.plan.plan_id
        assert all(r.success for r in result.stage_results.values())
        assert set(store.list_artifacts()) >= {"raw_data", "analytics", "report"}

        feedback = result.review_feedback()
        assert feedback["stages"]
        assert feedback["actionable"] is False
