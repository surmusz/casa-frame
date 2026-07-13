"""配置校验测试 — Phase 3。"""
import pytest

from casa.config import CASAConfig, ConfigValidationError, init_config, reset_config


def test_invalid_backend_raises():
    with pytest.raises(ConfigValidationError):
        CASAConfig(artifact_storage_backend="postgres")


def test_invalid_concurrency_policy_raises():
    with pytest.raises(ConfigValidationError):
        CASAConfig(concurrency_policy="lifo")


def test_redis_without_url_raises():
    with pytest.raises(ConfigValidationError):
        CASAConfig(scheduler_state_backend="redis", redis_url="")


def test_init_config_propagates_validation_error():
    reset_config()
    with pytest.raises(ConfigValidationError):
        init_config(artifact_storage_backend="invalid")


def test_valid_config_passes():
    cfg = CASAConfig(max_parallel_per_session=2)
    assert cfg.max_parallel_per_session == 2
