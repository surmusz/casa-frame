"""Artifact 路径安全工具。"""
from __future__ import annotations

import os
import re
import unicodedata

# ============================================================================
# 路径安全
# ============================================================================

_INVALID_PATH_RE = re.compile(r"\.\.[/\\]|[\\/]\.\.")


def _validate_path_component(name: str, label: str = "path") -> None:
    """拒绝含路径遍历字符、null 字节、或控制字符的组件。"""
    if not name or not name.strip():
        raise ValueError(f"{label} 不能为空")
    if "\x00" in name:
        raise ValueError(f"{label} 不能包含 null 字节: {name!r}")
    if _INVALID_PATH_RE.search(name):
        raise ValueError(f"{label} 包含非法字符: {name!r}")
    # 禁止含 / 或 \ 的组件
    if "/" in name or "\\" in name:
        raise ValueError(f"{label} 不能含路径分隔符: {name!r}")
    # 禁止 Unicode 控制字符（BIDI, ZWS 等）
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError(f"{label} 不能包含控制字符: {name!r}")


def _safe_join(base: str, *components: str) -> str:
    """安全路径拼接：校验组件后 join + realpath 验证。"""
    for c in components:
        _validate_path_component(c)
    joined = os.path.join(base, *components)
    real = os.path.realpath(joined)
    real_base = os.path.realpath(base)
    if not real.startswith(real_base + os.sep) and real != real_base:
        raise ValueError(f"路径逃逸检测: {joined} → {real}")
    return joined


def _job_root(base_dir: str, tenant_id: str, job_id: str) -> str:
    if tenant_id:
        return os.path.join(base_dir, tenant_id, job_id)
    return os.path.join(base_dir, job_id)


def _plan_rel_path(base_dir: str, tenant_id: str, job_id: str, plan_id: str) -> str:
    from ..config import ArtifactDir

    return os.path.join(_job_root(base_dir, tenant_id, job_id), "plans", plan_id, ArtifactDir.ARTIFACTS)
