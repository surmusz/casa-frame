"""
CASA 模块烟雾测试 — 验证核心模块可导入与基本行为。

用法: python scripts/casa_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile

from casa.config import init_config, get_config, override_config, reset_config
from casa.scope import RefID
from casa.contract import (
    Contract,
    ContractMaterializer,
    ContractValidationError,
    BaseDeliverable,
    BaseRequired,
)
from casa.artifact import ArtifactSchemaValidator
from casa.scheduler import SessionScheduler, InMemorySchedulerBackend
from casa.orchestration import PlanCompiler, CompileRequest, Stage

passed = 0
failed = 0


def fails(fn):
    """若 fn 抛出异常则返回 True。"""
    try:
        fn()
        return False
    except Exception:
        return True


def _check_colons_job():
    RefID.job_artifact("bad:id", "kind")


def _check_colons_session():
    RefID.session_doc("bad:id", "doc")


def _check_colons_global():
    RefID.global_kb("bad:global")


def _check_colons_plan():
    RefID.plan_artifact("bad:plan", "kind")


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name}")


def main() -> int:
    global passed, failed
    passed = 0
    failed = 0

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 60)
    print("CASA Smoke Tests — R4-R8 Fix Verification")
    print("=" * 60)

    # ─── R4-S1：RefID 输入含冒号校验 ───
    print("\n## R4-S1: RefID factory colons validation")
    check("job_artifact rejects ':'", fails(_check_colons_job))
    check("session_doc rejects ':'", fails(_check_colons_session))
    check("global_kb rejects ':'", fails(_check_colons_global))
    check("plan_artifact rejects ':'", fails(_check_colons_plan))

    # ─── R4-S2：override_config 保留敏感值 ───
    print("\n## R4-S2: override_config preserves sensitive values")
    init_config(llm_api_key="sk-real-key-123", debug=False)
    with override_config(debug=True):
        cfg = get_config()
        check("sensitive value preserved in override", cfg.llm_api_key == "sk-real-key-123")
        check("overridden value applied", cfg.debug is True)
    reset_config()

    # ─── R4-C1：Scheduler 原子 try_acquire_slot ───
    print("\n## R4-C1: Scheduler atomic slot acquisition")
    backend = InMemorySchedulerBackend()
    sched = SessionScheduler(backend=backend)

    init_config(max_parallel_per_session=2, concurrency_policy="fifo")
    result = sched.submit("s1", intent="test1")
    check("submit acquires slot", result.status == "accepted")

    result2 = sched.submit("s1", intent="test2")
    check("second submit acquires slot", result2.status == "accepted")

    result3 = sched.submit("s1", intent="test3")
    check("third submit queues (FIFO)", result3.status == "queued")

    sched.release("s1", result.run.run_id)
    check("active still 2 (queued run dequeued)", sched.active_count("s1") == 2)

    sched.release("s1", result2.run.run_id)
    check("active count down to 1", sched.active_count("s1") == 1)

    reset_config()

    # ─── R5-A1：artifact_kind 使用一致性 ───
    print("\n## R5-A1: artifact_kind consistency")
    stage = Stage(
        stage_id="s1",
        agent_id="agent_a",
        output_artifact_kind="custom_output",
        depends_on=[],
        input_refs=[],
    )
    check(
        "output_artifact_kind != agent_id",
        stage.output_artifact_kind != stage.agent_id,
    )

    # ─── R5-O1：ContractMaterializer 含 session_id/user_id ───
    print("\n## R5-O1: ContractMaterializer includes session/user IDs")
    contract = Contract(
        deliverable=BaseDeliverable(type="full"),
        required=BaseRequired(mode="analyze"),
        session_id="s_test_001",
        user_id="u_test_001",
    )
    ctx = ContractMaterializer.materialize(contract)
    check("materialized has session_id", ctx.get("session_id") == "s_test_001")
    check("materialized has user_id", ctx.get("user_id") == "u_test_001")

    # ─── R6-L1：seed_stages 过滤空 agent_id ───
    print("\n## R6-L1: seed_stages filters empty agent_id")
    agent_io = {"agent_a": ([], "artifact_a")}
    compiler = PlanCompiler(agent_io_map=agent_io)
    req = CompileRequest(
        seed_stages=[
            {"agent_id": ""},
            {"agent_id": "agent_a"},
        ]
    )
    result = compiler.compile(req)
    check("compile succeeds with empty seed", result is not None)
    check("agent_a is selected", "agent_a" in result.selected_agents)
    check("empty string not in selected", "" not in result.selected_agents)

    # ─── R6-L2：ArtifactSchemaValidator 路径遍历防护 ───
    print("\n## R6-L2: ArtifactSchemaValidator path traversal protection")
    with tempfile.TemporaryDirectory() as td:
        validator = ArtifactSchemaValidator(schema_dir=td)
        check("valid path OK", validator.validate("safe_name", {}) == [])
        try:
            validator.validate("../../etc/passwd", {})
            check("path traversal rejected", False)
        except ValueError:
            check("path traversal rejected", True)

    # ─── R7-P1：已取消 task 被 await ───
    print("\n## R7-P1: PlanExecutor awaiting cancelled tasks")
    execute_path = os.path.join(repo_root, "casa", "orchestration", "execute.py")
    plan_exec_path = os.path.join(repo_root, "casa", "orchestration", "plan_executor.py")
    with open(execute_path) as f:
        content = f.read()
    with open(plan_exec_path) as f:
        content += f.read()
    has_gather_after_cancel = (
        "gather(*pending_tasks" in content or "gather(*remaining" in content
    )
    check("await gather after cancel is present", has_gather_after_cancel)

    # ─── R8-I1：Contract.validate() 校验 user_id ───
    print("\n## R8-I1: Contract.validate() checks user_id")
    init_config(contract_validation_enabled=True)
    c1 = Contract(
        deliverable=BaseDeliverable(type="full"),
        required=BaseRequired(mode="analyze"),
        session_id="s1",
        user_id="",
    )
    try:
        c1.validate()
        check("empty user_id rejected", False)
    except ContractValidationError as e:
        check("empty user_id rejected", "user_id" in str(e))

    c2 = Contract(
        deliverable=BaseDeliverable(type="full"),
        required=BaseRequired(mode="analyze"),
        session_id="s1",
        user_id="u1",
    )
    try:
        c2.validate()
        check("valid contract passes", True)
    except ContractValidationError:
        check("valid contract passes", False)
    reset_config()

    # ─── R8-I3：CLI 脚手架示例已修正 ───
    print("\n## R8-I3: CLI quick-start example")
    cli_path = os.path.join(repo_root, "casa", "cli.py")
    with open(cli_path) as f:
        cli_content = f.read()
    check("no broken preset_id='full'", 'preset_id="full"' not in cli_content)
    check("uses CompileRequest without preset", "CompileRequest(" in cli_content and "preset_id" not in cli_content.split("CompileRequest(")[1].split(")")[0])

    # ─── 附加回归测试 ───
    print("\n## Regression: RefID parsing and creation")
    ref = RefID.job_artifact("j001", "theme_analytics")
    check("ref_id format correct", str(ref) == "job:j001:artifact:theme_analytics")
    parsed = ref.parsed
    check("parsed scope is JOB", parsed and parsed.scope == "job")
    check("parsed job_id", parsed and parsed.job_id == "j001")
    check("parsed artifact_kind", parsed and parsed.artifact_kind == "theme_analytics")

    r2 = RefID.plan_artifact("p001", "analytics")
    check("plan ref format", str(r2) == "plan:p001:artifact:analytics")
    p2 = r2.parsed
    check("plan parsed scope", p2 and p2.scope == "plan")

    ref_template = RefID("job:{job_id}:artifact:analytics")
    resolved = ref_template.with_job_id("j456")
    check("with_job_id substitution", str(resolved) == "job:j456:artifact:analytics")

    print("\n## Regression: Contract validation")
    init_config(contract_validation_enabled=True)
    c = Contract(
        deliverable=BaseDeliverable(type="full"),
        required=BaseRequired(mode="analyze"),
        session_id="s1",
        user_id="u1",
    )
    c.validate()
    check("valid contract validates", True)

    c_bad = Contract(
        deliverable=BaseDeliverable(type="invalid_type"),
        required=BaseRequired(mode="analyze"),
        session_id="s1",
        user_id="u1",
    )
    try:
        c_bad.validate()
        check("invalid deliverable type rejected", False)
    except ContractValidationError:
        check("invalid deliverable type rejected", True)
    reset_config()

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
    print("=" * 60)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
