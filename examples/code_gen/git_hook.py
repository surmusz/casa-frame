"""
GitHook — 代码 stage 自动分支与提交（PipelineHook 参考示例）。

用法::

    from casa import HookRegistry, Orchestrator
    from examples.code_gen.git_hook import GitHook

    hooks = HookRegistry()
    hooks.register(GitHook(repo_path="/path/to/repo"))
    orch = Orchestrator(..., hooks=hooks)
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any

from casa.hooks import PipelineHook

logger = logging.getLogger("casa.examples.git_hook")


class GitHook(PipelineHook):
    """stage 开始时创建分支，结束时提交变更。"""

    def __init__(self, repo_path: str, *, branch_prefix: str = "casa-agent/") -> None:
        self._repo = repo_path
        self._prefix = branch_prefix
        self._branches: dict[str, str] = {}

    async def on_stage_start(self, stage: Any, plan: Any) -> None:
        branch = f"{self._prefix}{stage.stage_id}"
        self._branches[stage.stage_id] = branch
        self._git("checkout", "-b", branch)

    async def on_stage_end(self, stage: Any, result: Any) -> None:
        if not getattr(result, "success", False):
            return
        self._git("add", "-A")
        msg = f"casa: stage {stage.stage_id} ({stage.agent_id})"
        code = self._git("commit", "-m", msg, check=False)
        if code != 0:
            logger.debug("git commit skipped (no changes?) for stage %s", stage.stage_id)

    def _git(self, *args: str, check: bool = True) -> int:
        try:
            proc = subprocess.run(
                ["git", "-C", self._repo, *args],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if check and proc.returncode != 0:
                logger.warning("git %s failed: %s", " ".join(args), proc.stderr.strip())
            return proc.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("git unavailable: %s", exc)
            return 1
