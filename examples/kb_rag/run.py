"""KB + RAG 示例，使用 KBRegistry。"""
import asyncio

from casa import (
    ArtifactStore, CompileRequest, DataStore, InMemoryKnowledgeBase,
    KBEntry, KBRegistry, PlanCompiler, PlanExecutor, Orchestrator,
    RefCatalog, Scope, SimpleAgentExecutor, StageRunner, init_config,
)


async def main():
    init_config(artifact_base_dir="casa_jobs_kb")
    kb = InMemoryKnowledgeBase("platform", scope=Scope.GLOBAL)
    kb.put(KBEntry(entry_id="faq", content={"q": "What is CASA?", "a": "Agent orchestration framework"}))
    registry = KBRegistry()
    registry.register(kb)

    store = ArtifactStore("demo")
    store.init_plan("p1")
    agent_io = {"worker": ([], "answer")}
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"worker"})
    runner = StageRunner(
        store=store,
        executor=SimpleAgentExecutor({"worker": lambda ctx: {"text": "from kb"}}),
    )
    orch = Orchestrator(compiler=compiler, executor=PlanExecutor(store=store, stage_runner=runner))
    cat = RefCatalog.build(session_id="s1", agent_id="worker", kb_registry=registry)
    print("KB refs:", [r["ref_id"] for r in cat.refs if r["scope"] == Scope.GLOBAL])
    await orch.run(CompileRequest(), job_id="demo")


if __name__ == "__main__":
    asyncio.run(main())
