"""
CASA 多 Agent 协同架构 — 统一配置与常量

本模块是 CASA 框架所有配置项的唯一真理源。
领域项目通过环境变量覆盖默认值，无需修改代码。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


# ============================================================================
# 包版本
# ============================================================================
from ._version import __version__


# ============================================================================
# ref_id 分层语义常量（Scope）
# ============================================================================
class Scope:
    """ref_id 的四层隔离域"""

    GLOBAL = "global"
    USER = "user"
    SESSION = "session"
    JOB = "job"
    PLAN = "plan"

    ALL = frozenset({GLOBAL, USER, SESSION, JOB, PLAN})


# ============================================================================
# 产物命名规范与存储结构常量（Artifact）
# ============================================================================
class ArtifactKind:
    """产物类别语义标记（领域可扩展）"""

    RAW = "raw"
    PROCESSED = "processed"
    ANALYTICS = "analytics"
    QA = "qa"


class ArtifactDir:
    """artifact 存储目录结构"""

    DEFAULT_BASE = "casa_jobs"
    ARTIFACTS = "artifacts"
    REPORTS = "reports"


# ============================================================================
# 统一寻址正则（RefPattern）
# ============================================================================
class RefPattern:
    """ref_id 标准格式模板"""

    JOB_ARTIFACT = "job:{job_id}:artifact:{kind}"
    SESSION_DOC = "session:{session_id}:doc:{doc_name}"
    SESSION_INTAKE = "session:{session_id}:intake:{field}"
    USER_KB = "user:{user_id}:kb:{doc_id}"
    GLOBAL_KB = "global:knowledge:{path}"
    PLAN_ARTIFACT = "plan:{plan_id}:artifact:{kind}"


# ============================================================================
# Agent 执行配置档（ExecutionProfile）
# ============================================================================
class ExecutionProfile:
    """Agent 的执行模式"""

    DETERMINISTIC = "deterministic"  # 纯代码，无 LLM
    STRUCTURED = "structured"        # 单次 LLM 调用，全量注入输入
    HARNESS = "harness"              # 带工具循环的 LLM Agent
    REPORT_CHAPTER = "report_chapter"  # 报告章节生成

    ALL = frozenset({DETERMINISTIC, STRUCTURED, HARNESS, REPORT_CHAPTER})


# ============================================================================
# Agent 角色分类（AgentRole）
# ============================================================================
class AgentRole:
    """Agent 三角色标签"""

    DIALOGUE = "dialogue"       # 对话采集
    ORCHESTRATOR = "orchestrator"  # 编排编译
    WORKER = "worker"           # 专业执行

    # Worker 子分类（领域可扩展）
    INTEL = "intel"
    PROCESSOR = "processor"
    ANALYZER = "analyzer"
    ASSEMBLER = "assembler"
    QA = "qa"
    SYNTHESIZER = "synthesizer"


# ============================================================================
# 编排常量
# ============================================================================
class Orchestration:
    """编排层默认参数"""

    DEFAULT_MAX_ITERATIONS = 5
    DEFAULT_TEMPERATURE = 0.2
    DEFAULT_MAX_TOKENS = 4000
    DEFAULT_LLM_TIMEOUT_SECONDS = 20.0

    # stage 容错链
    SIMPLE_RETRIES = 2       # 简单重试次数（不含首次）
    FRESH_SESSION_RETRIES = 1

    # 并发
    DEFAULT_MAX_PARALLEL_PER_SESSION = 4


# ============================================================================
# 并发策略
# ============================================================================
class ConcurrencyPolicy:
    """会话级并发策略"""

    REJECT = "reject"    # 超限拒绝
    FIFO = "fifo"        # 超限排队


# ============================================================================
# 存储后端
# ============================================================================
class StorageBackend:
    """artifact 存储后端选择"""

    LOCAL = "local"
    MINIO = "minio"
    S3 = "s3"


# ============================================================================
# 运行时配置（CASAConfig）— 统一管理所有可配置项
# ============================================================================
class ConfigValidationError(ValueError):
    """CASAConfig 校验失败。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _env_float(key: str, default: str) -> float:
    raw = os.getenv(key, default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigValidationError([f"环境变量 {key}={raw!r} 无效: {exc}"]) from exc


def _env_int(key: str, default: str) -> int:
    raw = os.getenv(key, default)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigValidationError([f"环境变量 {key}={raw!r} 无效: {exc}"]) from exc


@dataclass
class LLMProviderConfig:
    """单 provider 的 LLM 凭据（支持多 provider 并存）。"""

    provider: str
    api_key: str = ""
    base_url: str = ""
    default_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "api_key": "***" if self.api_key else "",
            "base_url": self.base_url,
            "default_model": self.default_model,
        }


@dataclass
class CASAConfig:
    """
    CASA 框架的集中配置。
    优先读环境变量，其次用此处默认值。
    领域项目创建 CASAConfig() 实例即可，无需读散落的 os.environ。
    """

    # --- Artifact 存储 ---
    artifact_storage_backend: str = field(
        default_factory=lambda: os.getenv("CASA_ARTIFACT_STORAGE_BACKEND", StorageBackend.LOCAL)
    )
    artifact_base_dir: str = field(
        default_factory=lambda: os.getenv("CASA_ARTIFACT_BASE_DIR", ArtifactDir.DEFAULT_BASE)
    )

    # 多租户（空字符串 = 单租户/向后兼容路径）
    tenant_id: str = field(default_factory=lambda: os.getenv("CASA_TENANT_ID", ""))

    # 配置元数据（文件加载器）
    config_profile: str = "default"
    config_file: str = ""

    # MinIO / S3
    s3_endpoint: str = field(default_factory=lambda: os.getenv("CASA_S3_ENDPOINT", ""))
    s3_access_key: str = field(default_factory=lambda: os.getenv("CASA_S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: os.getenv("CASA_S3_SECRET_KEY", ""))
    s3_bucket: str = field(default_factory=lambda: os.getenv("CASA_S3_BUCKET", "casa-artifacts"))
    s3_region: str = field(default_factory=lambda: os.getenv("CASA_S3_REGION", "us-east-1"))

    # --- LLM ---
    llm_default_model: str = field(default_factory=lambda: os.getenv("CASA_LLM_DEFAULT_MODEL", ""))
    llm_default_provider: str = field(
        default_factory=lambda: os.getenv("CASA_LLM_DEFAULT_PROVIDER", "openai")
    )
    llm_api_key: str = field(default_factory=lambda: os.getenv("CASA_LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: os.getenv("CASA_LLM_BASE_URL", ""))
    llm_temperature: float = field(
        default_factory=lambda: _env_float("CASA_LLM_TEMPERATURE", "0.2")
    )
    llm_max_tokens: int = field(
        default_factory=lambda: _env_int("CASA_LLM_MAX_TOKENS", "4000")
    )
    llm_timeout_seconds: float = field(
        default_factory=lambda: _env_float("CASA_LLM_TIMEOUT", "60.0")
    )
    # 多 provider 凭据：{"anthropic": LLMProviderConfig(...), "openai": ...}
    llm_providers: dict[str, LLMProviderConfig] = field(default_factory=dict)

    # --- 编排 ---
    orchestrator_llm_enabled: bool = field(
        default_factory=lambda: os.getenv("CASA_ORCHESTRATOR_LLM_ENABLED", "false").lower() == "true"
    )
    stage_max_iterations: int = field(
        default_factory=lambda: _env_int("CASA_STAGE_MAX_ITERATIONS", "5")
    )
    stage_simple_retries: int = field(
        default_factory=lambda: _env_int("CASA_STAGE_SIMPLE_RETRIES", "2")
    )
    stage_fresh_session_retries: int = field(
        default_factory=lambda: _env_int("CASA_STAGE_FRESH_SESSION_RETRIES", "1")
    )

    # --- 并发 ---
    concurrency_policy: str = field(
        default_factory=lambda: os.getenv("CASA_CONCURRENCY_POLICY", ConcurrencyPolicy.REJECT)
    )
    max_parallel_per_session: int = field(
        default_factory=lambda: _env_int("CASA_MAX_PARALLEL_PER_SESSION", "4")
    )

    # --- 调度状态外置（分布式必需）---
    scheduler_state_backend: str = field(
        default_factory=lambda: os.getenv("CASA_SCHEDULER_BACKEND", "memory")
    )
    redis_url: str = field(default_factory=lambda: os.getenv("CASA_REDIS_URL", ""))

    # --- Contract ---
    contract_validation_enabled: bool = field(
        default_factory=lambda: os.getenv("CASA_CONTRACT_VALIDATION_ENABLED", "true").lower() == "true"
    )

    # --- 调试 ---
    debug: bool = field(default_factory=lambda: os.getenv("CASA_DEBUG", "false").lower() == "true")
    dry_run: bool = field(default_factory=lambda: os.getenv("CASA_DRY_RUN", "false").lower() == "true")
    auto_render_deliverable: bool = field(
        default_factory=lambda: os.getenv("CASA_AUTO_RENDER_DELIVERABLE", "false").lower() == "true"
    )
    max_eval_stages_per_plan: int = field(
        default_factory=lambda: _env_int("CASA_MAX_EVAL_STAGES_PER_PLAN", "50")
    )
    auto_skip_deadlocked_stages: bool = field(
        default_factory=lambda: os.getenv("CASA_AUTO_SKIP_DEADLOCKED_STAGES", "false").lower() == "true"
    )
    lifecycle_auto_cleanup: bool = field(
        default_factory=lambda: os.getenv("CASA_LIFECYCLE_AUTO_CLEANUP", "false").lower() == "true"
    )
    lifecycle_cleanup_interval_seconds: float = field(
        default_factory=lambda: _env_float("CASA_LIFECYCLE_CLEANUP_INTERVAL", "3600.0")
    )
    auto_replan_on_deadlock: bool = field(
        default_factory=lambda: os.getenv("CASA_AUTO_REPLAN_ON_DEADLOCK", "false").lower() == "true"
    )

    def resolve_llm_provider(self, provider: str | None = None) -> LLMProviderConfig:
        """按 provider 名解析凭据；未配置时回退到全局 llm_* 字段。"""
        name = provider or self.llm_default_provider or "openai"
        if name in self.llm_providers:
            return self.llm_providers[name]
        return LLMProviderConfig(
            provider=name,
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
            default_model=self.llm_default_model,
        )

    @staticmethod
    def _normalize_provider_name(model_or_provider: str) -> str:
        """从 model_preference 推断 provider key（如 claude-* → anthropic）。"""
        if not model_or_provider:
            return ""
        lower = model_or_provider.lower()
        if lower.startswith("claude") or "anthropic" in lower:
            return "anthropic"
        if lower.startswith(("gpt", "o1", "o2", "o3", "o4")) or "openai" in lower:
            return "openai"
        if lower.startswith("gemini") or "google" in lower:
            return "google"
        return model_or_provider.split("-")[0]

    def get_llm_config(self, provider: str | None = None) -> dict[str, str]:
        """
        按 provider / model_preference 取凭据 dict，供 AgentExecutor 从 context 读取。

        Agent 实现者使用 ``context["llm_config"]["api_key"]``，无需查环境变量。
        """
        if provider:
            name = provider if provider in self.llm_providers else self._normalize_provider_name(provider)
            if not name or name not in self.llm_providers:
                name = self._normalize_provider_name(provider) or provider
        else:
            name = self.llm_default_provider or "openai"
        resolved = self.resolve_llm_provider(name)
        return {
            "provider": resolved.provider,
            "api_key": resolved.api_key,
            "base_url": resolved.base_url,
            "default_model": resolved.default_model or self.llm_default_model,
        }

    def __post_init__(self) -> None:
        if self.llm_providers:
            normalized: dict[str, LLMProviderConfig] = {}
            for name, cfg in self.llm_providers.items():
                if isinstance(cfg, LLMProviderConfig):
                    normalized[name] = cfg
                elif isinstance(cfg, dict):
                    normalized[name] = LLMProviderConfig(
                        provider=cfg.get("provider", name),
                        api_key=cfg.get("api_key", ""),
                        base_url=cfg.get("base_url", ""),
                        default_model=cfg.get("default_model", ""),
                    )
                else:
                    normalized[name] = cfg
            object.__setattr__(self, "llm_providers", normalized)
        errors: list[str] = []
        valid_backends = {StorageBackend.LOCAL, StorageBackend.MINIO, StorageBackend.S3}
        if self.artifact_storage_backend not in valid_backends:
            errors.append(
                f"artifact_storage_backend 须为 {sorted(valid_backends)} 之一，"
                f"当前: {self.artifact_storage_backend!r}"
            )
        if self.concurrency_policy not in {ConcurrencyPolicy.REJECT, ConcurrencyPolicy.FIFO}:
            errors.append(f"concurrency_policy 无效: {self.concurrency_policy!r}")
        if self.scheduler_state_backend not in {"memory", "redis"}:
            errors.append(f"scheduler_state_backend 须为 memory 或 redis")
        if self.scheduler_state_backend == "redis" and not self.redis_url:
            errors.append("scheduler_state_backend=redis 时 redis_url 不能为空")
        if self.max_parallel_per_session < 1:
            errors.append("max_parallel_per_session 须 >= 1")
        if self.stage_simple_retries < 0:
            errors.append("stage_simple_retries 须 >= 0")
        if self.artifact_storage_backend in {StorageBackend.MINIO, StorageBackend.S3}:
            if not self.s3_bucket:
                errors.append("minio/s3 模式下 s3_bucket 不能为空")
        if not (0 <= self.llm_temperature <= 2):
            errors.append("llm_temperature 须在 0-2 之间")
        if self.llm_max_tokens <= 0:
            errors.append("llm_max_tokens 须 > 0")
        if self.max_eval_stages_per_plan < 0:
            errors.append("max_eval_stages_per_plan 须 >= 0")
        if self.lifecycle_cleanup_interval_seconds <= 0:
            errors.append("lifecycle_cleanup_interval_seconds 须 > 0")
        if self.tenant_id:
            if not self.tenant_id.strip():
                errors.append("tenant_id 不能为纯空白")
            for ch in ("/", "\\", "..", "\x00"):
                if ch in self.tenant_id:
                    errors.append(f"tenant_id 含非法字符: {self.tenant_id!r}")
        if errors:
            raise ConfigValidationError(errors)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（隐藏敏感字段）。"""
        d = {}
        for f in self.__dataclass_fields__:
            v = getattr(self, f)
            if any(kw in f for kw in ("key", "secret", "password", "token")):
                d[f] = "***" if v else ""
            else:
                d[f] = v
        return d

    def to_dict_safe(self) -> dict[str, Any]:
        """序列化为 dict（完整值，不含掩码——仅内部使用）。"""
        return {
            f: getattr(self, f)
            for f in self.__dataclass_fields__
        }


# ============================================================================
# 全局单例（领域项目启动时创建）
# ============================================================================
_config: CASAConfig | None = None
_config_lock = threading.Lock()


def get_config() -> CASAConfig:
    """获取全局 CASAConfig 单例（线程安全）。"""
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = CASAConfig()
    return _config


def reload_config(*, path: str | None = None, profile: str | None = None) -> CASAConfig:
    """显式从文件重载配置（不自动监听）。"""
    from .config_loader import ConfigLoader
    current = get_config()
    cfg_path = path or current.config_file
    prof = profile or current.config_profile
    if not cfg_path:
        raise ConfigValidationError(["reload_config 需要 config_file 路径"])
    if cfg_path.endswith((".yaml", ".yml")):
        loaded = ConfigLoader.from_yaml(cfg_path, profile=prof)
    elif cfg_path.endswith(".toml"):
        loaded = ConfigLoader.from_toml(cfg_path, profile=prof)
    else:
        raise ConfigValidationError([f"不支持的配置文件格式: {cfg_path}"])
    return init_config(**loaded.to_dict_safe())


def init_config(**overrides: Any) -> CASAConfig:
    """
    初始化/覆盖配置（领域项目入口调用）。

    应在单线程启动阶段调用。运行中覆盖请使用 override_config()。
    """
    global _config
    try:
        with _config_lock:
            _config = CASAConfig(**overrides)
    except ConfigValidationError as exc:
        raise ConfigValidationError(
            [f"配置校验失败: {e}" for e in exc.errors]
        ) from exc
    return _config


@contextmanager
def override_config(**overrides: Any) -> Iterator[CASAConfig]:
    """
    临时覆盖配置的 context manager（用于测试隔离）。

    用法::

        with override_config(debug=True, dry_run=True):
            cfg = get_config()
            assert cfg.debug

    使用实例深拷贝保留敏感字段真实值，
    避免 to_dict() 掩码 "***" 导致的敏感值丢失。
    """
    global _config
    with _config_lock:
        old = _config
        if old is None:
            merged = overrides
        else:
            # 深拷贝原实例以保留敏感字段（非掩码值）
            merged = old.to_dict_safe()
            merged = {k: v for k, v in merged.items() if v != "***"}
            merged.update(overrides)
        _config = CASAConfig(**merged)
        try:
            yield _config
        finally:
            _config = old


def reset_config() -> None:
    """重置配置为默认值（测试清理用）。"""
    global _config
    with _config_lock:
        _config = None
