"""
CASA Agent 记忆 — 跨会话从历史执行中学习。
"""

from __future__ import annotations

import abc
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(kw_only=True)
class MemoryRecord:
    """一条经验记忆。"""

    record_id: str
    agent_id: str
    artifact_kind: str
    outcome: str = "success"
    error_type: str = ""
    quality_score: float = 1.0
    tokens_used: int = 0
    context_summary: str = ""
    created_at: str = ""
    ttl_hours: int = 168


class AgentMemory(abc.ABC):
    """Agent 经验记忆——用于跨运行学习。"""

    @abc.abstractmethod
    async def record(self, record: MemoryRecord) -> None:
        ...

    @abc.abstractmethod
    async def recall(self, agent_id: str, *, limit: int = 10) -> list[MemoryRecord]:
        ...

    @abc.abstractmethod
    async def recall_failures(self, agent_id: str, *, limit: int = 5) -> list[MemoryRecord]:
        ...


class InMemoryAgentMemory(AgentMemory):
    def __init__(self) -> None:
        self._records: list[MemoryRecord] = []
        self._lock = threading.Lock()

    async def record(self, record: MemoryRecord) -> None:
        if not record.created_at:
            record.created_at = datetime.now(timezone.utc).isoformat()
        if not record.record_id:
            record.record_id = f"mem_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._records.append(record)

    async def recall(self, agent_id: str, *, limit: int = 10) -> list[MemoryRecord]:
        with self._lock:
            return [r for r in self._records if r.agent_id == agent_id][-limit:]

    async def recall_failures(self, agent_id: str, *, limit: int = 5) -> list[MemoryRecord]:
        with self._lock:
            return [
                r for r in self._records
                if r.agent_id == agent_id and r.outcome == "failure"
            ][-limit:]


_default_memory: InMemoryAgentMemory | None = None
_memory_lock = threading.Lock()


def get_agent_memory() -> InMemoryAgentMemory:
    global _default_memory
    if _default_memory is None:
        with _memory_lock:
            if _default_memory is None:
                _default_memory = InMemoryAgentMemory()
    return _default_memory


def reset_agent_memory() -> None:
    global _default_memory
    with _memory_lock:
        _default_memory = InMemoryAgentMemory()
