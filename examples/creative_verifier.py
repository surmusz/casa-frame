"""
creative_verifier — AgentLoop 创意评估 verifier 参考示例。

演示独立评估模型按多维度审校生成内容，返回问题列表驱动迭代。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from casa.loop import VerifierContext

LLMEvalFn = Callable[[str, str], Awaitable[str]]


async def creative_verifier(
    ctx: VerifierContext,
    *,
    llm_eval: LLMEvalFn | None = None,
    artifacts_summary: str = "",
) -> list[str]:
    """
    按语调 / 情节 / 角色一致性评估，返回需修复的问题列表。

    参数:
        ctx: AgentLoop 只读快照
        llm_eval: async (system, user) -> 评估文本；未提供时用规则占位
        artifacts_summary: 当前产物摘要（由调用方注入）
    """
    if llm_eval:
        system = (
            "你是独立创意审校者。按以下维度评估：语调一致性、情节连续性、角色一致性。"
            "仅输出 JSON 数组字符串，每项为一条需修复的问题；无问题则输出 []。"
        )
        user = (
            f"意图: {ctx.intent}\n"
            f"迭代: {ctx.loop_iteration}/{ctx.max_iterations}\n"
            f"产物: {artifacts_summary or '(无)'}\n"
        )
        raw = await llm_eval(system, user)
        return _parse_issues(raw)

    issues: list[str] = []
    if not ctx.prior_success and ctx.loop_iteration == 1:
        issues.append("首轮执行未成功，需检查上游 stage")
    if "待补充" in artifacts_summary:
        issues.append("章节内容含占位符「待补充」")
    if len(artifacts_summary) < 50 and ctx.prior_success:
        issues.append("产物过短，可能未完成章节展开")
    return issues


def _parse_issues(raw: str) -> list[str]:
    import json

    raw = raw.strip()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except json.JSONDecodeError:
        pass
    if not raw or raw == "[]":
        return []
    return [line.strip("- ").strip() for line in raw.splitlines() if line.strip()]


# --- 用法示例 ---
async def _demo():
    from casa import AgentLoop, Orchestrator

    async def mock_llm(system: str, user: str) -> str:
        return '["结尾转折生硬", "主角动机与前章不一致"]'

    orch = Orchestrator(...)  # noqa: 填入实际 compiler/executor
    loop = AgentLoop(
        orchestrator=orch,
        verifier=lambda ctx: creative_verifier(
            ctx,
            llm_eval=mock_llm,
            artifacts_summary="第三章：主角突然放弃目标（待补充）",
        ),
        max_iterations=3,
    )
    # result = await loop.run("写一部长篇奇幻小说前三章", router=...)
