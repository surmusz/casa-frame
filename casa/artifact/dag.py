"""Artifact DAG 与 schema 校验。"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from ._path import _validate_path_component

logger = logging.getLogger("casa.artifact")

# ============================================================================
# 从 I/O 图自动推导依赖（ArtifactDAG）
# ============================================================================


@dataclass
class ArtifactDAG:
    """
    Artifact 依赖图：从 producer 映射推导 stage 间依赖。

    使用方式：
        dag = ArtifactDAG.from_registry(agent_registry, artifact_dict)
        stages = dag.compute_dependencies(selected_agent_ids)
        waves = dag.partition_waves(stages)  # 波次并行
    """

    producers: dict[str, str] = field(default_factory=dict)      # artifact_kind → agent_id
    consumers: dict[str, list[str]] = field(default_factory=dict)  # artifact_kind → [consuming agent_ids]
    agent_inputs: dict[str, list[str]] = field(default_factory=dict)  # agent_id → [input artifact kinds]

    @classmethod
    def from_declarations(
        cls,
        agent_io: dict[str, tuple[list[str], str]],
    ) -> ArtifactDAG:
        """
        从 agent I/O 声明构建 DAG。

        参数:
            agent_io: {agent_id: (input_artifact_kinds, output_artifact_kind)}
        """
        producers: dict[str, str] = {}
        consumers: dict[str, list[str]] = {}
        agent_inputs: dict[str, list[str]] = {}

        for agent_id, (inputs, output) in agent_io.items():
            agent_inputs[agent_id] = list(inputs)
            if output:
                if output in producers:
                    logger.warning(
                        "Artifact kind %r 有多个 producer: %s, %s",
                        output, producers[output], agent_id,
                    )
                producers[output] = agent_id
            for inp in inputs:
                consumers.setdefault(inp, []).append(agent_id)

        dag = cls(producers=producers, consumers=consumers, agent_inputs=agent_inputs)
        dag._check_cycles()
        return dag

    def _check_cycles(self) -> None:
        """检测 agent 依赖图中是否有环。"""
        # 构建邻接表：agent → [为其输入产物的 agent 列表]
        adj: dict[str, list[str]] = {}
        for aid, inputs in self.agent_inputs.items():
            deps = []
            for kind in inputs:
                prod = self.producers.get(kind)
                if prod and prod != aid:
                    deps.append(prod)
            adj[aid] = deps

        # 三色 DFS 环检测
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {aid: WHITE for aid in adj}

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            for neighbor in adj.get(node, []):
                if color.get(neighbor) == GRAY:
                    return [node, neighbor]
                if color.get(neighbor) == WHITE:
                    cycle = dfs(neighbor)
                    if cycle:
                        if cycle[0] == cycle[-1]:
                            return cycle
                        return [node] + cycle
            color[node] = BLACK
            return None

        for aid in adj:
            if color.get(aid) == WHITE:
                cycle = dfs(aid)
                if cycle:
                    raise ValueError(f"Artifact DAG 循环依赖: {' → '.join(cycle)}")

    def producer_of(self, kind: str) -> str:
        return self.producers.get(kind, "")

    def consumers_of(self, kind: str) -> list[str]:
        return self.consumers.get(kind, [])

    def compute_dependencies(
        self, agent_ids: set[str],
    ) -> list[dict[str, Any]]:
        """
        计算 stage 间依赖。返回的每个 dict 含：
          - stage_id / agent_id
          - depends_on: 必须等待完成的 stage_id 列表
          - input_artifacts: 该 stage 需要的 artifact kind 列表

        参数:
            agent_ids: 参与本次 run 的 agent_id 集合
        """
        # 建立 agent_id → stage_id 映射
        stage_map: dict[str, str] = {aid: aid for aid in agent_ids}
        stages: list[dict[str, Any]] = []

        # 获取每个 agent 的 I/O
        for aid in sorted(agent_ids):
            inputs = self.agent_inputs.get(aid, [])
            # 查找 input artifact 的 producer
            deps: list[str] = []
            for kind in inputs:
                producer = self.producers.get(kind)
                if producer and producer in agent_ids and producer != aid:
                    deps.append(stage_map[producer])

            stages.append({
                "stage_id": aid,
                "agent_id": aid,
                "depends_on": list(dict.fromkeys(deps)),
                "input_artifacts": inputs,
                "output_artifact": self._output_for(aid),
            })

        return stages

    def partition_waves(
        self, stages: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """
        将 stages 按 depends_on 分组为并行波次。
        同一 wave 内的 stage 可并行执行。
        """
        remaining = {s["stage_id"]: s for s in stages}
        completed: set[str] = set()
        waves: list[list[dict[str, Any]]] = []

        while remaining:
            ready = [
                s
                for sid, s in remaining.items()
                if all(d in completed for d in s.get("depends_on", []))
            ]
            if not ready:
                stuck = sorted(remaining.keys())
                logger.error("DAG deadlock: pending=%s completed=%s", stuck, sorted(completed))
                raise RuntimeError(f"Artifact DAG 死锁: {stuck}")

            waves.append(ready)
            for s in ready:
                completed.add(s["stage_id"])
                del remaining[s["stage_id"]]

        return waves

    def to_mermaid(self, *, direction: str = "LR") -> str:
        """将依赖图导出为 Mermaid 流程图。"""
        lines = [f"flowchart {direction}"]
        for agent_id, inputs in self.agent_inputs.items():
            output = self._output_for(agent_id)
            label = f"{agent_id}"
            if output:
                label = f"{agent_id}\\n[{output}]"
            lines.append(f'    {agent_id}["{label}"]')
            for inp in inputs:
                prod = self.producers.get(inp, inp)
                lines.append(f"    {prod} --> {agent_id}")
        return "\n".join(lines)

    def to_dot(self) -> str:
        """将依赖图导出为 Graphviz DOT。"""
        lines = ["digraph ArtifactDAG {"]
        for agent_id, inputs in self.agent_inputs.items():
            for inp in inputs:
                prod = self.producers.get(inp, inp)
                lines.append(f'  "{prod}" -> "{agent_id}";')
        lines.append("}")
        return "\n".join(lines)

    def _output_for(self, agent_id: str) -> str:
        for kind, prod in self.producers.items():
            if prod == agent_id:
                return kind
        return ""

    def closure(
        self,
        seed_agent_ids: set[str],
        *,
        required_agent_ids: set[str] | None = None,
    ) -> set[str]:
        """
        从 seed agents 出发，自动包含缺失的 producer agents。

        参数:
            seed_agent_ids: 用户选择的 agent_id 集合
            required_agent_ids: 必须始终包含的 agent（core pipeline）
        """
        pool = set(seed_agent_ids)
        if required_agent_ids:
            pool.update(required_agent_ids)

        added: list[str] = []
        changed = True
        while changed:
            changed = False
            for aid in list(pool):
                for inp_kind in self.agent_inputs.get(aid, []):
                    producer = self.producers.get(inp_kind)
                    if producer and producer not in pool:
                        pool.add(producer)
                        added.append(producer)
                        changed = True

        logger.info("Artifact closure: seed=%d → pool=%d added=%s",
                     len(seed_agent_ids), len(pool), added)
        return pool


# ============================================================================
# Artifact 输出校验（SchemaValidator）
# ============================================================================


class ArtifactSchemaValidator:
    """
    基于 JSON Schema 的 artifact 输出校验。

    使用方式：
        v = ArtifactSchemaValidator(schema_dir="schemas/")
        errs = v.validate("theme_analytics", artifact_data)
        if errs:
            raise ArtifactValidationError(errs)
    """

    def __init__(self, schema_dir: str = "schemas"):
        self.schema_dir = schema_dir
        self._cache: dict[str, dict] = {}
        self._cache_lock = threading.Lock()

    def validate(self, artifact_kind: str, data: dict, schema_version: int = 1) -> list[str]:
        """
        校验 artifact 数据。

        返回:
            空列表表示通过；非空表示校验失败
        """
        schema = self._load_schema(artifact_kind)
        if not schema:
            return []  # 无 schema → 跳过校验

        errors = _validate_against_schema(data, schema)
        if errors:
            logger.warning(
                "Schema validation failed for %s (schema_version=%d): %s",
                artifact_kind, schema_version, errors[:3],
            )
        return errors

    def _load_schema(self, artifact_kind: str) -> dict | None:
        # 路径遍历防护
        _validate_path_component(artifact_kind, "artifact_kind")

        from ..schema_registry import get_schema_registry
        reg_schema = get_schema_registry().get(artifact_kind)
        if reg_schema:
            return reg_schema

        if artifact_kind in self._cache:
            with self._cache_lock:
                if artifact_kind in self._cache:
                    return self._cache[artifact_kind]

        path = os.path.join(self.schema_dir, f"{artifact_kind}.json")
        if not os.path.exists(path):
            return None

        try:
            with open(path, encoding="utf-8") as f:
                schema = json.load(f)
            with self._cache_lock:
                self._cache[artifact_kind] = schema
            return schema
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Schema load failed: %s — %s", path, e)
            return None


def _validate_against_schema(data: dict, schema: dict) -> list[str]:
    """
    轻量 schema 校验（不依赖 jsonschema 库）。

    领域项目可替换为 full jsonschema 校验：
        import jsonschema
        jsonschema.validate(data, schema)
    """
    errors: list[str] = []

    if "required" in schema:
        for field in schema["required"]:
            if field not in data or data[field] is None:
                errors.append(f"缺少必需字段: {field}")

    if "properties" in schema:
        for prop, rules in schema["properties"].items():
            if prop in data:
                val = data[prop]
                if "type" in rules:
                    expected = rules["type"]
                    if expected == "array" and not isinstance(val, list):
                        errors.append(f"{prop}: 期望 array，实际 {type(val).__name__}")
                    elif expected == "string" and not isinstance(val, str):
                        errors.append(f"{prop}: 期望 string，实际 {type(val).__name__}")
                    elif expected == "number" and not isinstance(val, (int, float)):
                        errors.append(f"{prop}: 期望 number，实际 {type(val).__name__}")
                    elif expected == "object" and not isinstance(val, dict):
                        errors.append(f"{prop}: 期望 object，实际 {type(val).__name__}")

    return errors


class ArtifactValidationError(Exception):
    """Artifact schema 校验失败。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))
