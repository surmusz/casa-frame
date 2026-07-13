"""
CASA Intent Router — 将自然语言意图翻译为 Agent 集合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

LLMCall = Callable[[str, str], Awaitable[dict[str, Any]]]


@dataclass(kw_only=True)
class PresetCapability:
    preset_id: str
    display_name: str
    description: str
    agent_ids: list[str] = field(default_factory=list)
    deliverable_type: str = "full"
    tags: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class AgentCapability:
    """Agent 能力声明——IntentRouter 的匹配输入。"""

    agent_id: str
    display_name: str
    description: str
    input_artifacts: list[str] = field(default_factory=list)
    output_artifact: str = ""
    execution_profile: str = "harness"
    tags: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class IntentResult:
    """IntentRouter 的输出——供 CompileRequest 组装使用。"""

    agent_ids: list[str]
    policy: str = "for_user_start"
    summary: str = ""
    warnings: list[str] = field(default_factory=list)


class IntentRouter:
    """将自然语言意图翻译为 Agent 集合 + UsagePolicy。"""

    def __init__(
        self,
        *,
        catalog: dict[str, AgentCapability],
        llm_call: LLMCall | None = None,
    ):
        self._catalog = catalog
        self._llm_call = llm_call
        self._presets: dict[str, PresetCapability] = {}

    def register_presets(
        self,
        presets: dict[str, Any],
        descriptions: dict[str, str] | None = None,
    ) -> None:
        for pid, preset in presets.items():
            self._presets[pid] = PresetCapability(
                preset_id=pid,
                display_name=getattr(preset, "display_name", None) or pid,
                description=(descriptions or {}).get(pid, "")
                or getattr(preset, "display_name", "") or pid,
                agent_ids=list(getattr(preset, "selected_agent_ids", [])),
            )

    def find_preset(self, intent: str) -> PresetCapability | None:
        best: PresetCapability | None = None
        best_score = 0
        q = intent.lower()
        for p in self._presets.values():
            score = sum(1 for word in q.split() if word in p.description.lower())
            if score > best_score:
                best, best_score = p, score
        return best if best_score > 0 else None

    @property
    def presets(self) -> dict[str, PresetCapability]:
        return self._presets

    @property
    def catalog(self) -> dict[str, AgentCapability]:
        return self._catalog

    async def route(
        self,
        intent: str,
        *,
        deliverable_type: str = "full",
    ) -> IntentResult:
        """从意图中提取 Agent 集合。"""
        if not self._llm_call:
            return self._fallback(intent)

        catalog_text = "\n".join(
            f"- {a.agent_id}: {a.description}"
            + (f"（标签: {', '.join(a.tags)}）" if a.tags else "")
            for a in self._catalog.values()
        )

        system_prompt = (
            "你是 CASA 编排意图路由器。根据用户意图从 Agent 目录中选择必需的 Agent。\n"
            '只返回 JSON：{"agent_ids": [...], "policy": "..."}\n'
            "policy 可选值：for_user_start（完整执行）、for_patch（跳过已有结果）、"
            "for_preview（不执行核心管道）。\n"
            "规则：\n"
            "1. 只选择目录中存在的 agent_id，不要编造\n"
            "2. 选择最小必要集合——不选不需要的 Agent\n"
            "3. 如果用户意图模糊，选择最通用的分析 agent 集合\n"
            f"\nAgent 目录：\n{catalog_text}"
        )

        user_message = (
            f"用户意图：{intent}\n"
            f"交付物类型：{deliverable_type}"
        )

        try:
            raw = await self._llm_call(system_prompt, user_message)
        except Exception:
            return self._fallback(intent)

        return self._parse(raw, intent)

    def _parse(self, raw: dict[str, Any], intent: str) -> IntentResult:
        warnings: list[str] = []

        raw_ids: list[str] = raw.get("agent_ids", [])
        if not isinstance(raw_ids, list):
            raw_ids = []

        valid = [aid for aid in raw_ids if aid in self._catalog]
        invalid = [aid for aid in raw_ids if aid not in self._catalog]

        if invalid:
            warnings.append(f"LLM 返回了未知 agent_id，已丢弃：{invalid}")
        if not valid:
            warnings.append("LLM 未返回任何有效 agent_id，使用 fallback")
            return self._fallback(intent)

        policy = raw.get("policy", "for_user_start")
        if policy not in ("for_user_start", "for_patch", "for_preview"):
            policy = "for_user_start"

        return IntentResult(
            agent_ids=valid,
            policy=policy,
            summary=f"意图「{intent}」→ {len(valid)} 个 Agent",
            warnings=warnings,
        )

    def _fallback(self, intent: str) -> IntentResult:
        return IntentResult(
            agent_ids=list(self._catalog.keys()),
            policy="for_user_start",
            summary=f"意图「{intent}」→ 回退到全量 Agent（无 LLM）",
            warnings=["无 LLM 可用，使用全量 Agent 集合"],
        )

    @staticmethod
    def from_capability_matrix(
        matrix: Any,
        agent_io_map: dict[str, tuple[list[str], str]],
    ) -> IntentRouter:
        """从 CapabilityMatrix + agent_io_map 自动构建 catalog。"""
        catalog: dict[str, AgentCapability] = {}
        rows_fn = matrix.to_rows if hasattr(matrix, "to_rows") else matrix.list_rows
        for row in rows_fn():
            aid = row.agent_id
            inputs, output = agent_io_map.get(aid, ([], ""))
            catalog[aid] = AgentCapability(
                agent_id=aid,
                display_name=row.display_name or aid,
                description=row.task_template or row.display_name or aid,
                input_artifacts=list(inputs),
                output_artifact=output,
                execution_profile=row.execution_profile,
                tags=list(row.scope_tags or []),
            )
        return IntentRouter(catalog=catalog)
