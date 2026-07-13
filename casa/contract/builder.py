"""Contract 构建与 Gate。"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..observability import bind_run_context, log_event, record_metric
from .models import (
    BaseContext, BaseDeliverable, BasePreferences, BaseRequired,
    Contract, ContractField, ContractRevision, ContractValidationError,
    ContractVersionError, DeliverableType, FieldChange, ValidatorRegistry,
)

logger = logging.getLogger("casa.contract")

# ============================================================================
# 采集 → 校验 → 提交的唯一入口（ContractGate）
# ============================================================================


@dataclass(kw_only=True)
class RunRequest:
    """Contract Gate 校验通过后产出的 Run 提交请求。"""

    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex}")
    session_id: str
    user_id: str
    tenant_id: str = ""
    contract: Contract
    intent: str = ""
    idempotency_key: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "intent": self.intent or self.contract.deliverable.type,
            "contract": self.contract.to_dict(),
            "idempotency_key": self.idempotency_key,
        }


@dataclass(kw_only=True)
class ContractFieldValue:
    """一次 Contract 字段采集结果。"""

    field_name: str
    value: Any
    confidence: str = "confirmed"
    source: str = ""


class ContractBuilder:
    """
    辅助构建 Contract——从对话中逐字段采集值。
    """

    def __init__(
        self,
        *,
        required_class: type[BaseRequired],
        deliverable: BaseDeliverable | None = None,
    ):
        self._required_class = required_class
        self._deliverable = deliverable or BaseDeliverable()
        self._fields: dict[str, Any] = {}
        self._field_meta: dict[str, ContractFieldValue] = {}

    def offer(
        self,
        field_name: str,
        value: Any,
        *,
        confidence: str = "confirmed",
        source: str = "",
    ) -> None:
        self._fields[field_name] = value
        self._field_meta[field_name] = ContractFieldValue(
            field_name=field_name,
            value=value,
            confidence=confidence,
            source=source,
        )

    def missing_fields(self) -> list[ContractField]:
        declared = {f.name for f in self._required_class.fields() if f.required}
        collected = set(self._fields.keys())
        return [f for f in self._required_class.fields() if f.name in (declared - collected)]

    def is_complete(self) -> bool:
        return len(self.missing_fields()) == 0

    def build(
        self,
        *,
        session_id: str,
        user_id: str,
        tenant_id: str = "",
        intent_summary: str = "",
    ) -> Contract:
        return Contract(
            deliverable=self._deliverable,
            required=self._required_class(**self._fields),
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            intent_summary=intent_summary,
        )


class ContractGate:
    """
    Contract Gate：采集 → validate → 提交 Run 的唯一入口。

    使用方式：
        gate = ContractGate(validator=MyValidator(), min_contract_version=1)
        run_req = gate.submit(contract)
    """

    def __init__(
        self,
        *,
        validator: ContractValidator | None = None,
        min_contract_version: int = 1,
    ):
        self.validator = validator
        self.min_version = min_contract_version

    def submit(
        self,
        contract: Contract,
        *,
        idempotency_key: str = "",
    ) -> RunRequest:
        """
        校验 Contract 并产生 RunRequest。
        """
        if contract.version < self.min_version:
            raise ContractVersionError(
                f"Contract 版本 {contract.version} 不受支持。最低版本: {self.min_version}。"
                f"请使用 register_contract_migrator() 升级。"
            )
        # 1. 通用校验
        contract.validate()

        # 2. 领域校验（如 schema、业务规则）
        if self.validator:
            errs = self.validator.validate(contract)
            if errs:
                raise ContractValidationError(errs)

        # 3. 更新时间戳
        contract.updated_at = datetime.now(timezone.utc).isoformat()

        # 4. 产出 RunRequest
        run_req = RunRequest(
            session_id=contract.session_id,
            user_id=contract.user_id,
            tenant_id=contract.tenant_id,
            contract=contract,
            intent=contract.deliverable.type,
            idempotency_key=idempotency_key,
        )
        bind_run_context(
            run_id=run_req.run_id,
            session_id=run_req.session_id,
            user_id=run_req.user_id,
            tenant_id=contract.tenant_id,
        )
        record_metric("contract.submit", 1.0, run_id=run_req.run_id)
        log_event(
            "contract.submit",
            contract_id=contract.contract_id,
            deliverable_type=contract.deliverable.type,
        )
        return run_req


