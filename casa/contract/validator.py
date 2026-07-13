"""Contract 校验与物化。"""
from __future__ import annotations

import abc
import logging
from typing import Any

from .models import Contract, ContractValidationError, DeliverableType

logger = logging.getLogger("casa.contract")

# ============================================================================
# 校验器接口（ContractValidator）— 领域项目实现此接口
# ============================================================================


class ContractValidator(abc.ABC):
    """领域校验器接口。"""

    @abc.abstractmethod
    def validate(self, contract: Contract) -> list[str]:
        """
        校验 Contract 并返回错误列表。

        返回:
            空列表表示通过；非空表示校验失败，每条是一个错误描述
        """
        ...


# ============================================================================
# 内置：Schema 校验器基类
# ============================================================================


class SchemaContractValidator(ContractValidator):
    """
    基于 schema 定义的 Contract 校验器。
    自动校验必需字段是否存在、枚举值是否合法。
    """

    def validate(self, contract: Contract) -> list[str]:
        errors: list[str] = []

        # 校验 required 字段
        rtype = type(contract.required)
        for f in getattr(rtype, "fields", lambda: [])():
            val = getattr(contract.required, f.name, None)
            if f.required and val is None:
                errors.append(f"required.{f.name} 是必需的")

        # 校验 deliverable 类型
        valid_types = {e.value for e in DeliverableType}
        if contract.deliverable.type not in valid_types:
            errors.append(f"deliverable.type 须为 {sorted(valid_types)}之一")

        return errors


# ============================================================================
# Contract 物化（ContractMaterializer）— 将 Contract 写入 ArtifactStore
# ============================================================================


class ContractMaterializer:
    """
    将 Contract 物化为 Worker 可读的 analysis_context artifact。

    这是 CASA 的关键关卡：Worker 不读用户原话，只读此物化副本。
    """

    @staticmethod
    def materialize(contract: Contract, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        物化 Contract 为 analysis_context。

        参数:
            contract: 已校验的 Contract
            extra: 额外运行时数据（如 raw data、meta 等）

        返回:
            analysis_context dict，写入 artifact store
        """
        ctx: dict[str, Any] = {
            "version": 1,
            "contract_id": contract.contract_id,
            "session_id": contract.session_id,
            "user_id": contract.user_id,
            "tenant_id": contract.tenant_id,
            "deliverable_type": contract.deliverable.type,
            "required": contract.required.to_dict(),
            "context": contract.context.to_dict(),
            "preferences": contract.preferences.to_dict(),
        }
        if contract.intent_summary:
            ctx["intent_summary"] = contract.intent_summary
        if extra:
            ctx["extra"] = extra
        return ctx
