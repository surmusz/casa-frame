"""
CASA 知识库 — 多后端 KB 抽象，含 KB 级访问控制。
"""

from __future__ import annotations

import abc
import asyncio
import inspect
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Scope


@dataclass(kw_only=True)
class KBFreshness:
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    ttl_seconds: int | None = None
    stale: bool = False


@dataclass(kw_only=True)
class KBEntry:
    entry_id: str
    content: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    freshness: KBFreshness = field(default_factory=KBFreshness)
    kb_id: str = ""
    # 可选元数据标签；框架不在此字段做访问控制，权限在 KBRegistry / kb_read 层。
    tags: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class KBEntrySummary:
    entry_id: str
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    kb_id: str = ""


class KnowledgeBase(abc.ABC):
    kb_id: str
    kb_type: str = "memory"
    scope: str = Scope.GLOBAL

    @abc.abstractmethod
    async def search(
        self, query: str, *, top_k: int = 5, filters: dict[str, Any] | None = None,
    ) -> list[KBEntry]:
        ...

    @abc.abstractmethod
    async def get(self, entry_id: str) -> KBEntry | None:
        ...

    @abc.abstractmethod
    async def list_entries(self, *, filters: dict[str, Any] | None = None) -> list[KBEntrySummary]:
        ...


def _run_async(coro: Any) -> Any:
    if not inspect.isawaitable(coro):
        return coro
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class InMemoryKnowledgeBase(KnowledgeBase):
    def __init__(
        self,
        kb_id: str,
        *,
        scope: str = Scope.GLOBAL,
        entries: dict[str, KBEntry] | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
    ):
        self.kb_id = kb_id
        self.kb_type = "memory"
        self.scope = scope
        self._entries = dict(entries or {})
        self._embed_fn = embed_fn
        if embed_fn:
            self._vectors: dict[str, list[float]] = {}
            for e in self._entries.values():
                self._vectors[e.entry_id] = embed_fn(str(e.content))

    async def search(self, query: str, *, top_k: int = 5, filters: dict[str, Any] | None = None) -> list[KBEntry]:
        if self._embed_fn:
            qv = self._embed_fn(query)
            scored: list[tuple[float, KBEntry]] = []
            for e in self._entries.values():
                ev = self._vectors.get(e.entry_id)
                if ev is None:
                    ev = self._embed_fn(str(e.content))
                    self._vectors[e.entry_id] = ev
                scored.append((_cosine_similarity(qv, ev), e))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for s, e in scored if s > 0][:top_k]
        q = query.lower()
        hits = [
            e for e in self._entries.values()
            if q in str(e.content).lower() or q in e.entry_id.lower()
        ]
        return hits[:top_k]

    async def get(self, entry_id: str) -> KBEntry | None:
        return self._entries.get(entry_id)

    async def list_entries(self, *, filters: dict[str, Any] | None = None) -> list[KBEntrySummary]:
        return [
            KBEntrySummary(
                entry_id=e.entry_id,
                title=str(e.metadata.get("title", e.entry_id)),
                kb_id=self.kb_id,
            )
            for e in self._entries.values()
        ]

    def put(self, entry: KBEntry) -> None:
        entry.kb_id = self.kb_id
        self._entries[entry.entry_id] = entry
        if self._embed_fn:
            self._vectors[entry.entry_id] = self._embed_fn(str(entry.content))


class CodebaseKnowledgeBase(KnowledgeBase):
    """仓库文件索引知识库——将源码文件索引为 KBEntry。"""

    def __init__(
        self,
        kb_id: str,
        *,
        repo_path: str = "",
        embed_fn: Callable[[str], list[float]] | None = None,
    ):
        self.kb_id = kb_id
        self.kb_type = "codebase"
        self.scope = Scope.GLOBAL
        self._repo = repo_path or "."
        self._embed_fn = embed_fn
        self._entries: dict[str, KBEntry] = {}
        self._vectors: dict[str, list[float]] = {}

    def index_files(self, patterns: list[str]) -> int:
        import glob
        import os

        count = 0
        root = os.path.abspath(self._repo)
        for pattern in patterns:
            for path in glob.glob(os.path.join(root, "**", pattern), recursive=True):
                if not os.path.isfile(path):
                    continue
                rel = os.path.relpath(path, root)
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                ext = os.path.splitext(path)[1].lstrip(".")
                entry = KBEntry(
                    entry_id=rel,
                    content=content,
                    kb_id=self.kb_id,
                    metadata={"path": rel, "lang": ext or "text"},
                )
                self._entries[rel] = entry
                if self._embed_fn:
                    self._vectors[rel] = self._embed_fn(content)
                count += 1
        return count

    async def search(
        self, query: str, *, top_k: int = 5, filters: dict[str, Any] | None = None,
    ) -> list[KBEntry]:
        if self._embed_fn:
            qv = self._embed_fn(query)
            scored: list[tuple[float, KBEntry]] = []
            for e in self._entries.values():
                ev = self._vectors.get(e.entry_id)
                if ev is None:
                    ev = self._embed_fn(str(e.content))
                    self._vectors[e.entry_id] = ev
                scored.append((_cosine_similarity(qv, ev), e))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for s, e in scored if s > 0][:top_k]
        q = query.lower()
        hits = [
            e for e in self._entries.values()
            if q in str(e.content).lower()
            or q in e.entry_id.lower()
            or q in str(e.metadata.get("path", "")).lower()
        ]
        return hits[:top_k]

    async def get(self, entry_id: str) -> KBEntry | None:
        return self._entries.get(entry_id)

    async def list_entries(self, *, filters: dict[str, Any] | None = None) -> list[KBEntrySummary]:
        return [
            KBEntrySummary(
                entry_id=e.entry_id,
                title=str(e.metadata.get("path", e.entry_id)),
                metadata=dict(e.metadata),
                kb_id=self.kb_id,
            )
            for e in self._entries.values()
        ]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class _LegacyUserKB(KnowledgeBase):
    def __init__(self, reader: Callable[[str, str], Any]):
        self.kb_id = "user_kb"
        self.kb_type = "callback"
        self.scope = Scope.USER
        self._reader = reader

    def read(self, uid: str, doc_id: str) -> Any:
        return self._reader(uid, doc_id)

    async def search(self, query: str, *, top_k: int = 5, filters: dict[str, Any] | None = None) -> list[KBEntry]:
        uid = (filters or {}).get("user_id", "")
        entry_id = (filters or {}).get("entry_id")
        if not entry_id:
            return []
        data = self._reader(uid, entry_id)
        if data is None:
            return []
        return [KBEntry(entry_id=entry_id, content=data, kb_id=self.kb_id)]

    async def get(self, entry_id: str) -> KBEntry | None:
        if ":" not in entry_id:
            return None
        uid, doc_id = entry_id.split(":", 1)
        if not uid or not doc_id:
            return None
        data = self._reader(uid, doc_id)
        if data is None:
            return None
        return KBEntry(
            entry_id=doc_id,
            content=data,
            kb_id=self.kb_id,
            metadata={"user_id": uid},
        )

    async def list_entries(self, *, filters: dict[str, Any] | None = None) -> list[KBEntrySummary]:
        return [KBEntrySummary(entry_id="*", title="user kb", kb_id=self.kb_id)]


class _LegacyGlobalKB(KnowledgeBase):
    def __init__(self, reader: Callable[[str], Any]):
        self.kb_id = "global_kb"
        self.kb_type = "callback"
        self.scope = Scope.GLOBAL
        self._reader = reader

    def read(self, path: str) -> Any:
        return self._reader(path)

    async def search(self, query: str, *, top_k: int = 5, filters: dict[str, Any] | None = None) -> list[KBEntry]:
        path = (filters or {}).get("entry_id", query)
        data = self._reader(path)
        if data is None:
            return []
        return [KBEntry(entry_id=path, content=data, kb_id=self.kb_id)]

    async def get(self, entry_id: str) -> KBEntry | None:
        data = self._reader(entry_id)
        if data is None:
            return None
        return KBEntry(entry_id=entry_id, content=data, kb_id=self.kb_id)

    async def list_entries(self, *, filters: dict[str, Any] | None = None) -> list[KBEntrySummary]:
        return [KBEntrySummary(entry_id="platform", title="平台知识", kb_id=self.kb_id)]


class KBRegistry:
    def __init__(self) -> None:
        self._bases: dict[str, KnowledgeBase] = {}
        self._lock = threading.Lock()
        self._user_reader: Callable[[str, str], Any] | None = None
        self._global_reader: Callable[[str], Any] | None = None

    def register(self, kb: KnowledgeBase) -> None:
        with self._lock:
            self._bases[kb.kb_id] = kb

    def register_callbacks(
        self,
        *,
        user_kb_reader: Callable[[str, str], Any] | None = None,
        global_kb_reader: Callable[[str], Any] | None = None,
    ) -> None:
        self._user_reader = user_kb_reader
        self._global_reader = global_kb_reader
        if user_kb_reader:
            self.register(_LegacyUserKB(user_kb_reader))
        if global_kb_reader:
            self.register(_LegacyGlobalKB(global_kb_reader))

    def list_for_agent(self, agent_id: str, *, kb_read: list[str] | None = None) -> list[KnowledgeBase]:
        with self._lock:
            bases = list(self._bases.values())
        if kb_read is not None:
            allowed = set(kb_read)
            bases = [b for b in bases if b.kb_id in allowed]
        return bases

    def list_ref_entries(self, agent_id: str, *, kb_read: list[str] | None = None) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        for kb in self.list_for_agent(agent_id, kb_read=kb_read):
            summaries = _run_async(kb.list_entries())
            for s in summaries:
                if kb.scope == Scope.USER:
                    ref_id = f"user:{{uid}}:kb:{s.entry_id}"
                    kind = "user_kb"
                elif kb.scope == Scope.GLOBAL:
                    ref_id = f"global:knowledge:{s.entry_id}"
                    kind = "global_knowledge"
                else:
                    ref_id = f"kb:{kb.kb_id}:{s.entry_id}"
                    kind = f"kb_{kb.kb_id}"
                refs.append({"ref_id": ref_id, "kind": kind, "scope": kb.scope, "kb_id": kb.kb_id})
        return refs

    def read_user(self, agent_id: str, uid: str, doc_id: str, *, kb_read: list[str] | None = None) -> Any:
        self._check_kb_access(agent_id, "user_kb", kb_read)
        if self._user_reader:
            return self._user_reader(uid, doc_id)
        kb = self._bases.get("user_kb")
        if kb and isinstance(kb, _LegacyUserKB):
            return kb.read(uid, doc_id)
        raise KeyError(f"user kb entry not found: {doc_id}")

    def read_global(self, agent_id: str, path: str, *, kb_read: list[str] | None = None) -> Any:
        self._check_kb_access(agent_id, "global_kb", kb_read)
        if self._global_reader:
            return self._global_reader(path)
        kb = self._bases.get("global_kb")
        if kb and isinstance(kb, _LegacyGlobalKB):
            return kb.read(path)
        raise KeyError(f"global kb entry not found: {path}")

    @staticmethod
    def _check_kb_access(agent_id: str, kb_id: str, kb_read: list[str] | None) -> None:
        if kb_read is not None and kb_id not in kb_read:
            raise PermissionError(f"KB access denied for agent {agent_id}: {kb_id}")


_default_registry: KBRegistry | None = None
_registry_lock = threading.Lock()


def get_kb_registry() -> KBRegistry:
    global _default_registry
    if _default_registry is None:
        with _registry_lock:
            if _default_registry is None:
                _default_registry = KBRegistry()
    return _default_registry


def reset_kb_registry() -> None:
    global _default_registry
    with _registry_lock:
        _default_registry = KBRegistry()
