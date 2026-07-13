"""
CASA 配置加载器 — 支持 profile 的 YAML/TOML 文件加载。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import fields
from typing import Any, Callable

from .config import CASAConfig, ConfigValidationError

logger = logging.getLogger("casa.config_loader")


class ConfigLoader:
    """统一配置加载器：文件 + profile + 合并。"""

    @staticmethod
    def merge(*configs: CASAConfig) -> CASAConfig:
        if not configs:
            return CASAConfig()
        merged: dict[str, Any] = {}
        for cfg in configs:
            for f in fields(CASAConfig):
                val = getattr(cfg, f.name)
                if val not in (None, "", [], {}):
                    merged[f.name] = val
        return CASAConfig(**merged)

    @staticmethod
    def from_mapping(data: dict[str, Any], *, profile: str = "default") -> CASAConfig:
        base = dict(data.get("default", data))
        profiles = data.get("profiles", {})
        if profile in profiles:
            base.update(profiles[profile])
        elif profile != "default" and profile in data:
            base.update(data[profile])
        allowed = {f.name for f in fields(CASAConfig)}
        filtered = {k: v for k, v in base.items() if k in allowed}
        filtered["config_profile"] = profile
        return CASAConfig(**filtered)

    @staticmethod
    def from_yaml(path: str, *, profile: str = "default") -> CASAConfig:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigValidationError([
                "YAML 配置需要 PyYAML：pip install 'casa-frame[config]'"
            ]) from exc
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = ConfigLoader.from_mapping(data, profile=profile)
        cfg.config_file = path
        return cfg

    @staticmethod
    def from_toml(path: str, *, profile: str = "default") -> CASAConfig:
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError as exc:
                raise ConfigValidationError([
                    "TOML 配置需要 tomllib (py3.11+) 或 tomli：pip install 'casa-frame[config]'"
                ]) from exc
        with open(path, "rb") as f:
            data = tomllib.load(f)
        cfg = ConfigLoader.from_mapping(data, profile=profile)
        cfg.config_file = path
        return cfg


class ConfigWatcher:
    """
    配置文件热重载。

    mode:
      - ``poll``: 固定间隔检查 mtime（默认回退）
      - ``watch``: 强制使用 watchdog（inotify/fsevents）
      - ``auto``: 有 watchdog 时用文件系统事件，否则 poll
    """

    def __init__(
        self,
        path: str,
        *,
        profile: str = "default",
        poll_seconds: float = 5.0,
        mode: str = "auto",
        on_reload: Callable[[], None] | None = None,
    ):
        self.path = path
        self.profile = profile
        self.poll_seconds = poll_seconds
        self.mode = mode
        self._on_reload = on_reload
        self._last_mtime: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Any = None
        import os
        if os.path.exists(path):
            self._last_mtime = os.path.getmtime(path)

    def _reload(self) -> bool:
        if self.check_and_reload():
            if self._on_reload:
                self._on_reload()
            return True
        return False

    def check_and_reload(self) -> bool:
        import os
        from .config import init_config
        if not os.path.exists(self.path):
            return False
        mtime = os.path.getmtime(self.path)
        if mtime <= self._last_mtime:
            return False
        self._last_mtime = mtime
        if self.path.endswith((".yaml", ".yml")):
            cfg = ConfigLoader.from_yaml(self.path, profile=self.profile)
        elif self.path.endswith(".toml"):
            cfg = ConfigLoader.from_toml(self.path, profile=self.profile)
        else:
            return False
        init_config(**cfg.to_dict_safe())
        logger.info("config reloaded from %s (profile=%s)", self.path, self.profile)
        return True

    def _watchdog_available(self) -> bool:
        try:
            import watchdog  # noqa: F401
            return True
        except ImportError:
            return False

    def _effective_mode(self) -> str:
        if self.mode == "watch":
            if not self._watchdog_available():
                logger.warning(
                    "ConfigWatcher mode=watch 需要 watchdog：pip install watchdog；回退 poll",
                )
                return "poll"
            return "watch"
        if self.mode == "auto" and self._watchdog_available():
            return "watch"
        return "poll"

    def start(self) -> None:
        """启动后台监听（文件事件或 poll）。"""
        if self._thread and self._thread.is_alive():
            return
        if self._observer is not None:
            return
        self._stop.clear()
        effective = self._effective_mode()
        if effective == "watch":
            self._start_watchdog()
        else:
            self._thread = threading.Thread(
                target=self._poll_loop, name="casa-config-watcher", daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        """停止后台监听。"""
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=timeout or 5)
            self._observer = None
        if self._thread:
            self._thread.join(timeout=timeout or self.poll_seconds + 5)
            self._thread = None

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._reload()
            self._stop.wait(self.poll_seconds)

    def _start_watchdog(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event: Any) -> None:
                if event.is_directory:
                    return
                if event.src_path == watcher.path:
                    watcher._reload()

        handler = _Handler()
        self._observer = Observer()
        import os
        self._observer.schedule(handler, os.path.dirname(self.path) or ".", recursive=False)
        self._observer.start()
        logger.info("ConfigWatcher started (watchdog) for %s", self.path)
