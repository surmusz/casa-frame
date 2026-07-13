"""配置加载器测试。"""
import tempfile
import os

from casa.config import CASAConfig, init_config, reset_config
from casa.config_loader import ConfigLoader


def test_merge_configs():
    a = CASAConfig(max_parallel_per_session=2)
    b = CASAConfig(max_parallel_per_session=8, debug=True)
    merged = ConfigLoader.merge(a, b)
    assert merged.max_parallel_per_session == 8
    assert merged.debug is True


def test_from_mapping_profile():
    data = {
        "default": {"max_parallel_per_session": 4, "debug": False},
        "profiles": {"dev": {"debug": True}},
    }
    cfg = ConfigLoader.from_mapping(data, profile="dev")
    assert cfg.debug is True
    assert cfg.max_parallel_per_session == 4
    assert cfg.config_profile == "dev"


def test_from_yaml(tmp_path):
    yaml = tmp_path / "casa.yaml"
    yaml.write_text("default:\n  max_parallel_per_session: 6\n", encoding="utf-8")
    try:
        import yaml as _yaml  # noqa: F401
    except ImportError:
        return
    cfg = ConfigLoader.from_yaml(str(yaml))
    assert cfg.max_parallel_per_session == 6
