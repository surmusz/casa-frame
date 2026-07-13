"""产物缓存测试。"""
import tempfile

import pytest

from casa.cache import LocalArtifactCache, cache_key
from casa.config import init_config
from casa.artifact import ArtifactStore
from casa.orchestration import (
    CompileRequest, PlanCompiler, PlanExecutor, Orchestrator,
    StageRunner, SimpleAgentExecutor, UsagePolicy,
)


def test_cache_key_deterministic():
    k1 = cache_key("out", ["job:j:artifact:a"], {"x": 1})
    k2 = cache_key("out", ["job:j:artifact:a"], {"x": 1})
    assert k1 == k2
    assert k1.startswith("out:")


def test_local_cache_put_get_invalidate():
    with tempfile.TemporaryDirectory() as tmp:
        cache = LocalArtifactCache(cache_dir=tmp)
        key = cache_key("kind", ["ref1"], {})
        cache.put(key, {"cached": True})
        assert cache.get(key) == {"cached": True}
        assert cache.invalidate("kind") == 1
        assert cache.get(key) is None


@pytest.mark.asyncio
async def test_stage_runner_cache_hit(tmp_path):
    init_config(artifact_base_dir=str(tmp_path))
    cache = LocalArtifactCache(cache_dir=str(tmp_path / "cache"))
    calls = {"n": 0}

    def handler_a(ctx):
        return {"raw": 1}

    def handler_b(ctx):
        calls["n"] += 1
        return {"v": 1}

    agent_io = {"a": ([], "raw"), "b": (["raw"], "out")}
    store = ArtifactStore("j1")
    store.init_plan("p1")
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"a"})
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({"a": handler_a, "b": handler_b}),
        cache_backend=cache,
    )
    executor = PlanExecutor(store=store, stage_runner=runner)
    orch = Orchestrator(compiler=compiler, executor=executor)

    await orch.run(
        CompileRequest(
            seed_stages=[{"agent_id": "a"}, {"agent_id": "b"}],
            policy=UsagePolicy.for_patch(),
        ),
        job_id="j1",
    )
    assert calls["n"] == 1

    store2 = ArtifactStore("j2")
    store2.init_plan("p2")
    runner2 = StageRunner(
        store=store2,
        executor=SimpleAgentExecutor({"a": handler_a, "b": handler_b}),
        cache_backend=cache,
    )
    executor2 = PlanExecutor(store=store2, stage_runner=runner2)
    orch2 = Orchestrator(compiler=compiler, executor=executor2)
    await orch2.run(
        CompileRequest(seed_stages=[{"agent_id": "a"}, {"agent_id": "b"}]),
        job_id="j2",
    )
    assert calls["n"] == 2
    assert store2.read("out") == {"v": 1}


@pytest.mark.asyncio
async def test_token_usage_recorded(tmp_path):
    from casa.observability import run_context
    from casa.tenant import InMemoryTenantManager, Tenant, reset_tenant_manager

    reset_tenant_manager()
    init_config(artifact_base_dir=str(tmp_path))
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"daily_tokens": 10000}))

    import casa.tenant as tenant_mod
    old = tenant_mod._default_manager
    tenant_mod._default_manager = mgr

    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"})
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({
            "w": lambda c: {"ok": True, "_meta": {"tokens_total": 500}},
        }),
    )
    orch = Orchestrator(
        compiler=compiler,
        executor=PlanExecutor(store=store, stage_runner=runner),
    )
    with run_context(tenant_id="t1"):
        await orch.run(CompileRequest(), job_id="j")

    usage = mgr.get_token_usage("t1")
    assert usage["used"] == 500
    tenant_mod._default_manager = old
