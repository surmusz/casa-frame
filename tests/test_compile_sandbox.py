"""生产加固测试。"""
import tempfile

import pytest

from casa.authority import CapabilityMatrix, CapabilityRow
from casa.config import LLMProviderConfig, init_config, override_config
from casa.artifact import ArtifactStore
from casa.orchestration import (
    CompileRequest, MockAgentExecutor, PlanCompiler,
    PlanExecutor, SandboxedAgentExecutor, StageRunner,
)
from casa.scheduler import InMemorySchedulerBackend, SessionScheduler


def test_resolve_llm_provider_per_provider():
    init_config(
        llm_api_key="global-key",
        llm_base_url="https://global.example",
        llm_default_provider="openai",
        llm_providers={
            "anthropic": LLMProviderConfig(
                provider="anthropic",
                api_key="anthropic-key",
                base_url="https://anthropic.example",
                default_model="claude-3",
            ),
        },
    )
    from casa.config import get_config

    cfg = get_config()
    openai_cfg = cfg.resolve_llm_provider("openai")
    assert openai_cfg.api_key == "global-key"
    anthropic_cfg = cfg.resolve_llm_provider("anthropic")
    assert anthropic_cfg.api_key == "anthropic-key"
    assert anthropic_cfg.base_url == "https://anthropic.example"


def test_resolve_llm_provider_from_dict():
    init_config(
        llm_providers={
            "openai": {"api_key": "dict-key", "base_url": "https://openai.example"},
        },
    )
    from casa.config import get_config

    cfg = get_config()
    assert cfg.resolve_llm_provider("openai").api_key == "dict-key"


def test_eval_stage_injection_cap():
    matrix = CapabilityMatrix()
    for i in range(5):
        matrix.register(CapabilityRow(agent_id=f"worker{i}", data_write=f"out{i}"))
    matrix.register(CapabilityRow(agent_id="judge", is_evaluator=True, data_write="eval"))
    agent_io = {f"worker{i}": ([], f"out{i}") for i in range(5)}
    agent_io["judge"] = (["out0"], "eval")
    with override_config(max_eval_stages_per_plan=2):
        compiler = PlanCompiler(
            agent_io_map=agent_io,
            core_pipeline_ids={f"worker{i}" for i in range(5)},
            capability_matrix=matrix,
        )
        result = compiler.compile(
            CompileRequest(seed_stages=[{"agent_id": f"worker{i}"} for i in range(5)]),
        )
    eval_stages = [s for s in result.plan.stages if s.stage_role == "evaluator"]
    assert len(eval_stages) == 2


def test_trim_context_zero_budget():
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        runner = StageRunner(store=store, executor=MockAgentExecutor({}))
        from casa.orchestration import Stage, Plan

        ctx = runner._build_execute_context(
            Stage(stage_id="w", agent_id="w", context_limit_tokens=0),
            Plan(),
            fresh_session=False,
        )
        assert ctx["inputs"] == {"_trimmed": "budget=0, all inputs dropped"}
        assert "context_trimmed" in ctx.get("_meta", {})


@pytest.mark.asyncio
async def test_sandbox_fallback_marks_enforced_false(monkeypatch):
    captured: dict = {}

    class Inner:
        async def execute(self, agent_id: str, context: dict) -> dict:
            captured.update(context)
            return {"ok": True}

    async def _fail_docker(self, agent_id: str, context: dict) -> dict:
        raise RuntimeError("docker unavailable")

    sandboxed = SandboxedAgentExecutor(Inner())
    monkeypatch.setattr(sandboxed, "_execute_in_docker", _fail_docker.__get__(sandboxed, SandboxedAgentExecutor))
    await sandboxed.execute("w", {"sandbox": {"max_memory_mb": 256}})
    assert captured["sandbox"]["enforced"] is False
    assert captured["sandbox"]["fallback"] is True


def test_fifo_dequeue_on_submit_when_slot_available():
    class SlotBackend(InMemorySchedulerBackend):
        def __init__(self):
            super().__init__()
            self._fail_next_acquire = False

        def try_acquire_slot(self, session_id: str, slots_needed: int, cap: int):
            if self._fail_next_acquire:
                self._fail_next_acquire = False
                return False, self.get_active(session_id)
            return super().try_acquire_slot(session_id, slots_needed, cap)

    backend = SlotBackend()
    init_config(max_parallel_per_session=1, concurrency_policy="fifo")
    sched = SessionScheduler(backend=backend)
    r1 = sched.submit("sess")
    assert r1.status == "accepted"
    backend.set_active("sess", 0)
    backend._fail_next_acquire = True
    r2 = sched.submit("sess")
    assert r2.status == "accepted"


@pytest.mark.asyncio
async def test_stage_priority_ordering():
    order: list[str] = []

    async def track(agent_id: str, ctx: dict) -> dict:
        order.append(agent_id)
        return {"v": 1}

    class TrackingExecutor:
        async def execute(self, agent_id: str, context: dict) -> dict:
            return await track(agent_id, context)

    matrix = CapabilityMatrix()
    matrix.register(CapabilityRow(agent_id="core", data_write="core_out", priority=10))
    matrix.register(CapabilityRow(agent_id="extra", data_write="extra_out", priority=200))
    agent_io = {
        "core": ([], "core_out"),
        "extra": ([], "extra_out"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        init_config(artifact_base_dir=tmp)
        store = ArtifactStore("j")
        store.init_plan("p")
        compiler = PlanCompiler(
            agent_io_map=agent_io,
            core_pipeline_ids={"core", "extra"},
            capability_matrix=matrix,
        )
        plan = compiler.compile(
            CompileRequest(seed_stages=[{"agent_id": "core"}, {"agent_id": "extra"}]),
        ).plan
        runner = StageRunner(store=store, executor=TrackingExecutor())
        await PlanExecutor(
            store=store, stage_runner=runner, max_parallel_per_wave=1,
        ).execute(plan)
    assert order.index("core") < order.index("extra")
