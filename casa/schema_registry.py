"""
CASA Schema 注册表 — 带兼容性检查的版本化 artifact schema。
"""

from __future__ import annotations

import abc
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SchemaRecord:
    artifact_kind: str
    version: int
    schema: dict[str, Any]


class SchemaRegistry(abc.ABC):
    @abc.abstractmethod
    def register(self, artifact_kind: str, version: int, schema: dict[str, Any]) -> None:
        ...

    @abc.abstractmethod
    def get(self, artifact_kind: str, version: int = 1) -> dict[str, Any] | None:
        ...

    @abc.abstractmethod
    def check_compatible(self, artifact_kind: str, old_version: int, new_version: int) -> bool:
        ...


class InMemorySchemaRegistry(SchemaRegistry):
    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register(self, artifact_kind: str, version: int, schema: dict[str, Any]) -> None:
        with self._lock:
            self._schemas[(artifact_kind, version)] = dict(schema)

    def get(self, artifact_kind: str, version: int = 1) -> dict[str, Any] | None:
        with self._lock:
            return self._schemas.get((artifact_kind, version))

    def check_compatible(self, artifact_kind: str, old_version: int, new_version: int) -> bool:
        if new_version < old_version:
            return False
        old = self.get(artifact_kind, old_version)
        new = self.get(artifact_kind, new_version)
        if old is None or new is None:
            return True
        old_req = set(old.get("required", []))
        new_req = set(new.get("required", []))
        return old_req <= new_req


_default_registry: InMemorySchemaRegistry | None = None
_lock = threading.Lock()


def get_schema_registry() -> InMemorySchemaRegistry:
    global _default_registry
    if _default_registry is None:
        with _lock:
            if _default_registry is None:
                _default_registry = InMemorySchemaRegistry()
    return _default_registry


def reset_schema_registry() -> None:
    global _default_registry
    with _lock:
        _default_registry = InMemorySchemaRegistry()
