#!/usr/bin/env python3
"""最小 4-Agent CASA pipeline 示例。"""
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
    "data_fetcher": ([], "raw_data"),
    "analyst": (["raw_data"], "analytics"),
    "report_writer": (["analytics"], "report_content"),
    "qa_checker": (["report_content"], "qa_report"),
}


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        compiler = PlanCompiler(
            agent_io_map=AGENT_IO,
            core_pipeline_ids=set(AGENT_IO.keys()),
        )
        store = ArtifactStore(job_id="demo")
        store.init_plan("plan_001")
        handlers = {
            "data_fetcher": lambda ctx: {"items": [1, 2, 3]},
            "analyst": lambda ctx: {"themes": ["A", "B"]},
            "report_writer": lambda ctx: {"title": "Demo Report"},
            "qa_checker": lambda ctx: {"score": 0.95},
        }
        runner = StageRunner(store=store, executor=SimpleAgentExecutor(handlers))
        executor = PlanExecutor(store=store, stage_runner=runner)
        orch = Orchestrator(compiler=compiler, executor=executor)
        result = await orch.run(CompileRequest(), job_id="demo", run_id="example_run")
        print(f"Plan {result.plan.plan_id}: {len(result.plan.stages)} stages")
        print(f"Artifacts: {store.list_artifacts()}")


if __name__ == "__main__":
    asyncio.run(main())
