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


def put_object_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> None:
    bucket = _get_bucket()
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    bucket.put_object(key, data, headers=headers)


def delete_object(key: str) -> None:
    bucket = _get_bucket()
    bucket.delete_object(key)


def copy_object(source_key: str, target_key: str) -> None:
    bucket = _get_bucket()
    bucket.copy_object(bucket.bucket_name, source_key, target_key)


def head_object(key: str) -> oss2.models.HeadObjectResult:
    bucket = _get_bucket()
    return bucket.head_object(key)


def sign_put_url(
    key: str,
    *,
    expires: int = 15 * 60,
    content_type: Optional[str] = None,
) -> tuple[str, dict]:
    """Return a signed PUT URL + the exact headers the client must send.

    OSS signatures include some headers (e.g. Content-Type). If we sign it,
    the client must send the same value, otherwise the request will be rejected.
    """

    bucket = _get_bucket()
    signed_headers: dict[str, str] = {}
    if content_type:
        signed_headers["Content-Type"] = content_type
    url = bucket.sign_url("PUT", key, int(expires), headers=signed_headers or None)
    return url, signed_headers


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
