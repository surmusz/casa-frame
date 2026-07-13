"""审查修复回归测试。"""
import asyncio
import json
import tempfile

import pytest

from casa.artifact import ArtifactStore
from casa.authority import AuthorityResolver, CapabilityMatrix, CapabilityRow, InMemoryGrantStore
from casa.config import init_config
from casa.orchestration import (
    CompileRequest,
    Orchestrator,
    Plan,
    PlanCompiler,
    PlanExecutor,
    PlanNormalizer,
    SimpleAgentExecutor,
    Stage,
    StageRunner,
    UsagePolicy,
)
from casa.orchestration.compile import PlanNormalizer as PN
from casa.orchestration.models import Plan as PlanModel
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler
from casa.scope import DataStore, DataStoreAccessError, RefID
from casa.tenant import InMemoryTenantManager, Tenant, reset_tenant_manager
from casa.recovery import FreshSessionStrategy, RecoveryChain, RecoveryContext
from casa.artifact.backend import S3ArtifactBackend
from casa.cache import LocalArtifactCache, cache_key, inputs_fingerprint
from casa.config import ConcurrencyPolicy


def test_s3_prefix_with_tenant():
    init_config()
    store = ArtifactStore("job1", tenant_id="tenant_a")
    store.init_plan("p1")
    assert store._artifact_storage_prefix() == "artifacts/tenant_a/job1/p1/"


def test_s3_prefix_nested_base_without_tenant():
    plan_dir = "/var/data/acme/deep/job1/plans/p1/artifacts"
    prefix = S3ArtifactBackend._prefix_from_plan_dir(plan_dir)
    assert prefix == "artifacts/job1/p1/"


def test_storage_key_includes_tenant():
    init_config()
    store = ArtifactStore("job1", tenant_id="tenant_a")
    store.init_plan("p1")
    assert store._storage_key("out") == "artifacts/tenant_a/job1/p1/out"


def test_normalizer_removes_invalid_depends_on():
    normalizer = PN(core_pipeline_ids=set(), enabled_agent_ids=set())
    plan = Plan(stages=[
        Stage(stage_id="a", agent_id="a", depends_on=[]),
        Stage(stage_id="b", agent_id="b", depends_on=["a"]),
    ])

    def unhealthy(agent_id: str, *, window_minutes: int = 5):
        if agent_id == "a":
            return {"healthy": False, "error_rate": 1.0}
        return {"healthy": True, "error_rate": 0.0}

    normalizer.agent_health_check = unhealthy  # type: ignore[method-assign]
    result = normalizer.normalize(plan)
    assert len(result.stages) == 1
    assert result.stages[0].stage_id == "b"
    assert result.stages[0].depends_on == []


def test_datastore_explicit_empty_grants_deny():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j1")
        store.init_plan("p1")
        store.write("secret", {"v": 1})
        ds = DataStore(artifact_store=store, job_id="j1", data_grants_read=[])
        with pytest.raises(DataStoreAccessError):
            ds.resolve_read(RefID.job_artifact("j1", "secret"))


def test_mark_failed_releases_tenant_quota():
    reset_tenant_manager()
    init_config(max_parallel_per_session=2)
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"max_parallel": 1}))
    import casa.tenant as tenant_mod
    old = tenant_mod._default_manager
    tenant_mod._default_manager = mgr
    try:
        sched = SessionScheduler(backend=InMemorySchedulerBackend())
        r = sched.submit("s1", tenant_id="t1", slots_needed=1)
        assert r.status == "accepted"
        assert not asyncio.run(mgr.check_quota("t1", "max_parallel"))
        sched.mark_failed("s1", r.run.run_id, "boom")
        assert asyncio.run(mgr.check_quota("t1", "max_parallel"))
    finally:
        tenant_mod._default_manager = old


def test_quota_reject_no_ghost_idempotency():
    reset_tenant_manager()
    init_config(max_parallel_per_session=4)
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"max_parallel": 0}))
    import casa.tenant as tenant_mod
    old = tenant_mod._default_manager
    tenant_mod._default_manager = mgr
    try:
        sched = SessionScheduler(backend=InMemorySchedulerBackend())
        r1 = sched.submit("s1", tenant_id="t1", idempotency_key="k1")
        assert r1.status == "rejected"
        r2 = sched.submit("s1", tenant_id="t1", idempotency_key="k1")
        assert r2.status == "rejected"
        assert r2.run is None
    finally:
        tenant_mod._default_manager = old


@pytest.mark.asyncio
async def test_idempotent_skip_requires_patch_policy(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"a": ([], "out")}
    store = ArtifactStore("j1")
    store.init_plan("p1")
    store.write("out", {"cached": True})
    calls = {"n": 0}
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({"a": lambda _c: (calls.__setitem__("n", calls["n"] + 1) or {"v": 2})}),
    )
    executor = PlanExecutor(store=store, stage_runner=runner)
    orch = Orchestrator(compiler=PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a"}), executor=executor)

    plan_default = Plan(stages=[Stage(stage_id="a", agent_id="a")], usage_policy=UsagePolicy.for_user_start())
    await executor.execute(plan_default)
    assert calls["n"] == 1

    calls["n"] = 0
    plan_patch = Plan(stages=[Stage(stage_id="a", agent_id="a")], usage_policy=UsagePolicy.for_patch())
    await executor.execute(plan_patch)
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_fresh_session_strategy_after_simple_retry():
    attempts = {"n": 0}

    async def execute_fn(fresh: bool) -> dict:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("fail")
        assert fresh is True
        return {"ok": True}

    from casa.recovery import SimpleRetryStrategy, default_recovery_chain

    chain = default_recovery_chain(simple_retries=1, fresh_session_retries=1)
    outcome, data, _ = await chain.execute(Stage(stage_id="s1", agent_id="a1"), execute_fn)
    assert outcome == "success"
    assert data == {"ok": True}
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_approve_plan_and_run_approved(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"a": ([], "out")}
    store = ArtifactStore("j1")
    store.init_plan("p1")
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a"})
    runner = StageRunner(store=store, executor=SimpleAgentExecutor({"a": lambda _c: {"ok": True}}))
    orch = Orchestrator(compiler=compiler, executor=PlanExecutor(store=store, stage_runner=runner))
    result = await orch.run(CompileRequest(review_mode=True, seed_stages=[{"agent_id": "a"}]))
    pid = result.plan.plan_id
    assert await orch.approve_plan(pid)
    assert pid not in orch._pending_plans
    exec_result = await orch.run_approved_plan(pid, job_id="j1")
    assert exec_result.stage_results["a"].success


def test_plan_from_dict_roundtrip():
    plan = PlanModel(
        stages=[Stage(stage_id="a", agent_id="a", output_artifact_kind="out")],
        usage_policy=UsagePolicy.for_patch(),
        intent_summary="test",
    )
    restored = PlanModel.from_dict(plan.to_dict())
    assert restored.plan_id == plan.plan_id
    assert restored.stages[0].agent_id == "a"
    assert restored.usage_policy.allow_skip_core_if_artifacts_exist is True


def test_fifo_dequeue_reserves_daily_runs():
    reset_tenant_manager()
    init_config(max_parallel_per_session=1, concurrency_policy=ConcurrencyPolicy.FIFO)
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"max_parallel": 2, "daily_runs": 2}))
    import casa.tenant as tenant_mod
    old = tenant_mod._default_manager
    tenant_mod._default_manager = mgr
    try:
        sched = SessionScheduler(backend=InMemorySchedulerBackend())
        r1 = sched.submit("s1", tenant_id="t1")
        r2 = sched.submit("s1", tenant_id="t1")
        assert r1.status == "accepted"
        assert r2.status == "queued"
        sched.release("s1", r1.run.run_id)
        promoted = sched.backend.get_run(r2.run.run_id)
        assert promoted.status == "accepted"
        assert not mgr.try_reserve_quota("t1", "daily_runs", 1)
    finally:
        tenant_mod._default_manager = old


def test_cache_fingerprint_changes_on_upstream(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    store = ArtifactStore("j1")
    store.init_plan("p1")
    store.write("raw", {"v": 1})
    refs = ["job:j1:artifact:raw"]
    fp1 = inputs_fingerprint(store, refs, extract_kind=lambda r: r.rsplit(":", 1)[-1])
    store.write("raw", {"v": 2})
    fp2 = inputs_fingerprint(store, refs, extract_kind=lambda r: r.rsplit(":", 1)[-1])
    assert fp1 != fp2


@pytest.mark.asyncio
async def test_hitl_multi_stage_continues_after_resume(tmp_path):
    from casa.interrupt import InterruptController

    init_config(artifact_base_dir=str(tmp_path))
    agent_io = {"a": ([], "out_a"), "b": (["out_a"], "out_b")}
    store = ArtifactStore("j")
    store.init_plan("p")
    ctrl = InterruptController()

    def handler_a(_ctx):
        return {"v": 1, "_meta": {"interaction_request": {"message": "confirm"}}}

    def handler_b(_ctx):
        return {"v": 2}

    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({"a": handler_a, "b": handler_b}),
        interrupt_ctrl=ctrl,
    )
    executor = PlanExecutor(store=store, stage_runner=runner, interrupt_ctrl=ctrl)
    plan = Plan(stages=[
        Stage(stage_id="a", agent_id="a", output_artifact_kind="out_a"),
        Stage(stage_id="b", agent_id="b", output_artifact_kind="out_b", depends_on=["a"]),
    ])
    task = asyncio.create_task(executor.execute(plan))
    await asyncio.sleep(0.05)
    assert ctrl.is_paused
    ctrl.resume()
    results = await task
    assert results["a"].success
    assert results["b"].success
    assert store.read("out_b") == {"v": 2}


def test_authority_empty_db_grants_override_code():
    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="a", data_read=["x"], data_write="y", tool_ids=["t1"]))
    store = InMemoryGrantStore()
    from casa.authority import DataGrant
    store.save_data_grant(DataGrant(agent_id="a", read_artifacts=[], write_artifact=""))
    store.save_tool_grant(__import__("casa.authority.grants", fromlist=["ToolGrant"]).ToolGrant(
        agent_id="a", tool_id="t0", enabled=True,
    ))
    store._tool_grants["a"] = {}
    resolver = AuthorityResolver(matrix=matrix, grant_store=store)
    assert resolver.resolve_data_grants("a")["read"] == []
    assert resolver.resolve_tools("a") == []
