import json
from typing import Iterable, Optional
from urllib.parse import urlencode

import oss2
from flask import current_app


def _get_bucket():
    cfg = current_app.config
    auth = oss2.Auth(cfg["OSS_ACCESS_KEY_ID"], cfg["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, cfg["OSS_ENDPOINT"], cfg["OSS_BUCKET"])


def object_exists(key: str) -> bool:
    bucket = _get_bucket()
    return bucket.object_exists(key)


def list_objects(prefix: str, suffix: Optional[str] = None) -> Iterable[str]:
    bucket = _get_bucket()
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        key = obj.key
        if suffix and not key.endswith(suffix):
            continue
        yield key


def list_objects_with_meta(prefix: str, suffix: Optional[str] = None) -> Iterable[dict]:
    bucket = _get_bucket()
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        key = obj.key
        if suffix and not key.endswith(suffix):
            continue
        yield {
            "key": key,
            "last_modified": int(getattr(obj, "last_modified", 0) or 0),
        }


def get_object_bytes(key: str) -> bytes:
    bucket = _get_bucket()
    result = bucket.get_object(key)
    return result.read()


def get_object_text(key: str) -> str:
    return get_object_bytes(key).decode("utf-8")


def get_object_json(key: str):
    if not object_exists(key):
        return None
    return json.loads(get_object_text(key))


def put_object(key: str, data: bytes, content_type: Optional[str] = None) -> None:
    bucket = _get_bucket()
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    bucket.put_object(key, data, headers=headers)


def put_object_text(key: str, text: str, content_type: Optional[str] = "text/plain") -> None:
    put_object(key, text.encode("utf-8"), content_type=content_type)


def put_object_json(key: str, payload: dict) -> None:
    put_object_text(key, json.dumps(payload, ensure_ascii=False, indent=2), content_type="application/json")


def put_object_from_file(key: str, filename: str, content_type: Optional[str] = None) -> None:
    bucket = _get_bucket()
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    bucket.put_object_from_file(key, filename, headers=headers)


def delete_object(key: str) -> None:
    bucket = _get_bucket()
    bucket.delete_object(key)


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


def sign_put_url(key: str, expires: int = 900, headers: Optional[dict] = None) -> str:
    bucket = _get_bucket()
    return bucket.sign_url("PUT", key, expires, headers=headers)
