#!/usr/bin/env python3
"""RAG 问答 pipeline — 检索 + 生成 + QA（structured + harness 模式）。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from casa import (
    ArtifactStore, CompileRequest, Orchestrator, PlanCompiler,
    PlanExecutor, SimpleAgentExecutor, StageRunner, init_config,
)

AGENT_IO = {
    "doc_ingest": ([], "corpus"),
    "retriever": (["corpus"], "retrieved_chunks"),
    "answer_gen": (["retrieved_chunks"], "draft_answer"),
    "qa_gate": (["draft_answer"], "qa_result"),
}


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        compiler = PlanCompiler(
            agent_io_map=AGENT_IO,
            core_pipeline_ids=set(AGENT_IO.keys()),
        )
        store = ArtifactStore(job_id="rag_demo")
        store.init_plan("p1")
        handlers = {
            "doc_ingest": lambda ctx: {"docs": ["doc1", "doc2"]},
            "retriever": lambda ctx: {"chunks": ["relevant chunk"]},
            "answer_gen": lambda ctx: {"answer": "CASA is a multi-agent framework."},
            "qa_gate": lambda ctx: {"passed": True, "score": 0.9},
        }
        runner = StageRunner(store=store, executor=SimpleAgentExecutor(handlers))
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(store=store, stage_runner=runner),
        )
        await orch.run(CompileRequest(), job_id="rag_demo")
        print("QA result:", store.read("qa_result"))


if __name__ == "__main__":
    asyncio.run(main())
