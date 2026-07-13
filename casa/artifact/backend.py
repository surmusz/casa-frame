"""Artifact 存储后端。"""
from __future__ import annotations

import abc
import json
import logging
import os
import tempfile
import threading
from typing import Any, Callable

from ..config import get_config, StorageBackend, ArtifactDir
from ._path import _safe_join, _validate_path_component, _job_root

logger = logging.getLogger("casa.artifact")

# ============================================================================
# 存储后端接口
# ============================================================================


class ArtifactBackend(abc.ABC):
    """存储后端抽象接口。"""

    @abc.abstractmethod
    def write(self, storage_key: str, data: dict, plan_dir: str, artifact_kind: str) -> None: ...

    @abc.abstractmethod
    def read(self, storage_key: str, plan_dir: str, artifact_kind: str) -> dict | None: ...

    @abc.abstractmethod
    def list_keys(self, plan_dir: str, *, storage_prefix: str = "") -> list[str]: ...

    @abc.abstractmethod
    def exists(self, plan_dir: str, artifact_kind: str, *, storage_prefix: str = "") -> bool: ...

    @abc.abstractmethod
    def delete(self, plan_dir: str, artifact_kind: str, *, storage_prefix: str = "") -> bool: ...

    @abc.abstractmethod
    def write_deliverable_file(
        self, data: bytes, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> str: ...

    @abc.abstractmethod
    def read_deliverable_file(
        self, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> bytes | None: ...

    def write_report(
        self, data: bytes, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> str:
        """已弃用：请实现 write_deliverable_file。"""
        return self.write_deliverable_file(
            data, base_dir, tenant_id, job_id, plan_id, filename,
        )

    def read_report(
        self, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> bytes | None:
        """已弃用：请实现 read_deliverable_file。"""
        return self.read_deliverable_file(
            base_dir, tenant_id, job_id, plan_id, filename,
        )


# ============================================================================
# 本地后端（Local）
# ============================================================================


class LocalArtifactBackend(ArtifactBackend):
    """本地文件系统存储。"""

    def write(self, storage_key: str, data: dict, plan_dir: str, artifact_kind: str) -> None:
        _validate_path_component(artifact_kind, "artifact_kind")
        os.makedirs(plan_dir, exist_ok=True)
        filepath = os.path.join(plan_dir, f"{artifact_kind}.json")
        # 原子写入：先写临时文件再 rename，防止崩溃时残留半写入文件
        fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix=f".{artifact_kind}.", dir=plan_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, filepath)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        logger.info("Artifact written: %s", filepath)

    def read(self, storage_key: str, plan_dir: str, artifact_kind: str) -> dict | None:
        _validate_path_component(artifact_kind, "artifact_kind")
        filepath = os.path.join(plan_dir, f"{artifact_kind}.json")
        try:
            file_size = os.path.getsize(filepath)
            if file_size > _MAX_ARTIFACT_SIZE:
                logger.warning("Artifact too large (%d bytes), rejecting: %s", file_size, filepath)
                return None
            with open(filepath, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Artifact read failed for %s: %s", artifact_kind, type(e).__name__)
            return None

    def list_keys(self, plan_dir: str, *, storage_prefix: str = "") -> list[str]:
        if not os.path.isdir(plan_dir):
            return []
        return sorted(
            f.replace(".json", "")
            for f in os.listdir(plan_dir)
            if f.endswith(".json") and not f.startswith(".hint_")
        )

    def exists(self, plan_dir: str, artifact_kind: str, *, storage_prefix: str = "") -> bool:
        _validate_path_component(artifact_kind, "artifact_kind")
        return os.path.isfile(os.path.join(plan_dir, f"{artifact_kind}.json"))

    def delete(self, plan_dir: str, artifact_kind: str, *, storage_prefix: str = "") -> bool:
        _validate_path_component(artifact_kind, "artifact_kind")
        filepath = os.path.join(plan_dir, f"{artifact_kind}.json")
        try:
            os.remove(filepath)
            return True
        except FileNotFoundError:
            return False

    def write_deliverable_file(
        self, data: bytes, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> str:
        report_dir = os.path.join(
            _job_root(base_dir, tenant_id, job_id), "plans", plan_id, ArtifactDir.REPORTS,
        )
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, filename)
        fd, tmp_path = tempfile.mkstemp(suffix=".html", prefix=f".{filename}.", dir=report_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        return path

    def read_deliverable_file(
        self, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> bytes | None:
        path = os.path.join(
            _job_root(base_dir, tenant_id, job_id), "plans", plan_id, ArtifactDir.REPORTS, filename,
        )
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None


# ============================================================================
# S3 / MinIO 后端（参考实现 — 需 boto3）
# ============================================================================


class S3ArtifactBackend(ArtifactBackend):
    """
    S3/MinIO 存储后端参考实现。

    用法::

        backend = S3ArtifactBackend(
            bucket="casa-artifacts",
            endpoint_url="https://s3.amazonaws.com",
            access_key="...", secret_key="...",
        )
        register_backend("s3", backend)
        init_config(artifact_storage_backend="s3")
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
    ):
        self._bucket = bucket
        self._endpoint = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "S3ArtifactBackend 需要 boto3：pip install 'casa-frame[s3]'"
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint or None,
                aws_access_key_id=self._access_key or None,
                aws_secret_access_key=self._secret_key or None,
                region_name=self._region,
            )

    @staticmethod
    def _object_key(storage_key: str) -> str:
        return f"{storage_key}.json"

    @staticmethod
    def _prefix_from_plan_dir(plan_dir: str) -> str:
        """从 plan_dir 推导 S3 前缀（无 tenant 信息时的回退；优先用 storage_prefix）。"""
        norm = plan_dir.replace("\\", "/").rstrip("/")
        parts = norm.split("/")
        if len(parts) >= 2 and parts[-1] in ("artifacts", "artifact"):
            plan_id = parts[-2]
            if len(parts) >= 4 and parts[-3] == "plans":
                job_id = parts[-4]
                return f"artifacts/{job_id}/{plan_id}/"
        if "plans" in parts:
            idx = parts.index("plans")
            if idx >= 1 and idx + 1 < len(parts):
                job_id = parts[idx - 1]
                plan_id = parts[idx + 1]
                return f"artifacts/{job_id}/{plan_id}/"
        return norm + "/"

    def write(self, storage_key: str, data: dict, plan_dir: str, artifact_kind: str) -> None:
        self._ensure_client()
        _validate_path_component(artifact_kind, "artifact_kind")
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(body) > _MAX_ARTIFACT_SIZE:
            raise ValueError(f"artifact too large: {artifact_kind}")
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._object_key(storage_key),
            Body=body,
            ContentType="application/json",
        )

    def read(self, storage_key: str, plan_dir: str, artifact_kind: str) -> dict | None:
        self._ensure_client()
        try:
            resp = self._client.get_object(
                Bucket=self._bucket, Key=self._object_key(storage_key),
            )
            body = resp["Body"].read()
            if len(body) > _MAX_ARTIFACT_SIZE:
                return None
            return json.loads(body.decode("utf-8"))
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            if exc.__class__.__name__ in ("NoSuchKey", "404"):
                return None
            logger.warning("S3 read failed for %s: %s", storage_key, exc)
            return None

    def list_keys(self, plan_dir: str, *, storage_prefix: str = "") -> list[str]:
        self._ensure_client()
        prefix = storage_prefix or self._prefix_from_plan_dir(plan_dir)
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".json") and not "/.hint_" in key:
                    name = key[len(prefix):]
                    if name.endswith(".json"):
                        keys.append(name[:-5])
        return sorted(keys)

    def exists(self, plan_dir: str, artifact_kind: str, *, storage_prefix: str = "") -> bool:
        self._ensure_client()
        if storage_prefix:
            key = f"{storage_prefix}{artifact_kind}.json"
        else:
            prefix = self._prefix_from_plan_dir(plan_dir)
            key = f"{prefix}{artifact_kind}.json"
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, plan_dir: str, artifact_kind: str, *, storage_prefix: str = "") -> bool:
        self._ensure_client()
        if storage_prefix:
            key = f"{storage_prefix}{artifact_kind}.json"
        else:
            prefix = self._prefix_from_plan_dir(plan_dir)
            key = f"{prefix}{artifact_kind}.json"
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as exc:
            if exc.__class__.__name__ in ("NoSuchKey", "404"):
                return False
            logger.warning("S3 delete failed for %s: %s", key, exc)
            return False

    def write_deliverable_file(
        self, data: bytes, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> str:
        self._ensure_client()
        _validate_path_component(filename, "filename")
        parts = [base_dir.strip("/")] if base_dir else []
        if tenant_id:
            parts.append(tenant_id)
        parts.extend([job_id, "plans", plan_id, ArtifactDir.REPORTS, filename])
        key = "/".join(parts)
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        return key

    def read_deliverable_file(
        self, base_dir: str, tenant_id: str,
        job_id: str, plan_id: str, filename: str,
    ) -> bytes | None:
        self._ensure_client()
        parts = [base_dir.strip("/")] if base_dir else []
        if tenant_id:
            parts.append(tenant_id)
        parts.extend([job_id, "plans", plan_id, ArtifactDir.REPORTS, filename])
        key = "/".join(parts)
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception:
            return None

    def presigned_url(self, storage_key: str, *, expires_in: int = 3600) -> str:
        """生成 artifact 临时下载 URL（S3 扩展能力，不在 ABC 中声明）。"""
        self._ensure_client()
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": self._object_key(storage_key)},
            ExpiresIn=expires_in,
        )


# ============================================================================
# 后端工厂
# ============================================================================

_MAX_ARTIFACT_SIZE = 50 * 1024 * 1024  # 50MB — 防 JSON bomb

_backend_cache: dict[str, ArtifactBackend] = {}
_backend_cache_lock = threading.Lock()


def _resolve_backend(config) -> ArtifactBackend:
    backend_type = config.artifact_storage_backend
    with _backend_cache_lock:
        if backend_type in _backend_cache:
            return _backend_cache[backend_type]

    if backend_type in {StorageBackend.MINIO, StorageBackend.S3}:
        if config.s3_bucket:
            backend = S3ArtifactBackend(
                bucket=config.s3_bucket,
                endpoint_url=config.s3_endpoint,
                access_key=config.s3_access_key,
                secret_key=config.s3_secret_key,
                region=config.s3_region,
            )
            with _backend_cache_lock:
                _backend_cache[backend_type] = backend
            return backend
        logger.warning(
            "%s 后端未配置 s3_bucket，回退 local。请 register_backend() 或设置 CASA_S3_* 环境变量。",
            backend_type,
        )
        backend = LocalArtifactBackend()
    else:
        backend = LocalArtifactBackend()

    with _backend_cache_lock:
        _backend_cache[backend_type] = backend
    return backend


def register_backend(name: str, backend: ArtifactBackend) -> None:
    """注册自定义存储后端。"""
    with _backend_cache_lock:
        _backend_cache[name] = backend
    logger.info("Artifact backend registered: %s", name)


def reset_backend_cache() -> None:
    """清空后端缓存（测试清理用）。"""
    with _backend_cache_lock:
        _backend_cache.clear()


