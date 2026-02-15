from datetime import datetime
from pathlib import Path

from flask import current_app


def _prefix() -> str:
    raw = str(current_app.config.get("OSS_PREFIX") or "benoss").strip("/")
    return raw or "benoss"


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if not suffix:
        return ""
    if len(suffix) > 16:
        return ""
    if not all(ch.isalnum() or ch == "." for ch in suffix):
        return ""
    return suffix


def record_content_key(content_uuid: str, filename: str = "", when: datetime | None = None) -> str:
    now = when or datetime.utcnow()
    suffix = _safe_suffix(filename)
    day = now.strftime("%Y-%m-%d")
    return f"{_prefix()}/records/{day}/objects/{content_uuid}{suffix}"


def generated_asset_key(
    user_id: int,
    asset_kind: str,
    asset_uuid: str,
    filename: str = "",
    when: datetime | None = None,
) -> str:
    now = when or datetime.utcnow()
    day = now.strftime("%Y-%m-%d")
    suffix = _safe_suffix(filename)
    safe_kind = (asset_kind or "asset").strip().replace("/", "-")
    return f"{_prefix()}/generated/{day}/user-{int(user_id)}/{safe_kind}/{asset_uuid}{suffix}"
