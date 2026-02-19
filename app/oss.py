from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import oss2
from flask import current_app


def _to_bool(raw, *, default: bool = False) -> bool:
    value = str(raw if raw is not None else "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _has_remote_config() -> bool:
    cfg = current_app.config
    return bool(
        cfg.get("OSS_ENDPOINT")
        and cfg.get("OSS_ACCESS_KEY_ID")
        and cfg.get("OSS_ACCESS_KEY_SECRET")
        and cfg.get("OSS_BUCKET")
    )


def has_remote_backend() -> bool:
    return _has_remote_config()


def _allow_remote_failover_local() -> bool:
    return _to_bool(current_app.config.get("OSS_REMOTE_FAILOVER_LOCAL", "1"), default=True)


def _get_bucket():
    cfg = current_app.config
    auth = oss2.Auth(cfg["OSS_ACCESS_KEY_ID"], cfg["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, cfg["OSS_ENDPOINT"], cfg["OSS_BUCKET"])


def _local_root() -> Path:
    root = Path(current_app.config.get("OSS_LOCAL_DIR") or "data/oss-local")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _local_path(key: str, *, ensure_parent: bool) -> Path:
    rel = str(key or "").strip().lstrip("/")
    if not rel:
        raise ValueError("empty key")
    target = (_local_root() / rel).resolve()
    root = _local_root().resolve()
    if root not in target.parents and target != root:
        raise ValueError("invalid key")
    if ensure_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _safe_local_path(key: str) -> Path:
    return _local_path(key, ensure_parent=True)


def _existing_local_path(key: str) -> Path:
    return _local_path(key, ensure_parent=False)


def _read_local_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    if max_bytes and int(max_bytes) > 0:
        with path.open("rb") as fp:
            return fp.read(int(max_bytes))
    return path.read_bytes()


def _copy_local_to_file(source: Path, target: Path, *, max_bytes: int | None = None) -> int:
    total = 0
    remaining = int(max_bytes) if max_bytes and int(max_bytes) > 0 else None
    with source.open("rb") as src, target.open("wb") as dst:
        while True:
            chunk_size = 65536 if remaining is None else min(65536, remaining)
            if chunk_size <= 0:
                break
            chunk = src.read(chunk_size)
            if not chunk:
                break
            dst.write(chunk)
            total += len(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return total


def object_exists(key: str) -> bool:
    if _has_remote_config():
        try:
            if _get_bucket().object_exists(key):
                return True
        except Exception:
            pass
    try:
        return _existing_local_path(key).exists()
    except Exception:
        return False


def get_object_bytes(key: str, *, max_bytes: int | None = None) -> bytes:
    if _has_remote_config():
        try:
            result = _get_bucket().get_object(key)
            if max_bytes and int(max_bytes) > 0:
                return result.read(int(max_bytes))
            return result.read()
        except Exception:
            local_path = _existing_local_path(key)
            if local_path.exists():
                return _read_local_bytes(local_path, max_bytes=max_bytes)
            raise

    path = _existing_local_path(key)
    return _read_local_bytes(path, max_bytes=max_bytes)


def get_object_to_file(key: str, filename: str, *, max_bytes: int | None = None) -> int:
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)

    if _has_remote_config():
        try:
            result = _get_bucket().get_object(key)
            total = 0
            remaining = int(max_bytes) if max_bytes and int(max_bytes) > 0 else None
            with target.open("wb") as fp:
                while True:
                    chunk_size = 65536 if remaining is None else min(65536, remaining)
                    if chunk_size <= 0:
                        break
                    chunk = result.read(chunk_size)
                    if not chunk:
                        break
                    fp.write(chunk)
                    total += len(chunk)
                    if remaining is not None:
                        remaining -= len(chunk)
            return total
        except Exception:
            source = _existing_local_path(key)
            if source.exists():
                return _copy_local_to_file(source, target, max_bytes=max_bytes)
            raise

    source = _existing_local_path(key)
    return _copy_local_to_file(source, target, max_bytes=max_bytes)


def put_object_from_file(key: str, filename: str, content_type: Optional[str] = None) -> None:
    if _has_remote_config():
        headers = {"Content-Type": content_type} if content_type else None
        try:
            _get_bucket().put_object_from_file(key, filename, headers=headers)
            return
        except Exception:
            if not _allow_remote_failover_local():
                raise
            current_app.logger.warning("OSS remote upload failed; falling back to local storage", exc_info=True)
    shutil.copyfile(filename, _safe_local_path(key))


def put_object_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> None:
    if _has_remote_config():
        headers = {"Content-Type": content_type} if content_type else None
        try:
            _get_bucket().put_object(key, data, headers=headers)
            return
        except Exception:
            if not _allow_remote_failover_local():
                raise
            current_app.logger.warning("OSS remote upload failed; falling back to local storage", exc_info=True)
    _safe_local_path(key).write_bytes(data)


def delete_object(key: str) -> None:
    if _has_remote_config():
        try:
            _get_bucket().delete_object(key)
            return
        except Exception:
            path = _existing_local_path(key)
            if path.exists():
                path.unlink(missing_ok=True)
                return
            raise
    try:
        _existing_local_path(key).unlink(missing_ok=True)
    except Exception:
        pass


def copy_object(source_key: str, target_key: str) -> None:
    if _has_remote_config():
        try:
            bucket = _get_bucket()
            bucket.copy_object(bucket.bucket_name, source_key, target_key)
            return
        except Exception:
            source_path = _existing_local_path(source_key)
            if source_path.exists():
                shutil.copyfile(source_path, _safe_local_path(target_key))
                return
            raise
    shutil.copyfile(_existing_local_path(source_key), _safe_local_path(target_key))


def sign_get_url(key: str, *, expires: int = 3600, params: Optional[dict] = None) -> str:
    if _has_remote_config():
        try:
            return _get_bucket().sign_url("GET", key, int(expires), params=params)
        except Exception:
            if not _allow_remote_failover_local():
                raise

    base = str(current_app.config.get("OSS_PUBLIC_BASE_URL") or "").strip()
    if not base:
        return ""
    query = f"?{urlencode(params)}" if params else ""
    return f"{base.rstrip('/')}/{key}{query}"


def sign_put_url(
    key: str,
    *,
    expires: int = 900,
    content_type: str | None = None,
    params: Optional[dict] = None,
) -> str:
    if not _has_remote_config():
        return ""
    headers = {"Content-Type": content_type} if content_type else None
    return _get_bucket().sign_url("PUT", key, int(expires), headers=headers, params=params)


def public_url(key: str, *, expires: int = 3600, params: Optional[dict] = None) -> str:
    cfg = current_app.config
    base = str(cfg.get("OSS_PUBLIC_BASE_URL") or "").strip()
    assume_public = str(cfg.get("OSS_ASSUME_PUBLIC") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if base and assume_public:
        query = f"?{urlencode(params)}" if params else ""
        return f"{base.rstrip('/')}/{key}{query}"
    return sign_get_url(key, expires=expires, params=params)
