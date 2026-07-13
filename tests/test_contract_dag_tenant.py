"""Phase 2 测试：契约修订、租户、DAG 可视化。"""
import asyncio

from casa.artifact import ArtifactDAG
from casa.contract import BaseDeliverable, BaseRequired, Contract, ContractRevision
from casa.orchestration import Plan, Stage
from casa.tenant import InMemoryTenantManager, Tenant


def test_contract_revise_and_diff():
    c1 = Contract(
        deliverable=BaseDeliverable(type="full"),
        required=BaseRequired(),
        session_id="s1",
        user_id="u1",
    )
    c2 = Contract(
        deliverable=BaseDeliverable(type="insights"),
        required=BaseRequired(),
        session_id="s1",
        user_id="u1",
    )
    rev = c1.revise("u1", intent_summary="updated")
    assert isinstance(rev, ContractRevision)
    diffs = c1.diff(c2)
    assert any(d.field_path == "deliverable.type" for d in diffs)


def test_dag_to_mermaid():
    dag = ArtifactDAG.from_declarations({
        "a": ([], "x"),
        "b": (["x"], "y"),
    })
    m = dag.to_mermaid()
    assert "flowchart" in m
    assert "a" in m and "b" in m


def test_plan_to_mermaid():
    plan = Plan(stages=[
        Stage(stage_id="s1", agent_id="a1"),
        Stage(stage_id="s2", agent_id="a2", depends_on=["s1"]),
    ])
    assert "s1 --> s2" in plan.to_mermaid()


def test_tenant_quota():
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"max_parallel": 1}))
    assert asyncio.run(mgr.check_quota("t1", "max_parallel"))
    assert mgr.try_reserve_quota("t1", "max_parallel")
    assert not asyncio.run(mgr.check_quota("t1", "max_parallel"))
    mgr.release_quota("t1", "max_parallel")
    assert asyncio.run(mgr.check_quota("t1", "max_parallel"))
