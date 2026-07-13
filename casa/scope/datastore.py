"""DataStore facade。"""
from __future__ import annotations

import logging
from typing import Any

from ..config import Scope
from .ref import ParsedRef, RefID, parse_ref

logger = logging.getLogger("casa.scope")

# ============================================================================
# 统一读写 + 归属校验 facade（DataStore）
# ============================================================================


class DataStoreAccessError(Exception):
    """数据访问被拒绝。"""

    def __init__(self, ref_id: str, reason: str):
        self.ref_id = ref_id
        self.reason = reason
        super().__init__(f"DataStore 拒绝访问 {ref_id!r}: {reason}")


class DataStore:
    """
    统一数据访问 facade：解析 ref_id → 读写校验 → 路由到对应后端。

    使用方式：
        ds = DataStore(
            agent_id="my_worker",
            job_id="j001",
            session_id="s001",
            user_id="u001",
            artifact_store=store,
        )
        data = ds.resolve_read(RefID.job_artifact("j001", "theme_analytics"))
        ds.resolve_write(ref, {"key": "value"})
    """

    def __init__(
        self,
        *,
        agent_id: str = "",
        job_id: str = "",
        session_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        artifact_store: Any | None = None,
        data_grants_read: list[str] | None = None,
        data_grants_write: str | None = None,
        # 领域项目注入的后端读取函数
        session_reader: Any | None = None,  # Callable[[str], dict]
        user_kb_reader: Any | None = None,  # 已弃用：请用 KBRegistry.register() + kb_read
        global_kb_reader: Any | None = None,  # 已弃用：请用 KBRegistry.register() + kb_read
        kb_registry: Any | None = None,
        kb_read: list[str] | None = None,
        # 严格模式：未设置 ID 时，拒绝带该 ID 的 ref 访问
        strict: bool = False,
    ):
        self.agent_id = agent_id
        self.job_id = job_id
        self.session_id = session_id
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.artifact_store = artifact_store
        self._read_grants: set[str] | None = (
            set(data_grants_read) if data_grants_read is not None else None
        )
        self._write_grant = data_grants_write
        self._strict = strict

        # 领域项目注入
        self._session_reader = session_reader
        self._user_kb_reader = user_kb_reader
        self._global_kb_reader = global_kb_reader
        from ..knowledge import KBRegistry, get_kb_registry
        self._kb_registry: KBRegistry = kb_registry or get_kb_registry()
        self._kb_read: list[str] | None = None if kb_read is None else list(kb_read)
        if user_kb_reader or global_kb_reader:
            self._kb_registry.register_callbacks(
                user_kb_reader=user_kb_reader,
                global_kb_reader=global_kb_reader,
            )

    # --- 读 ---
    def resolve_read(self, ref: RefID | str) -> Any:
        """解析 ref_id 并读取数据，带归属校验。"""
        if isinstance(ref, str):
            ref = RefID(ref)

        if not ref.is_valid:
            if ref.is_bare_key:
                raise DataStoreAccessError(str(ref), "禁止裸 key，请使用完整 ref_id")
            raise DataStoreAccessError(str(ref), "ref_id 格式无效")

        parsed = ref.parsed

        if parsed.scope == Scope.JOB:
            return self._read_job(ref, parsed)
        if parsed.scope == Scope.SESSION:
            return self._read_session(ref, parsed)
        if parsed.scope == Scope.USER:
            return self._read_user(ref, parsed)
        if parsed.scope == Scope.GLOBAL:
            return self._read_global(ref, parsed)
        if parsed.scope == Scope.PLAN:
            self._check_plan_match(parsed)
            return self._read_job(ref, parsed)  # PLAN artifact 复用 job 读写路径

        raise DataStoreAccessError(str(ref), f"未知 scope: {parsed.scope}")

    def _check_job_match(self, parsed: ParsedRef) -> None:
        """校验 job_id 归属。artifact_store 注入时以其 job_id 为权威。"""
        if self._strict and not self.job_id and parsed.job_id:
            raise DataStoreAccessError(parsed.ref_id, "job_id 未设置（严格模式）")
        expected_job = self.job_id or ""
        if self.artifact_store is not None:
            store_job = getattr(self.artifact_store, "job_id", "")
            if store_job:
                if expected_job and expected_job != store_job:
                    raise DataStoreAccessError(
                        parsed.ref_id,
                        f"DataStore job_id 与 artifact_store 不一致: {expected_job} vs {store_job}",
                    )
                expected_job = store_job
        if parsed.job_id:
            if not expected_job:
                raise DataStoreAccessError(parsed.ref_id, "job_id 未设置，拒绝跨 job 访问")
            if parsed.job_id != expected_job:
                raise DataStoreAccessError(
                    parsed.ref_id, f"job_id 不匹配: {parsed.job_id} vs {expected_job}"
                )
        if self.tenant_id and self.artifact_store is not None:
            store_tenant = getattr(self.artifact_store, "tenant_id", "")
            if store_tenant and store_tenant != self.tenant_id:
                raise DataStoreAccessError(
                    parsed.ref_id,
                    f"tenant_id 不匹配: {store_tenant} vs {self.tenant_id}",
                )

    def _check_plan_match(self, parsed: ParsedRef) -> None:
        """校验 plan_id 归属（PLAN scope）。"""
        if not parsed.plan_id:
            raise DataStoreAccessError(parsed.ref_id, "plan_id 缺失")
        if self.artifact_store is None:
            raise DataStoreAccessError(parsed.ref_id, "artifact_store 未注入")
        store_plan = getattr(self.artifact_store, "_plan_id", None)
        if store_plan and parsed.plan_id != store_plan:
            raise DataStoreAccessError(
                parsed.ref_id, f"plan_id 不匹配: {parsed.plan_id} vs {store_plan}"
            )

    def _check_session_match(self, parsed: ParsedRef) -> None:
        """校验 session_id 归属。"""
        sid = parsed.session_id
        if self._strict and not self.session_id and sid:
            raise DataStoreAccessError(parsed.ref_id, "session_id 未设置（严格模式）")
        if self.session_id and sid and sid != self.session_id:
            raise DataStoreAccessError(parsed.ref_id, "session_id 不匹配")

    def _check_user_match(self, parsed: ParsedRef) -> None:
        """校验 user_id 归属。"""
        uid = parsed.user_id
        if self._strict and not self.user_id and uid:
            raise DataStoreAccessError(parsed.ref_id, "user_id 未设置（严格模式）")
        if self.user_id and uid and uid != self.user_id:
            raise DataStoreAccessError(parsed.ref_id, "user_id 不匹配")

    def _read_job(self, ref: RefID, parsed: ParsedRef) -> Any:
        key = parsed.artifact_kind or ""

        # 数据许可校验（None = 未配置，兼容旧调用；空集合 = 显式拒绝全部读）
        if self._read_grants is not None and key not in self._read_grants:
            raise DataStoreAccessError(str(ref), f"数据许可拒绝: {key}")

        # job_id 归属校验
        self._check_job_match(parsed)

        if not self.artifact_store:
            raise DataStoreAccessError(str(ref), "artifact_store 未注入")

        data = self.artifact_store.read(key)
        if data is None:
            raise KeyError(f"artifact 不存在: {key}")
        return data

    def _read_session(self, ref: RefID, parsed: ParsedRef) -> Any:
        self._check_session_match(parsed)
        sid = parsed.session_id

        if not self._session_reader:
            raise DataStoreAccessError(str(ref), "session_reader 未注入")

        return self._session_reader(sid or self.session_id, parsed)

    def _read_user(self, ref: RefID, parsed: ParsedRef) -> Any:
        self._check_user_match(parsed)
        uid = parsed.user_id
        doc_id = parsed.field or ""
        try:
            return self._kb_registry.read_user(
                self.agent_id, uid or self.user_id, doc_id, kb_read=self._kb_read,
            )
        except (KeyError, PermissionError):
            if not self._user_kb_reader:
                raise DataStoreAccessError(str(ref), "user_kb_reader 未注入") from None
            return self._user_kb_reader(uid or self.user_id, doc_id)

    def _read_global(self, ref: RefID, parsed: ParsedRef) -> Any:
        path = parsed.field or ""
        try:
            return self._kb_registry.read_global(
                self.agent_id, path, kb_read=self._kb_read,
            )
        except (KeyError, PermissionError):
            if not self._global_kb_reader:
                raise DataStoreAccessError(str(ref), "global_kb_reader 未注入") from None
            return self._global_kb_reader(path)

    # --- 写 ---
    def resolve_write(self, ref: RefID | str, data: Any) -> dict[str, Any]:
        """解析 ref_id 并写入数据，带归属校验。"""
        if isinstance(ref, str):
            ref = RefID(ref)

        if not ref.is_valid:
            raise DataStoreAccessError(str(ref), "ref_id 格式无效")

        parsed = ref.parsed

        if parsed.scope == Scope.JOB:
            return self._write_job(ref, parsed, data)
        if parsed.scope == Scope.SESSION:
            return self._write_session(ref, parsed, data)
        if parsed.scope == Scope.PLAN:
            self._check_plan_match(parsed)
            return self._write_job(ref, parsed, data)  # PLAN artifact 复用 job 写入路径

        raise DataStoreAccessError(str(ref), f"不可写的 scope: {parsed.scope}")

    def _write_job(self, ref: RefID, parsed: ParsedRef, data: Any) -> dict[str, Any]:
        key = parsed.artifact_kind or ""

        # 数据许可校验（写）：None = 未配置；"" = 显式拒绝写
        if self._write_grant is not None and key != self._write_grant:
            raise DataStoreAccessError(str(ref), f"数据许可（写）拒绝: {key}")

        # job_id 归属校验
        self._check_job_match(parsed)

        if not self.artifact_store:
            raise DataStoreAccessError(str(ref), "artifact_store 未注入")

        if not isinstance(data, dict):
            raise TypeError("job artifact write 期望 dict 类型")

        self.artifact_store.write(key, data)
        return {"ref_id": str(ref), "written": True, "kind": key}

    def _write_session(self, ref: RefID, parsed: ParsedRef, data: Any) -> dict[str, Any]:
        self._check_session_match(parsed)
        # 领域项目覆写 session_writer 处理具体写入逻辑
        return {"ref_id": str(ref), "written": True}

