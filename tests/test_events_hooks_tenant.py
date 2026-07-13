"""EventBus、Hook、租户与知识库回归测试。"""
import asyncio
import threading

import pytest

from casa.config import Scope
from casa.events import Event, InProcessEventBus
from casa.hooks import HookRegistry, PipelineHook
from casa.knowledge import InMemoryKnowledgeBase, KBEntry
from casa.tenant import InMemoryTenantManager, Tenant


def test_event_bus_unsubscribe():
    bus = InProcessEventBus()
    seen: list[str] = []

    def handler(event):
        seen.append(event.event_type)

    sub_id = bus.subscribe("foo.*", handler)
    asyncio.run(bus.publish(Event(event_type="foo.bar")))
    assert seen == ["foo.bar"]
    assert bus.unsubscribe(sub_id)
    seen.clear()
    asyncio.run(bus.publish(Event(event_type="foo.baz")))
    assert seen == []


def test_kb_embed_fn_search():
    def embed(text: str) -> list[float]:
        return [1.0, 0.0] if "apple" in text.lower() else [0.0, 1.0]

    kb = InMemoryKnowledgeBase("t", scope=Scope.GLOBAL, embed_fn=embed)
    kb.put(KBEntry(entry_id="a", content="apple pie"))
    kb.put(KBEntry(entry_id="b", content="banana"))
    import asyncio
    hits = asyncio.run(kb.search("apple", top_k=1))
    assert hits[0].entry_id == "a"


@pytest.mark.asyncio
async def test_orchestrator_auto_render(tmp_path):
    from casa import (
        ArtifactStore, CompileRequest, DeliverableSpec, Orchestrator,
        PlanCompiler, PlanExecutor, SimpleAgentExecutor, StageRunner,
        get_deliverable_registry, init_config, reset_deliverable_registry,
    )

    reset_deliverable_registry()
    init_config(artifact_base_dir=str(tmp_path))
    get_deliverable_registry().register(
        DeliverableSpec(deliverable_id="full", label="Full", sources=["out"]),
    )
    agent_io = {"w": ([], "out")}
    store = ArtifactStore("j")
    store.init_plan("p")
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"w"})
    runner = StageRunner(store=store, executor=SimpleAgentExecutor({"w": lambda c: {"v": 1}}))
    orch = Orchestrator(compiler=compiler, executor=PlanExecutor(store=store, stage_runner=runner))
    result = await orch.run(CompileRequest(deliverable_type="full"), auto_render=True)
    assert result.deliverable_output is not None
    assert "path" in result.deliverable_output


def test_hook_registry_concurrent_register():
    reg = HookRegistry()
    errors: list[Exception] = []

    class NoOpHook(PipelineHook):
        pass

    def worker(n: int) -> None:
        try:
            for _ in range(20):
                reg.register(NoOpHook(), priority=n)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    asyncio.run(reg.fire("compile_start", request={}))


def test_try_reserve_quota_atomic():
    mgr = InMemoryTenantManager()
    mgr.register(Tenant(tenant_id="t1", quotas={"max_parallel": 1}))
    assert mgr.try_reserve_quota("t1", "max_parallel")
    assert not mgr.try_reserve_quota("t1", "max_parallel")
    mgr.release_quota("t1", "max_parallel")
    assert mgr.try_reserve_quota("t1", "max_parallel")
