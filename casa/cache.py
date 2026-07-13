"""
CASA Artifact Cache — 跨运行 artifact 复用。
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
import threading
from typing import Any, Callable


def cache_key(
    artifact_kind: str,
    input_refs: list[str],
    params: dict[str, Any],
    *,
    tenant_id: str = "",
    job_id: str = "",
    plan_id: str = "",
    inputs_fingerprint: str = "",
) -> str:
    """计算缓存键：kind + scope + inputs 摘要。"""
    payload = json.dumps({
        "kind": artifact_kind,
        "tenant_id": tenant_id,
        "job_id": job_id,
        "plan_id": plan_id,
        "inputs_fingerprint": inputs_fingerprint,
        "inputs": sorted(input_refs),
        "params": dict(sorted(params.items())),
    }, sort_keys=True, ensure_ascii=False)
    return f"{artifact_kind}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def inputs_fingerprint(store: Any, input_refs: list[str], *, extract_kind: Callable[[str], str]) -> str:
    """根据上游 artifact 内容计算指纹。"""
    parts: list[str] = []
    for ref in sorted(input_refs):
        kind = extract_kind(ref)
        data = store.read(kind)
        if data is None:
            parts.append(f"{kind}:missing")
        else:
            blob = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
            parts.append(f"{kind}:{hashlib.sha256(blob).hexdigest()[:12]}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class ArtifactCacheBackend(abc.ABC):
    """跨运行 artifact 缓存后端。"""

    @abc.abstractmethod
    def get(self, cache_key: str) -> dict | None:
        ...

    @abc.abstractmethod
    def put(self, cache_key: str, data: dict, metadata: dict | None = None) -> None:
        ...

    @abc.abstractmethod
    def invalidate(self, artifact_kind: str) -> int:
        """使指定 kind 的所有缓存失效。返回失效数量。"""
        ...

    def invalidate_key(self, cache_key: str) -> int:
        """使单个 cache_key 失效（默认实现委托 invalidate kind 前缀）。"""
        kind = cache_key.split(":", 1)[0]
        return self.invalidate(kind)


class LocalArtifactCache(ArtifactCacheBackend):
    """本地文件缓存。"""

    def __init__(self, cache_dir: str = "casa_cache"):
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = key.replace("/", "_")
        return os.path.join(self._cache_dir, f"{safe}.json")

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def put(self, key: str, data: dict, metadata: dict | None = None) -> None:
        path = self._path(key)
        with self._lock:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

    def invalidate(self, artifact_kind: str) -> int:
        count = 0
        safe_prefix = artifact_kind.replace("/", "_") + ":"
        with self._lock:
            for fname in os.listdir(self._cache_dir):
                if fname.startswith(safe_prefix):
                    os.remove(os.path.join(self._cache_dir, fname))
                    count += 1
        return count

    def invalidate_key(self, cache_key: str) -> int:
        path = self._path(cache_key)
        with self._lock:
            try:
                os.remove(path)
                return 1
            except FileNotFoundError:
                return 0
