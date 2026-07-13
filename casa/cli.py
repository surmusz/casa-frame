"""CASA CLI — 项目脚手架。"""
from __future__ import annotations

import argparse
import os
import textwrap


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def cmd_init(args: argparse.Namespace) -> None:
    target = args.target or "."
    casa_dir = os.path.join(target, "casa_project")
    os.makedirs(casa_dir, exist_ok=True)

    _write(os.path.join(casa_dir, "casa_config.py"), textwrap.dedent("""\
        from casa import init_config

        init_config(
            artifact_base_dir="casa_jobs",
            # llm_api_key="sk-...",
        )
    """))

    _write(os.path.join(casa_dir, "agent_io.py"), textwrap.dedent("""\
        AGENT_IO = {
            "data_fetcher": ([], "raw_data"),
            "analyst": (["raw_data"], "analytics"),
            "report_writer": (["analytics"], "report_content"),
            "qa_checker": (["report_content"], "qa_report"),
        }
    """))

    _write(os.path.join(casa_dir, "run.py"), textwrap.dedent("""\
        import asyncio
        from casa import (
            ArtifactStore, Orchestrator, PlanCompiler, PlanExecutor,
            SimpleAgentExecutor, StageRunner, CompileRequest, init_config,
        )
        from agent_io import AGENT_IO
        import casa_config  # noqa: F401

        async def main():
            compiler = PlanCompiler(
                agent_io_map=AGENT_IO,
                core_pipeline_ids={"data_fetcher"},
            )
            store = ArtifactStore(job_id="demo")
            store.init_plan("plan_001")
            handlers = {
                "data_fetcher": lambda ctx: {"items": [1, 2, 3]},
                "analyst": lambda ctx: {"themes": ["A"]},
                "report_writer": lambda ctx: {"title": "Report"},
                "qa_checker": lambda ctx: {"score": 1.0},
            }
            runner = StageRunner(store=store, executor=SimpleAgentExecutor(handlers))
            executor = PlanExecutor(store=store, stage_runner=runner)
            orch = Orchestrator(compiler=compiler, executor=executor)
            result = await orch.run(CompileRequest(), job_id="demo")
            print("stages:", len(result.plan.stages))

        if __name__ == "__main__":
            asyncio.run(main())
    """))

    print(f"CASA project scaffold created at {casa_dir}/")
    print("  casa_config.py  — configuration")
    print("  agent_io.py     — agent I/O declarations")
    print("  run.py          — minimal runner")


def main() -> None:
    parser = argparse.ArgumentParser(prog="casa", description="CASA framework CLI")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="Scaffold a new CASA project")
    init_p.add_argument("target", nargs="?", help="Target directory")
    init_p.set_defaults(func=cmd_init)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
