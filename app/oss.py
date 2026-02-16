from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import oss2
from flask import current_app


def _has_remote_config() -> bool:
    cfg = current_app.config
    return bool(
        cfg.get("OSS_ENDPOINT")
        and cfg.get("OSS_ACCESS_KEY_ID")
        and cfg.get("OSS_ACCESS_KEY_SECRET")
        and cfg.get("OSS_BUCKET")
    )


def _get_bucket():
    cfg = current_app.config
    auth = oss2.Auth(cfg["OSS_ACCESS_KEY_ID"], cfg["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, cfg["OSS_ENDPOINT"], cfg["OSS_BUCKET"])


def _local_root() -> Path:
    root = Path(current_app.config.get("OSS_LOCAL_DIR") or "data/oss-local")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_local_path(key: str) -> Path:
    rel = str(key or "").strip().lstrip("/")
    if not rel:
        raise ValueError("empty key")
    target = (_local_root() / rel).resolve()
    root = _local_root().resolve()
    if root not in target.parents and target != root:
        raise ValueError("invalid key")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def get_object_bytes(key: str, *, max_bytes: int | None = None) -> bytes:
    if _has_remote_config():
        result = _get_bucket().get_object(key)
        if max_bytes and int(max_bytes) > 0:
            return result.read(int(max_bytes))
        return result.read()

    path = _safe_local_path(key)
    if max_bytes and int(max_bytes) > 0:
        with path.open("rb") as fp:
            return fp.read(int(max_bytes))
    return path.read_bytes()


def get_object_to_file(key: str, filename: str, *, max_bytes: int | None = None) -> int:
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)

    if _has_remote_config():
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

    source = _safe_local_path(key)
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


def put_object_from_file(key: str, filename: str, content_type: Optional[str] = None) -> None:
    if _has_remote_config():
        headers = {"Content-Type": content_type} if content_type else None
        _get_bucket().put_object_from_file(key, filename, headers=headers)
        return
    shutil.copyfile(filename, _safe_local_path(key))


def put_object_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> None:
    if _has_remote_config():
        headers = {"Content-Type": content_type} if content_type else None
        _get_bucket().put_object(key, data, headers=headers)
        return
    _safe_local_path(key).write_bytes(data)


def delete_object(key: str) -> None:
    if _has_remote_config():
        _get_bucket().delete_object(key)
        return
    try:
        _safe_local_path(key).unlink(missing_ok=True)
    except Exception:
        pass


def copy_object(source_key: str, target_key: str) -> None:
    if _has_remote_config():
        bucket = _get_bucket()
        bucket.copy_object(bucket.bucket_name, source_key, target_key)
        return
    shutil.copyfile(_safe_local_path(source_key), _safe_local_path(target_key))


def sign_get_url(key: str, *, expires: int = 3600, params: Optional[dict] = None) -> str:
    if _has_remote_config():
        return _get_bucket().sign_url("GET", key, int(expires), params=params)

    base = str(current_app.config.get("OSS_PUBLIC_BASE_URL") or "").strip()
    if not base:
        return ""
    query = f"?{urlencode(params)}" if params else ""
    return f"{base.rstrip('/')}/{key}{query}"


def public_url(key: str, *, expires: int = 3600, params: Optional[dict] = None) -> str:
    cfg = current_app.config
    base = str(cfg.get("OSS_PUBLIC_BASE_URL") or "").strip()
    assume_public = str(cfg.get("OSS_ASSUME_PUBLIC") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if base and assume_public:
        query = f"?{urlencode(params)}" if params else ""
        return f"{base.rstrip('/')}/{key}{query}"
    return sign_get_url(key, expires=expires, params=params)
