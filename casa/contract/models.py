"""Contract 数据模型。"""
from __future__ import annotations

import abc
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, TypeVar, Callable

from ..config import get_config
from ..observability import bind_run_context, log_event, record_metric

logger = logging.getLogger("casa.contract")

# ============================================================================
# 通用基类
# ============================================================================


class ContractValidationError(ValueError):
    """Contract 校验失败时抛出。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class ContractVersionError(ValueError):
    """Contract 版本不兼容时抛出。"""

    def __init__(self, message: str):
        super().__init__(message)


CURRENT_CONTRACT_VERSION = 1

# 领域项目注册 (from_version, to_version) -> migrator
ContractMigratorFn = Any  # Callable[[dict[str, Any]], dict[str, Any]]
_contract_migrators: dict[tuple[int, int], ContractMigratorFn] = {}
_contract_migrators_lock = threading.Lock()


def register_contract_migrator(
    from_version: int,
    to_version: int,
    migrator: ContractMigratorFn,
) -> None:
    """注册 Contract 版本迁移器（如 v1→v2）。"""
    with _contract_migrators_lock:
        _contract_migrators[(from_version, to_version)] = migrator


# ============================================================================
# 交付物类型（领域可扩展）
# ============================================================================
class DeliverableType(str, Enum):
    """交付物类型枚举——领域项目可继承扩展。"""

    FULL = "full"
    PARTIAL = "partial"
    INSIGHTS = "insights"
    PATCH = "patch"

    @classmethod
    def labels(cls) -> dict[str, str]:
        return {
            cls.FULL.value: "完整交付",
            cls.PARTIAL.value: "部分交付",
            cls.INSIGHTS.value: "洞察摘要",
            cls.PATCH.value: "局部更新",
        }


# ============================================================================
# 领域项目继承此基类（ContractSchema）
# ============================================================================
D = TypeVar("D", bound="BaseDeliverable")  # noqa: D 用作 deliverable 的 TypeVar
R = TypeVar("R", bound="BaseRequired")      # noqa: R 用作 required 的 TypeVar


@dataclass(kw_only=True)
class ContractField:
    """声明一个 Contract 字段的元信息。"""

    name: str
    description: str
    required: bool = True
    default: Any = None
    validator: str | None = None  # 引用 ValidatorRegistry 中的 key
    depends_on: str = ""  # 例如 deliverable.type == 'full'
    i18n_key: str = ""


# ============================================================================
# 校验器注册表
# ============================================================================

ValidatorFn = Callable[[Any], list[str]]


class ValidatorRegistry:
    """Contract schema 的命名字段校验器。"""

    _validators: dict[str, ValidatorFn] = {}
    _lock = threading.Lock()

    @classmethod
    def register(cls, key: str, fn: ValidatorFn) -> None:
        with cls._lock:
            cls._validators[key] = fn

    @classmethod
    def validate(cls, key: str, value: Any) -> list[str]:
        with cls._lock:
            fn = cls._validators.get(key)
        if fn is None:
            return [f"未知 validator: {key}"]
        return fn(value)

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._validators.clear()


def _eval_depends_on(expr: str, contract: Contract) -> bool:
    """简单 depends_on 表达式，如 'deliverable.type == \"full\"'。"""
    if not expr:
        return True
    expr = expr.strip()
    if "==" in expr:
        left, right = expr.split("==", 1)
        left = left.strip()
        right = right.strip().strip("'\"")
        parts = left.split(".")
        obj: Any = contract
        for part in parts:
            obj = getattr(obj, part, None)
        return str(obj) == right
    return True


@dataclass(kw_only=True)
class FieldChange:
    field_path: str
    old_value: Any
    new_value: Any


@dataclass(kw_only=True)
class ContractRevision:
    revision_id: str
    timestamp: str
    changed_by: str
    changes: list[FieldChange] = field(default_factory=list)


# 需要前向引用 — Contract 定义在下方
@dataclass(kw_only=True)
class BaseDeliverable:
    """交付物规格基类——领域项目继承并添加字段。"""

    type: str = DeliverableType.FULL.value
    format: str = "json"

    def to_dict(self) -> dict[str, Any]:
        d = {"type": self.type}
        if self.format != "json":
            d["format"] = self.format
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaseDeliverable:
        return cls(type=data.get("type", DeliverableType.FULL.value), format=data.get("format", "json"))

    @classmethod
    def fields(cls) -> list[ContractField]:
        return [ContractField(name="type", description="交付物类型", required=True)]


@dataclass(kw_only=True)
class BaseRequired:
    """任务必需参数基类——领域项目继承并添加字段。"""

    mode: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaseRequired:
        return cls(mode=data.get("mode", "default"))

    @classmethod
    def fields(cls) -> list[ContractField]:
        return [ContractField(name="mode", description="运行模式", required=True)]


@dataclass(kw_only=True)
class BaseContext:
    """任务上下文基类——领域项目继承并添加字段。"""

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaseContext:
        return cls()

    @classmethod
    def fields(cls) -> list[ContractField]:
        return []


@dataclass(kw_only=True)
class BasePreferences:
    """用户偏好基类——领域项目继承并添加字段。"""

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BasePreferences:
        return cls()

    @classmethod
    def fields(cls) -> list[ContractField]:
        return []


# ============================================================================
# 顶层容器（Contract）
# ============================================================================


@dataclass(kw_only=True)
class Contract:
    """
    CASA 契约：任务说明书。

    对话 Agent 构建 Contract → Orchestrator 只读 Contract 编译 Plan → Worker 只读物化副本。

    使用方式：
        # 1. 领域项目继承 Contract 各子结构
        @dataclass(kw_only=True)
        class MyRequired(BaseRequired):
            items: list[str] = field(default_factory=list)

        # 2. 构建 Contract
        c = Contract(
            deliverable=BaseDeliverable(type="full"),
            required=MyRequired(mode="analyze", items=["a", "b"]),
            session_id="s_001",
            user_id="u_001",
        )

        # 3. 校验
        c.validate()  # 失败抛 ContractValidationError

        # 4. 提交 Run
        gate = ContractGate(validator=MyValidator())
        gate.submit(c)  # 返回 RunRequest
    """

    # 核心字段
    deliverable: BaseDeliverable
    required: BaseRequired
    context: BaseContext = field(default_factory=BaseContext)
    preferences: BasePreferences = field(default_factory=BasePreferences)

    # 身份与版本
    contract_id: str = field(default_factory=lambda: f"ctr_{uuid.uuid4().hex}")
    version: int = CURRENT_CONTRACT_VERSION
    session_id: str = ""
    user_id: str = ""
    tenant_id: str = ""

    # 元信息
    intent_summary: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    revisions: list[ContractRevision] = field(default_factory=list)

    def revise(self, changed_by: str, **updates: Any) -> ContractRevision:
        changes: list[FieldChange] = []
        for key, new_val in updates.items():
            if hasattr(self, key):
                old_val = getattr(self, key)
                if old_val != new_val:
                    changes.append(FieldChange(field_path=key, old_value=old_val, new_value=new_val))
                    setattr(self, key, new_val)
        rev = ContractRevision(
            revision_id=f"rev_{uuid.uuid4().hex}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            changed_by=changed_by,
            changes=changes,
        )
        self.revisions.append(rev)
        self.updated_at = rev.timestamp
        return rev

    def diff(self, other: Contract) -> list[FieldChange]:
        changes: list[FieldChange] = []
        for key in ("session_id", "user_id", "tenant_id", "intent_summary"):
            a, b = getattr(self, key), getattr(other, key)
            if a != b:
                changes.append(FieldChange(field_path=key, old_value=a, new_value=b))
        if self.deliverable.type != other.deliverable.type:
            changes.append(FieldChange(
                field_path="deliverable.type",
                old_value=self.deliverable.type,
                new_value=other.deliverable.type,
            ))
        return changes

    def to_dict(self) -> dict[str, Any]:
        """序列化为 API 传输格式（含完整身份字段）。"""
        d: dict[str, Any] = {
            "contract_id": self.contract_id,
            "version": self.version,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deliverable": self.deliverable.to_dict(),
            "required": self.required.to_dict(),
            "context": self.context.to_dict(),
            "preferences": self.preferences.to_dict(),
        }
        if self.intent_summary:
            d["intent_summary"] = self.intent_summary
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, validate: bool = True) -> Contract:
        """按 version 路由反序列化；缺 version 按 v1 处理。"""
        migrated = cls._migrate_to_current(dict(data))
        contract = cls._from_dict_v1(migrated)
        if validate:
            contract.validate()
        return contract

    @classmethod
    def _deserialize_deliverable(cls, data: dict[str, Any]) -> BaseDeliverable:
        return BaseDeliverable.from_dict(data)

    @classmethod
    def _deserialize_required(cls, data: dict[str, Any]) -> BaseRequired:
        return BaseRequired.from_dict(data)

    @classmethod
    def _deserialize_context(cls, data: dict[str, Any]) -> BaseContext:
        return BaseContext.from_dict(data)

    @classmethod
    def _deserialize_preferences(cls, data: dict[str, Any]) -> BasePreferences:
        return BasePreferences.from_dict(data)

    @classmethod
    def _migrate_to_current(cls, data: dict[str, Any]) -> dict[str, Any]:
        version = int(data.get("version", 1))
        if version > CURRENT_CONTRACT_VERSION:
            raise ContractVersionError(
                f"Contract v{version} 高于框架支持版本 v{CURRENT_CONTRACT_VERSION}"
            )
        while version < CURRENT_CONTRACT_VERSION:
            key = (version, version + 1)
            with _contract_migrators_lock:
                migrator = _contract_migrators.get(key)
            if migrator is None:
                raise ContractVersionError(
                    f"Contract v{version} 无法迁移到 v{CURRENT_CONTRACT_VERSION}："
                    f"缺少 migrator {key}"
                )
            data = migrator(data)
            version = int(data.get("version", version + 1))
        return data

    @classmethod
    def _from_dict_v1(cls, data: dict[str, Any]) -> Contract:
        deliverable_data = data.get("deliverable") or {}
        required_data = data.get("required") or {}
        context_data = data.get("context") or {}
        preferences_data = data.get("preferences") or {}
        return cls(
            deliverable=cls._deserialize_deliverable(deliverable_data),
            required=cls._deserialize_required(required_data),
            context=cls._deserialize_context(context_data),
            preferences=cls._deserialize_preferences(preferences_data),
            contract_id=data.get("contract_id", f"ctr_{uuid.uuid4().hex}"),
            version=int(data.get("version", 1)),
            session_id=data.get("session_id", ""),
            user_id=data.get("user_id", ""),
            tenant_id=data.get("tenant_id", ""),
            intent_summary=data.get("intent_summary", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )

    def validate(self) -> None:
        """校验 Contract 完整性。子类应扩展此方法添加领域规则。

        安全关键校验（session_id, user_id, version）始终执行，
        contract_validation_enabled 仅控制领域层校验。
        """
        errors: list[str] = []

        if self.version < 1:
            errors.append("version 必须 >= 1")

        if not self.session_id:
            errors.append("session_id 缺失")
        if not self.user_id:
            errors.append("user_id 缺失")

        config = get_config()
        if config.contract_validation_enabled:
            dtype = self.deliverable.type
            if dtype not in {e.value for e in DeliverableType}:
                errors.append(f"未知交付物类型: {dtype}")

            # 委托给领域 validator（若有）
            self._validate_domain(errors)

        if errors:
            raise ContractValidationError(errors)

    def _validate_domain(self, errors: list[str]) -> None:
        """子类重写以添加领域校验逻辑。"""
        pass

    # =========================================================================
    # 供 UI 自动生成表单的 Schema 自省
    # =========================================================================
    @classmethod
    def schema(cls, locale: str = "", i18n: ContractI18nProvider | None = None) -> dict[str, Any]:
        """返回基类 Contract 的字段 schema（供 UI 自动生成表单）。"""
        def label(f: ContractField) -> str:
            if i18n and f.i18n_key:
                return i18n.translate(f.i18n_key, locale) or f.description
            return f.description

        deliverable_fields = [field_to_dict(f) for f in BaseDeliverable.fields()]
        if i18n:
            for d in deliverable_fields:
                cf = next((f for f in BaseDeliverable.fields() if f.name == d["name"]), None)
                if cf:
                    d["description"] = label(cf)
        return {
            "deliverable": {"fields": deliverable_fields},
            "required": {"fields": [field_to_dict(f) for f in BaseRequired.fields()]},
            "context": {"fields": [field_to_dict(f) for f in BaseContext.fields()]},
            "preferences": {"fields": [field_to_dict(f) for f in BasePreferences.fields()]},
            "locale": locale,
        }


class ContractI18nProvider(abc.ABC):
    """Contract schema 字段的可选 i18n 标签。"""

    @abc.abstractmethod
    def translate(self, i18n_key: str, locale: str) -> str:
        ...


class DictContractI18nProvider(ContractI18nProvider):
    def __init__(self, catalog: dict[str, dict[str, str]]):
        self._catalog = catalog

    def translate(self, i18n_key: str, locale: str) -> str:
        return self._catalog.get(i18n_key, {}).get(locale, "")


def field_to_dict(f: ContractField) -> dict[str, Any]:
    return {
        "name": f.name,
        "description": f.description,
        "required": f.required,
        "default": f.default,
    }

