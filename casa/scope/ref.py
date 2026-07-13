"""RefID 与 parse_ref。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Scope, RefPattern

logger = logging.getLogger("casa.scope")

# ============================================================================
# RefID 正则匹配（RefID）
# ============================================================================

_JOB_ARTIFACT_RE = re.compile(r"^job:(?P<job>[^:]+):artifact:(?P<kind>.+)$")
_SESSION_DOC_RE = re.compile(r"^session:(?P<sid>[^:]+):doc:(?P<doc>.+)$")
_SESSION_INTAKE_RE = re.compile(r"^session:(?P<sid>[^:]+):intake:(?P<field>.+)$")
_USER_KB_RE = re.compile(r"^user:(?P<uid>[^:]+):kb:(?P<doc>.+)$")
_GLOBAL_KB_RE = re.compile(r"^global:knowledge:(?P<path>.+)$")
_PLAN_ARTIFACT_RE = re.compile(r"^plan:(?P<plan>[^:]+):artifact:(?P<kind>.+)$")

# 冒号不可出现在 ref_id 的单个段内——覆盖 ASCII 及 Unicode 混淆冒号
_CONFUSABLE_COLON_CHARS = frozenset(":：﹕∶˸꞉։׃܃܄࠰᠄")
_HAS_CONFUSABLE_COLON_RE = re.compile(f"[{''.join(_CONFUSABLE_COLON_CHARS)}]")


# ============================================================================
# 解析后的 ref_id（ParsedRef）
# ============================================================================


@dataclass(kw_only=True)
class ParsedRef:
    """解析后的 ref_id 结构体。"""

    ref_id: str
    scope: str
    kind: str
    job_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    plan_id: str | None = None
    field: str | None = None
    artifact_kind: str | None = None

    def __repr__(self) -> str:
        return f"<ParsedRef scope={self.scope} kind={self.kind} id={self.ref_id}>"


# ============================================================================
# 不可变的强类型 ref_id（RefID）
# ============================================================================


class RefID:
    """
    不可变的强类型 ref_id。

    使用方式：
        ref = RefID.job_artifact("j001", "theme_analytics")
        str(ref)  # "job:j001:artifact:theme_analytics"
        ref.scope  # "job"

    不鼓励直接拼字符串，用 RefID 工厂方法保证命名一致性。
    """

    __slots__ = ("_value", "_parsed")

    def __init__(self, ref_id: str):
        self._value = ref_id
        self._parsed = parse_ref(ref_id)

    @staticmethod
    def _check_no_colon(val: str, label: str) -> None:
        """拒绝含冒号（含 ASCII 及 Unicode 混淆冒号）的值。"""
        if _HAS_CONFUSABLE_COLON_RE.search(val):
            raise ValueError(f"{label} 不能包含冒号: {val!r}")

    @classmethod
    def job_artifact(cls, job_id: str, kind: str) -> RefID:
        cls._check_no_colon(job_id, "job_id")
        cls._check_no_colon(kind, "kind")
        return cls(f"job:{job_id}:artifact:{kind}")

    @classmethod
    def session_doc(cls, session_id: str, doc: str = "brief") -> RefID:
        cls._check_no_colon(session_id, "session_id")
        cls._check_no_colon(doc, "doc")
        return cls(f"session:{session_id}:doc:{doc}")

    @classmethod
    def session_intake(cls, session_id: str, field: str) -> RefID:
        cls._check_no_colon(session_id, "session_id")
        cls._check_no_colon(field, "field")
        return cls(f"session:{session_id}:intake:{field}")

    @classmethod
    def user_kb(cls, user_id: str, doc_id: str) -> RefID:
        cls._check_no_colon(user_id, "user_id")
        cls._check_no_colon(doc_id, "doc_id")
        return cls(f"user:{user_id}:kb:{doc_id}")

    @classmethod
    def global_kb(cls, path: str) -> RefID:
        cls._check_no_colon(path, "path")
        return cls(f"global:knowledge:{path}")

    @classmethod
    def plan_artifact(cls, plan_id: str, kind: str) -> RefID:
        cls._check_no_colon(plan_id, "plan_id")
        cls._check_no_colon(kind, "kind")
        return cls(f"plan:{plan_id}:artifact:{kind}")

    @property
    def value(self) -> str:
        return self._value

    @property
    def parsed(self) -> ParsedRef | None:
        return self._parsed

    @property
    def scope(self) -> str:
        return self._parsed.scope if self._parsed else ""

    @property
    def artifact_kind(self) -> str:
        if self._parsed:
            return self._parsed.artifact_kind or ""
        return ""

    @property
    def is_valid(self) -> bool:
        return self._parsed is not None

    @property
    def is_bare_key(self) -> bool:
        """裸 key（如 "theme_analytics"）视为非法。"""
        return self._parsed is None and ":" not in self._value

    def with_job_id(self, job_id: str) -> RefID:
        """替换 ref_id 中的占位符 {job_id} 为实际 job_id。"""
        self._check_no_colon(job_id, "job_id")
        return RefID(self._value.replace("{job_id}", job_id))

    def __str__(self) -> str:
        return self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RefID):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return False

    def __repr__(self) -> str:
        return f"RefID({self._value!r})"


# ============================================================================
# ref_id 解析
# ============================================================================


def parse_ref(ref_id: str) -> ParsedRef | None:
    """解析 ref_id 字符串为 ParsedRef。裸 key 返回 None。"""
    ref_id = (ref_id or "").strip()
    if not ref_id:
        return None

    m = _JOB_ARTIFACT_RE.match(ref_id)
    if m:
        return ParsedRef(
            ref_id=ref_id, scope=Scope.JOB, kind="artifact",
            job_id=m.group("job"), artifact_kind=m.group("kind"),
        )
    m = _SESSION_DOC_RE.match(ref_id)
    if m:
        return ParsedRef(
            ref_id=ref_id, scope=Scope.SESSION, kind="doc",
            session_id=m.group("sid"), field=m.group("doc"),
        )
    m = _SESSION_INTAKE_RE.match(ref_id)
    if m:
        return ParsedRef(
            ref_id=ref_id, scope=Scope.SESSION, kind="intake",
            session_id=m.group("sid"), field=m.group("field"),
        )
    m = _USER_KB_RE.match(ref_id)
    if m:
        return ParsedRef(
            ref_id=ref_id, scope=Scope.USER, kind="kb",
            user_id=m.group("uid"), field=m.group("doc"),
        )
    m = _GLOBAL_KB_RE.match(ref_id)
    if m:
        return ParsedRef(
            ref_id=ref_id, scope=Scope.GLOBAL, kind="knowledge",
            field=m.group("path"),
        )
    m = _PLAN_ARTIFACT_RE.match(ref_id)
    if m:
        return ParsedRef(
            ref_id=ref_id, scope=Scope.PLAN, kind="artifact",
            plan_id=m.group("plan"), artifact_kind=m.group("kind"),
        )
    return None

