"""
CASA 交付物 — 结构化终态输出规格与渲染器。
"""

from __future__ import annotations

import abc
import json
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass(kw_only=True)
class ChapterSpec:
    chapter_id: str
    title: str
    source_artifact: str
    template_block: str = ""
    optional: bool = False


@dataclass(kw_only=True)
class DeliverableSpec:
    deliverable_id: str
    label: str
    format: str = "json"
    template: str = ""
    chapters: list[ChapterSpec] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    renderer: str = "default"


@dataclass(kw_only=True)
class DeliverableOutput:
    content: bytes
    format: str
    metadata: dict[str, Any] = field(default_factory=dict)


class DeliverableRenderer(abc.ABC):
    @abc.abstractmethod
    async def render(self, spec: DeliverableSpec, artifacts: dict[str, dict]) -> DeliverableOutput:
        ...


class DefaultJsonRenderer(DeliverableRenderer):
    async def render(self, spec: DeliverableSpec, artifacts: dict[str, dict]) -> DeliverableOutput:
        payload = {k: artifacts.get(k, {}) for k in spec.sources}
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return DeliverableOutput(content=body, format="json", metadata={"deliverable_id": spec.deliverable_id})


class MarkdownRenderer(DeliverableRenderer):
    """按 ChapterSpec 顺序拼接 artifact 为 Markdown 文档。"""

    async def render(self, spec: DeliverableSpec, artifacts: dict[str, dict]) -> DeliverableOutput:
        parts: list[str] = []
        for ch in spec.chapters:
            raw = artifacts.get(ch.source_artifact, {})
            if isinstance(raw.get("_text_content"), str):
                body = raw["_text_content"]
            else:
                body = str(raw.get("text", raw))
            parts.append(f"# {ch.title}\n\n{body}\n")
        if not parts and spec.sources:
            for src in spec.sources:
                raw = artifacts.get(src, {})
                body = raw.get("_text_content") or str(raw.get("text", raw))
                parts.append(f"## {src}\n\n{body}\n")
        content = "\n".join(parts).encode("utf-8")
        return DeliverableOutput(content=content, format="md", metadata={"deliverable_id": spec.deliverable_id})


class RawFilesRenderer(DeliverableRenderer):
    """将多个 artifact 打包为 zip（代码场景终态交付）。"""

    async def render(self, spec: DeliverableSpec, artifacts: dict[str, dict]) -> DeliverableOutput:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for kind in spec.sources:
                data = artifacts.get(kind, {})
                if isinstance(data.get("_text_content"), str):
                    zf.writestr(f"{kind}.txt", data["_text_content"])
                else:
                    zf.writestr(
                        f"{kind}.json",
                        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                    )
        return DeliverableOutput(
            content=buf.getvalue(),
            format="zip",
            metadata={"deliverable_id": spec.deliverable_id, "file_count": len(spec.sources)},
        )


class DeliverableRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, DeliverableSpec] = {}
        self._renderers: dict[str, DeliverableRenderer] = {
            "default": DefaultJsonRenderer(),
            "markdown": MarkdownRenderer(),
            "raw_files": RawFilesRenderer(),
        }
        self._lock = threading.Lock()

    def register(self, spec: DeliverableSpec, renderer: DeliverableRenderer | None = None) -> None:
        with self._lock:
            self._specs[spec.deliverable_id] = spec
            if renderer:
                self._renderers[spec.renderer] = renderer

    def spec_for(self, deliverable_type: str) -> DeliverableSpec | None:
        with self._lock:
            return self._specs.get(deliverable_type)

    def required_artifacts(self, deliverable_type: str) -> set[str]:
        spec = self.spec_for(deliverable_type)
        if not spec:
            return set()
        required = set(spec.sources)
        required.update(c.source_artifact for c in spec.chapters)
        return required

    async def render(self, deliverable_type: str, artifacts: dict[str, dict]) -> DeliverableOutput | None:
        spec = self.spec_for(deliverable_type)
        if not spec:
            return None
        renderer = self._renderers.get(spec.renderer, DefaultJsonRenderer())
        return await renderer.render(spec, artifacts)


_default_registry: DeliverableRegistry | None = None
_registry_lock = threading.Lock()


def get_deliverable_registry() -> DeliverableRegistry:
    global _default_registry
    if _default_registry is None:
        with _registry_lock:
            if _default_registry is None:
                _default_registry = DeliverableRegistry()
    return _default_registry


def reset_deliverable_registry() -> None:
    global _default_registry
    with _registry_lock:
        _default_registry = DeliverableRegistry()
