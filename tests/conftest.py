"""CASA 测试共享 fixture。"""
import pytest

from casa.config import reset_config
from casa.audit import reset_audit_sink
from casa.observability import reset_metrics_sink, reset_run_context
from casa.scheduler import reset_scheduler
from casa.artifact import reset_backend_cache


@pytest.fixture(autouse=True)
def reset_globals():
    reset_config()
    reset_metrics_sink()
    reset_scheduler()
    reset_backend_cache()
    reset_audit_sink()
    reset_run_context()
    yield
    reset_config()
    reset_metrics_sink()
    reset_scheduler()
    reset_backend_cache()
    reset_audit_sink()
    reset_run_context()
