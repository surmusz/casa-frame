"""Replan 应对措施。"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("casa.orchestration")

class ReplanHandler:
    """
    Replan Handler：LLM 驱动的应对措施生成。

    领域项目可以接入自己的 LLM Gateway 实现 replan 逻辑。
    """

    def __init__(
        self,
        *,
        llm_call: Callable[[str, str], Awaitable[dict]] | None = None,
    ):
        """
        参数:
            llm_call: async (system_prompt, user_message) → dict
        """
        self._llm_call = llm_call

    async def decide_replan(
        self,
        failed_stage_id: str,
        failed_agent_id: str,
        error: str,
        completed_stages: list[str],
        artifact_kinds: list[str],
        deliverable_type: str = "full",
    ) -> dict[str, Any]:
        """
        返回 LLM 生成的应对措施 dict。
        领域项目需实现 llm_call 或重写此方法。
        """
        if not self._llm_call:
            return {
                "mitigation_stage": {
                    "stage_id": f"{failed_stage_id}_retry",
                    "agent_id": failed_agent_id,
                    "params": {"mitigation_reason": "auto retry", "error": error},
                },
                "summary": "自动重试",
            }

        user_msg = (
            f"阶段执行失败：{failed_stage_id} ({failed_agent_id})\n"
            f"错误：{error}\n"
            f"已完成阶段：{completed_stages}\n"
            f"已有 artifact：{artifact_kinds}\n"
            f"交付物类型：{deliverable_type}\n"
            f"请给出应对措施 JSON。"
        )

        try:
            return await self._llm_call(
                "你是 CASA 编排器。阶段失败时给出应对措施。仅输出 JSON。",
                user_msg,
            )
        except Exception as e:
            logger.warning("Replan LLM call failed: %s", e)
            return {
                "mitigation_stage": {
                    "stage_id": f"{failed_stage_id}_retry",
                    "agent_id": failed_agent_id,
                    "params": {"mitigation_reason": "fallback", "error": error},
                },
                "summary": "Fallback 重试",
            }
