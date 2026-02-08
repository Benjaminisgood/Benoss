from typing import Optional
from urllib.parse import urlencode

import oss2
from flask import current_app


def _get_bucket():
    cfg = current_app.config
    auth = oss2.Auth(cfg["OSS_ACCESS_KEY_ID"], cfg["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, cfg["OSS_ENDPOINT"], cfg["OSS_BUCKET"])


def get_object_bytes(key: str) -> bytes:
    bucket = _get_bucket()
    result = bucket.get_object(key)
    return result.read()


def put_object_from_file(key: str, filename: str, content_type: Optional[str] = None) -> None:
    bucket = _get_bucket()
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    bucket.put_object_from_file(key, filename, headers=headers)


def delete_object(key: str) -> None:
    bucket = _get_bucket()
    bucket.delete_object(key)


def copy_object(source_key: str, target_key: str) -> None:
    bucket = _get_bucket()
    bucket.copy_object(bucket.bucket_name, source_key, target_key)


def _is_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def public_url(key: str, expires: int = 3600, params: Optional[dict] = None) -> str:
    cfg = current_app.config
    base = cfg.get("OSS_PUBLIC_BASE_URL")
    assume_public = _is_truthy(cfg.get("OSS_ASSUME_PUBLIC"))
    if base and assume_public:
        query = f"?{urlencode(params)}" if params else ""
        return f"{base.rstrip('/')}/{key}{query}"
    bucket = _get_bucket()
    return bucket.sign_url("GET", key, expires, params=params)

