#!/usr/bin/env python3
"""多视角分析 — 并行分析师 → 汇总 → QA。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from casa import (
    ArtifactStore, ArtifactDAG, CompileRequest, Orchestrator, PlanCompiler,
    PlanExecutor, SimpleAgentExecutor, StageRunner, init_config,
)

AGENT_IO = {
    "intel_a": ([], "raw_a"),
    "intel_b": ([], "raw_b"),
    "processor": (["raw_a", "raw_b"], "corpus"),
    "analyst_x": (["corpus"], "perspective_x"),
    "analyst_y": (["corpus"], "perspective_y"),
    "assembler": (["perspective_x", "perspective_y"], "report_content"),
    "qa": (["report_content"], "qa_report"),
}


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        dag = ArtifactDAG.from_declarations(AGENT_IO)
        stages = dag.compute_dependencies(set(AGENT_IO.keys()))
        waves = dag.partition_waves(stages)
        print("Wave plan:")
        for i, wave in enumerate(waves):
            print(f"  Wave {i}: {[s['agent_id'] for s in wave]}")

        compiler = PlanCompiler(
            agent_io_map=AGENT_IO,
            core_pipeline_ids=set(AGENT_IO.keys()),
        )
        store = ArtifactStore(job_id="multi")
        store.init_plan("p1")
        handlers = {aid: (lambda ctx, a=aid: {"agent": a, "ok": True}) for aid in AGENT_IO}
        runner = StageRunner(store=store, executor=SimpleAgentExecutor(handlers))
        orch = Orchestrator(
            compiler=compiler,
            executor=PlanExecutor(store=store, stage_runner=runner),
        )
        await orch.run(CompileRequest(), job_id="multi")
        print("Artifacts:", store.list_artifacts())


if __name__ == "__main__":
    asyncio.run(main())
