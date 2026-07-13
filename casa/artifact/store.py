"""ArtifactStore 与定义。"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import get_config, ArtifactDir, StorageBackend, Scope
from ..observability import get_run_context, record_metric
from ..audit import emit_audit
from ._path import _safe_join, _validate_path_component, _job_root, _plan_rel_path

logger = logging.getLogger("casa.artifact")

from .backend import (
    ArtifactBackend, LocalArtifactBackend, S3ArtifactBackend, _resolve_backend,
)

# ============================================================================
# Artifact 定义（ArtifactDefinition）
# ============================================================================


@dataclass(kw_only=True)
class ArtifactDefinition:
    """
    注册在 ArtifactDictionary 中的一个产物类型。

    领域项目在启动时注册所有 artifact kind：
        reg = ArtifactDictionary()
        reg.register(ArtifactDefinition(kind="raw_data", schema_path="schemas/raw_data.json"))
        reg.register(ArtifactDefinition(kind="theme_analytics", producer="theme_analyst"))
    """

    kind: str
    description: str = ""
    schema_path: str = ""  # JSON Schema 文件路径
    schema_version: int = 1
    producer: str = ""      # 生产该产物的 agent_id
    scope: str = Scope.JOB
    is_terminal: bool = False  # 是否终端产物（如终态报告）
    is_required: bool = False  # 是否 core pipeline 必需产物
    retention_tier: str = "job"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "schema_version": self.schema_version,
            "producer": self.producer,
            "scope": self.scope,
            "is_terminal": self.is_terminal,
            "is_required": self.is_required,
            "retention_tier": self.retention_tier,
        }


# ============================================================================
# 全局产物注册表（ArtifactDictionary）
# ============================================================================


class ArtifactDictionary:
    """
    全局产物注册表。领域项目启动时注册所有 artifact kind。

    使用方式：
        reg = ArtifactDictionary()
        reg.register(ArtifactDefinition(kind="raw_data", ...))
        reg.register(ArtifactDefinition(kind="theme_analytics", ...))

        # 查询
        prod = reg.producer_of("theme_analytics")  # "theme_analyst"
        kinds = reg.list_kinds()                   # ["raw_data", "theme_analytics", ...]
    """

    def __init__(self):
        self._defs: dict[str, ArtifactDefinition] = {}

    def register(self, ad: ArtifactDefinition) -> None:
        if ad.kind in self._defs:
            logger.warning("Artifact kind %r 被重复注册，将覆盖", ad.kind)
        self._defs[ad.kind] = ad

    def producer_of(self, kind: str) -> str:
        ad = self._defs.get(kind)
        return ad.producer if ad else ""

    def list_kinds(self) -> list[str]:
        return sorted(self._defs.keys())

    def list_terminal(self) -> list[str]:
        return [k for k, ad in self._defs.items() if ad.is_terminal]

    def list_required(self) -> list[str]:
        return [k for k, ad in self._defs.items() if ad.is_required]

    def get(self, kind: str) -> ArtifactDefinition | None:
        return self._defs.get(kind)

    def to_catalog(self) -> list[dict[str, Any]]:
        return [ad.to_dict() for ad in self._defs.values()]


# ============================================================================
# 产物存储（ArtifactStore）— 统一读写接口
# ============================================================================


class ArtifactStore:
    """
    统一 Artifact 读写 facade。

    领域目切换存储后端只需改 CASA_ARTIFACT_STORAGE_BACKEND 环境变量，
    无需修改调用方代码。

    使用方式：
        store = ArtifactStore(job_id="j001")
        store.init_plan("plan_001")
        store.write("theme_analytics", {"themes": [...]})
        data = store.read("theme_analytics")
    """

    def __init__(
        self,
        job_id: str,
        *,
        base_dir: str | None = None,
        plan_id: str | None = None,
        tenant_id: str | None = None,
    ):
        _validate_path_component(job_id, "job_id")
        config = get_config()
        self.job_id = job_id
        self.tenant_id = tenant_id if tenant_id is not None else config.tenant_id
        if self.tenant_id:
            _validate_path_component(self.tenant_id, "tenant_id")
        self.base_dir = base_dir or config.artifact_base_dir
        self._plan_id: str | None = plan_id
        self._plan_dir: str = ""
        self._backend = _resolve_backend(config)

        # 若构造时传了 plan_id，自动初始化 plan 目录
        if self._plan_id:
            self.init_plan(self._plan_id)

    @property
    def plan_id(self) -> str:
        return self._plan_id or "default"

    @property
    def plan_dir(self) -> str:
        """当前 plan 的 artifact 目录路径。"""
        return self._plan_dir

    def init_plan(self, plan_id: str) -> None:
        """为新 plan 创建目录结构。"""
        _validate_path_component(plan_id, "plan_id")
        self._plan_id = plan_id
        parts: list[str] = []
        if self.tenant_id:
            parts.append(self.tenant_id)
        parts.extend([self.job_id, "plans", plan_id, ArtifactDir.ARTIFACTS])
        self._plan_dir = _safe_join(self.base_dir, *parts)
        os.makedirs(self._plan_dir, exist_ok=True)

    def write(self, artifact_kind: str, data: dict, *, coordination_hint: dict | None = None) -> None:
        """写一个 artifact；可选 ``coordination_hint`` 供下游 Agent 轻量协调。"""
        if not self._plan_dir:
            raise ValueError("ArtifactStore 未 init_plan，拒绝写入")
        _validate_path_component(artifact_kind, "artifact_kind")
        payload_bytes = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self._backend.write(self._storage_key(artifact_kind), data, self._plan_dir, artifact_kind)
        if coordination_hint:
            hint_bytes = json.dumps(coordination_hint, ensure_ascii=False).encode("utf-8")
            self.write_deliverable_file(hint_bytes, filename=f".hint_{artifact_kind}.json")
        ctx = get_run_context()
        tags: dict[str, str] = {"artifact_kind": artifact_kind, "job_id": self.job_id}
        if ctx and ctx.run_id:
            tags["run_id"] = ctx.run_id
        if ctx and ctx.stage_id:
            tags["stage_id"] = ctx.stage_id
        record_metric("artifact.size_bytes", float(payload_bytes), **tags)
        emit_audit(
            "artifact.written",
            actor=ctx.stage_id if ctx and ctx.stage_id else "",
            artifact_kind=artifact_kind,
            size_bytes=payload_bytes,
            job_id=self.job_id,
        )

    def health(self) -> dict[str, Any]:
        """检查 artifact 存储是否可用（base_dir 可写）。"""
        writable = False
        error = ""
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            test_path = os.path.join(self.base_dir, ".casa_health_check")
            with open(test_path, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test_path)
            writable = True
        except OSError as exc:
            error = str(exc)
        return {
            "status": "ok" if writable else "degraded",
            "base_dir": self.base_dir,
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "plan_id": self.plan_id,
            "backend": type(self._backend).__name__,
            "writable": writable,
            "error": error,
        }

    def read(self, artifact_kind: str) -> dict | None:
        """读一个 artifact。不存在返回 None。"""
        if not self._plan_dir:
            raise ValueError("ArtifactStore 未 init_plan，拒绝读取")
        _validate_path_component(artifact_kind, "artifact_kind")
        data = self._backend.read(
            self._storage_key(artifact_kind), self._plan_dir, artifact_kind,
        )
        if data is None:
            legacy_key = self._legacy_storage_key(artifact_kind)
            if legacy_key:
                data = self._backend.read(legacy_key, self._plan_dir, artifact_kind)
        return data

    def _legacy_storage_key(self, kind: str) -> str | None:
        if self.tenant_id:
            return f"artifacts/{self.job_id}/{self.plan_id}/{kind}"
        return None

    def _artifact_storage_prefix(self) -> str:
        if self.tenant_id:
            return f"artifacts/{self.tenant_id}/{self.job_id}/{self.plan_id}/"
        return f"artifacts/{self.job_id}/{self.plan_id}/"

    def _legacy_storage_prefix(self) -> str | None:
        """无 tenant 前缀的旧 S3 布局（向后兼容）。"""
        if self.tenant_id:
            return f"artifacts/{self.job_id}/{self.plan_id}/"
        return None

    def exists(self, artifact_kind: str) -> bool:
        """检查 artifact 是否已存在（用于幂等 skip）。"""
        if not self._plan_dir:
            return False
        _validate_path_component(artifact_kind, "artifact_kind")
        prefix = self._artifact_storage_prefix()
        if self._backend.exists(
            self._plan_dir, artifact_kind, storage_prefix=prefix,
        ):
            return True
        legacy = self._legacy_storage_prefix()
        if legacy:
            return self._backend.exists(
                self._plan_dir, artifact_kind, storage_prefix=legacy,
            )
        return False

    def delete(self, artifact_kind: str) -> bool:
        """删除 artifact（若存在）。"""
        if not self._plan_dir:
            raise ValueError("ArtifactStore 未 init_plan，拒绝删除")
        _validate_path_component(artifact_kind, "artifact_kind")
        prefix = self._artifact_storage_prefix()
        deleted = self._backend.delete(self._plan_dir, artifact_kind, storage_prefix=prefix)
        legacy = self._legacy_storage_prefix()
        if legacy:
            deleted = self._backend.delete(
                self._plan_dir, artifact_kind, storage_prefix=legacy,
            ) or deleted
        return deleted

    def list_artifacts(self) -> list[str]:
        """列出当前 plan 下所有 artifact kind。"""
        if not self._plan_dir:
            return []
        keys = set(self._backend.list_keys(
            self._plan_dir, storage_prefix=self._artifact_storage_prefix(),
        ))
        legacy = self._legacy_storage_prefix()
        if legacy:
            keys.update(self._backend.list_keys(self._plan_dir, storage_prefix=legacy))
        return sorted(keys)

    def read_coordination_hint(self, artifact_kind: str) -> dict | None:
        """读取上游 artifact 附带的协调提示（不污染 artifact 本体）。"""
        _validate_path_component(artifact_kind, "artifact_kind")
        raw = self.read_deliverable_file(f".hint_{artifact_kind}.json")
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def write_text(
        self,
        artifact_kind: str,
        content: str,
        *,
        coordination_hint: dict | None = None,
    ) -> None:
        """写纯文本 artifact（代码文件等）——内部包装为 dict。"""
        self.write(artifact_kind, {"_text_content": content}, coordination_hint=coordination_hint)

    def read_text(self, artifact_kind: str) -> str | None:
        """读纯文本 artifact。"""
        data = self.read(artifact_kind)
        if data is None:
            return None
        return data.get("_text_content", "")

    def _storage_key(self, kind: str) -> str:
        if self.tenant_id:
            return f"artifacts/{self.tenant_id}/{self.job_id}/{self.plan_id}/{kind}"
        return f"artifacts/{self.job_id}/{self.plan_id}/{kind}"

    # --- 报告读写 ---
    def write_deliverable_file(self, data: bytes, filename: str = "output.json") -> str:
        """写入终态交付物文件（推荐路径，替代 write_report）。"""
        _validate_path_component(filename, "filename")
        return self._backend.write_deliverable_file(
            data, self.base_dir, self.tenant_id, self.job_id, self.plan_id, filename
        )

    def write_report(self, data: bytes, filename: str = "report.html") -> str:
        """已弃用：请使用 write_deliverable_file 或 DeliverableRenderer。"""
        return self.write_deliverable_file(data, filename=filename)

    def read_deliverable_file(self, filename: str = "output.json") -> bytes | None:
        if not self._plan_id:
            raise ValueError("ArtifactStore 未 init_plan，拒绝读取 deliverable")
        _validate_path_component(filename, "filename")
        return self._backend.read_deliverable_file(
            self.base_dir, self.tenant_id, self.job_id, self.plan_id, filename
        )

    def read_report(self, filename: str = "report.html") -> bytes | None:
        """已弃用：请使用 read_deliverable_file。"""
        return self.read_deliverable_file(filename=filename)

