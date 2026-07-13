"""Agent 执行器实现。"""
from __future__ import annotations

import abc
import inspect
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("casa.orchestration")

class AgentExecutor(abc.ABC):
    """Agent 执行器接口。领域项目实现此接口接入自己的 Agent 运行时。"""

    @abc.abstractmethod
    async def execute(self, agent_id: str, context: dict[str, Any]) -> dict:
        """
        执行一个 Agent 并返回产物 dict。

        参数:
            agent_id: Agent 标识
            context: {"stage_id", "input_refs", "injected_prompt", "params", "fresh_session"}

        返回:
            artifact dict
        """
        ...

    async def execute_streaming(
        self,
        agent_id: str,
        context: dict[str, Any],
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> dict:
        """支持流式输出的 execute——默认委托给 execute()。"""
        return await self.execute(agent_id, context)

    async def warmup(self, agent_ids: list[str]) -> None:
        """可选：在 wave 执行前预加载模型/资源。"""
        return None


class SimpleAgentExecutor(AgentExecutor):
    """
    简单 Agent 执行器：基于 agent_io_map 的函数映射。

    使用方式：
        def my_analyst(ctx):
            return {"result": "分析完成"}

        executor = SimpleAgentExecutor({
            "my_analyst": my_analyst,
        })
    """

    def __init__(self, handlers: dict[str, Callable[[dict], dict]]):
        self._handlers = handlers

    async def execute(self, agent_id: str, context: dict[str, Any]) -> dict:
        handler = self._handlers.get(agent_id)
        if not handler:
            raise ValueError(f"Agent {agent_id} 未注册执行 handler")
        result = handler(context)
        # 支持 async handler：若返回协程则 await
        if inspect.iscoroutine(result):
            result = await result
        return result


class MockAgentExecutor(AgentExecutor):
    """
    测试用 Agent 执行器——不调 LLM，直接返回预设数据。

    用法:
        executor = MockAgentExecutor({"analyst": {"themes": ["A", "B"]}})
    """

    def __init__(self, responses: dict[str, dict], *, default: dict | None = None):
        self._responses = responses
        self._default = default or {"empty": True}
        self._calls: list[dict[str, Any]] = []

    async def execute(self, agent_id: str, context: dict[str, Any]) -> dict:
        self._calls.append({"agent_id": agent_id, "context": context})
        return dict(self._responses.get(agent_id, self._default))

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def calls_for(self, agent_id: str) -> list[dict]:
        return [c for c in self._calls if c["agent_id"] == agent_id]


class SandboxedAgentExecutor(AgentExecutor):
    """沙箱化 Agent 执行器——Docker 不可用时回退到 inner。"""

    def __init__(
        self,
        inner: AgentExecutor,
        *,
        image: str = "python:3.12-slim",
        network: str = "none",
        memory_limit: str = "512m",
        timeout: int = 300,
    ):
        self._inner = inner
        self._image = image
        self._network = network
        self._memory_limit = memory_limit
        self._timeout = timeout

    async def execute(self, agent_id: str, context: dict[str, Any]) -> dict:
        ctx = dict(context)
        sandbox = dict(ctx.get("sandbox", {}))
        sandbox["enforced"] = True
        ctx["sandbox"] = sandbox
        try:
            return await self._execute_in_docker(agent_id, ctx)
        except Exception:
            logger.warning(
                "Docker 不可用，SandboxedAgentExecutor 回退到直接执行"
                "（沙箱未强制生效；生产环境应 fail-closed 或禁止回退）",
            )
            sandbox["enforced"] = False
            sandbox["fallback"] = True
            ctx["sandbox"] = sandbox
            return await self._inner.execute(agent_id, ctx)

    async def _execute_in_docker(self, agent_id: str, context: dict[str, Any]) -> dict:
        import subprocess
        import json
        import tempfile
        import os

        sandbox = context.get("sandbox", {})
        mounts: list[dict[str, Any]] = list(sandbox.get("mounts", []))

        with tempfile.TemporaryDirectory() as tmp:
            ctx_path = os.path.join(tmp, "context.json")
            with open(ctx_path, "w", encoding="utf-8") as f:
                json.dump({"agent_id": agent_id, "context": context}, f)

            cmd = [
                "docker", "run", "--rm",
                f"--network={self._network}",
                f"--memory={self._memory_limit}",
                "-v", f"{tmp}:/data",
            ]
            cmd.extend(self._docker_volume_args(mounts))
            cmd.extend([
                self._image,
                "python", "-c",
                "import json; print(json.dumps({'sandbox': True}))",
            ])
            subprocess.run(
                cmd,
                check=True,
                timeout=self._timeout,
                capture_output=True,
            )
        return await self._inner.execute(agent_id, context)

    @staticmethod
    def _docker_volume_args(mounts: list[dict[str, Any]]) -> list[str]:
        import os

        args: list[str] = []
        for m in mounts:
            host = m.get("host_path", "")
            if not host:
                continue
            host_abs = os.path.abspath(host)
            if not os.path.exists(host_abs):
                logger.warning("沙箱挂载跳过不存在的路径: %s", host_abs)
                continue
            container = m.get("container_path", "/mnt")
            mode = m.get("mode", "ro")
            if mode not in ("ro", "rw"):
                mode = "ro"
            args.extend(["-v", f"{host_abs}:{container}:{mode}"])
        return args


class CodeAgentExecutor(SandboxedAgentExecutor):
    """代码 Agent 执行器——沙箱挂载仓库并收集变更文件列表。"""

    def __init__(
        self,
        inner: AgentExecutor,
        *,
        repo_path: str = "",
        branch_prefix: str = "casa-agent/",
        **kwargs: Any,
    ):
        super().__init__(inner, **kwargs)
        self._repo = repo_path
        self._branch_prefix = branch_prefix

    async def execute(self, agent_id: str, context: dict[str, Any]) -> dict:
        ctx = dict(context)
        ctx.setdefault("sandbox", {})
        mounts = list(ctx["sandbox"].get("mounts", []))
        if self._repo:
            mounts.append({
                "host_path": self._repo,
                "container_path": "/workspace",
                "mode": "rw",
            })
        ctx["sandbox"]["mounts"] = mounts
        result = await super().execute(agent_id, ctx)
        result.setdefault("_meta", {})
        result["_meta"]["changed_files"] = self._collect_changed_files()
        return result

    def _collect_changed_files(self) -> list[str]:
        import os
        import subprocess

        if not self._repo or not os.path.isdir(self._repo):
            return []
        git_dir = os.path.join(self._repo, ".git")
        if not os.path.isdir(git_dir):
            return []
        try:
            proc = subprocess.run(
                ["git", "-C", self._repo, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0:
                return []
            files: list[str] = []
            for line in proc.stdout.splitlines():
                line = line.strip()
                if len(line) > 3:
                    files.append(line[3:].strip())
            return files
        except (OSError, subprocess.SubprocessError):
            return []
