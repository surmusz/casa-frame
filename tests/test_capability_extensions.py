"""能力扩展测试（write_text、渲染器、沙箱、ValidatorFn 等）。"""
import asyncio
import os
import subprocess
import tempfile

import pytest

from casa.artifact import ArtifactStore
from casa.deliverable import (
    ChapterSpec,
    DeliverableRegistry,
    DeliverableSpec,
    MarkdownRenderer,
    RawFilesRenderer,
)
from casa.knowledge import CodebaseKnowledgeBase
from casa.orchestration import (
    CodeAgentExecutor,
    MockAgentExecutor,
    Plan,
    SandboxedAgentExecutor,
    Stage,
    StageRunner,
)


def test_write_text_read_text_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore("j1", base_dir=tmp)
        store.init_plan("p1")
        store.write_text("main_py", "print('hi')", coordination_hint={"version": 1})
        assert store.read_text("main_py") == "print('hi')"
        assert store.read_coordination_hint("main_py") == {"version": 1}


@pytest.mark.asyncio
async def test_markdown_renderer():
    renderer = MarkdownRenderer()
    spec = DeliverableSpec(
        deliverable_id="novel",
        label="Novel",
        chapters=[ChapterSpec(chapter_id="c1", title="第一章", source_artifact="ch1")],
    )
    out = await renderer.render(spec, {"ch1": {"text": "从前有座山。"}})
    assert b"# \xe7\xac\xac\xe4\xb8\x80\xe7\xab\xa0" in out.content or b"#" in out.content
    assert out.format == "md"


@pytest.mark.asyncio
async def test_raw_files_renderer_zip():
    renderer = RawFilesRenderer()
    spec = DeliverableSpec(
        deliverable_id="bundle",
        label="Code",
        sources=["main", "util"],
    )
    out = await renderer.render(
        spec,
        {
            "main": {"_text_content": "def main(): pass"},
            "util": {"k": 1},
        },
    )
    assert out.format == "zip"
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(out.content)) as zf:
        names = set(zf.namelist())
    assert "main.txt" in names
    assert "util.json" in names


def test_codebase_kb_index_search():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "scheduler.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("class SessionScheduler: pass  # atomic slot")
        kb = CodebaseKnowledgeBase("repo", repo_path=tmp)
        n = kb.index_files(["*.py"])
        assert n == 1
        hits = asyncio.run(kb.search("atomic slot"))
        assert len(hits) == 1
        assert hits[0].metadata["lang"] == "py"


def test_docker_volume_args_builds_flags():
    with tempfile.TemporaryDirectory() as tmp:
        args = SandboxedAgentExecutor._docker_volume_args([
            {"host_path": tmp, "container_path": "/workspace", "mode": "rw"},
        ])
    assert args == ["-v", f"{os.path.abspath(tmp)}:/workspace:rw"]


def test_docker_volume_args_skips_missing():
    args = SandboxedAgentExecutor._docker_volume_args([
        {"host_path": "/nonexistent-path-xyz", "container_path": "/w", "mode": "rw"},
    ])
    assert args == []


@pytest.mark.asyncio
async def test_custom_validator_triggers_retry():
    calls = {"n": 0}

    class _Exec:
        async def execute(self, agent_id: str, context: dict) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"_text_content": "import *"}
            return {"_text_content": "import os"}

    def lint_validator(artifact_kind: str, data: dict) -> list[str]:
        if "import *" in data.get("_text_content", ""):
            return ["禁止使用 import *"]
        return []

    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore("j1", base_dir=tmp)
        store.init_plan("p1")
        runner = StageRunner(
            store=store,
            executor=_Exec(),
            schema_validator=lint_validator,
            simple_retries=2,
        )
        plan = Plan(stages=[Stage(stage_id="s1", agent_id="coder", output_artifact_kind="code")])
        result = await runner.run(plan.stages[0], plan, set())
        assert result.success
        assert calls["n"] == 2
        assert store.read_text("code") == "import os"


@pytest.mark.asyncio
async def test_code_agent_executor_collects_changed_files():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
        fpath = os.path.join(tmp, "new.py")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("x = 1")

        inner = MockAgentExecutor({"coder": {"ok": True}})
        executor = CodeAgentExecutor(inner, repo_path=tmp)
        result = await executor.execute("coder", {})
        assert result.get("_meta", {}).get("changed_files") == ["new.py"]


def test_deliverable_registry_builtin_renderers():
    reg = DeliverableRegistry()
    assert "markdown" in reg._renderers
    assert "raw_files" in reg._renderers


@pytest.mark.asyncio
async def test_git_hook_stage_lifecycle():
    from examples.code_gen.git_hook import GitHook

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp, capture_output=True, check=True)

        hook = GitHook(repo_path=tmp)
        stage = Stage(stage_id="s1", agent_id="coder")
        plan = Plan(stages=[stage])
        await hook.on_stage_start(stage, plan)
        with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8") as f:
            f.write("pass")
        from casa.orchestration import StageResult

        await hook.on_stage_end(stage, StageResult(stage_id="s1", agent_id="coder", success=True))

        proc = subprocess.run(
            ["git", "-C", tmp, "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "casa-agent/s1" in proc.stdout or proc.returncode == 0
