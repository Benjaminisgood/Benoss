from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from flask import current_app

from .runtime_settings import get_setting_str


def _base_dir() -> Path:
    default_path = str(current_app.config.get("LOCAL_DAILY_ARCHIVE_DIR") or "")
    configured = get_setting_str("LOCAL_DAILY_ARCHIVE_DIR", default=default_path).strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        path = Path(current_app.root_path).parent / "data" / "daily-archive"
    if not path.is_absolute():
        path = (Path(current_app.root_path).parent / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_file_path(day_value: date) -> Path:
    return _base_dir() / f"{day_value.isoformat()}.json"


def _record_text(record) -> str:
    content = getattr(record, "content", None)
    if not content:
        return ""
    if getattr(content, "kind", "") == "text":
        return str(getattr(content, "text_content", "") or "").strip()
    filename = str(getattr(content, "filename", "") or "").strip() or "file"
    content_type = str(getattr(content, "content_type", "") or "").strip() or "application/octet-stream"
    preview = str(getattr(record, "preview", "") or "").strip()
    parts = [f"[FILE] {filename} ({content_type})"]
    if preview:
        parts.append(preview)
    return "\n".join(parts).strip()


def _record_payload(record) -> dict:
    user = getattr(record, "user", None)
    created_at = getattr(record, "created_at", None)
    updated_at = getattr(record, "updated_at", None)
    return {
        "id": int(getattr(record, "id", 0) or 0),
        "record_no": int(getattr(record, "id", 0) or 0),
        "format": str(getattr(record, "format", "") or ""),
        "visibility": str(getattr(record, "visibility", "") or "private"),
        "preview": str(getattr(record, "preview", "") or ""),
        "tags": list(getattr(record, "get_tags", lambda: [])() or []),
        "created_at": created_at.isoformat() + "Z" if created_at else None,
        "updated_at": updated_at.isoformat() + "Z" if updated_at else None,
        "user": {
            "id": int(getattr(user, "id", 0) or int(getattr(record, "user_id", 0) or 0)),
            "username": str(getattr(user, "username", "") or ""),
        },
        "text": _record_text(record),
    }


def save_daily_archive(
    day_value: date,
    records: Sequence,
    *,
    scope: str = "public",
    source: str = "home_today",
    timezone_name: str = "UTC",
) -> dict:
    path = archive_file_path(day_value)
    payload = {
        "day": day_value.isoformat(),
        "scope": str(scope or "public"),
        "source": str(source or "unknown"),
        "timezone": str(timezone_name or "UTC"),
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "record_count": len(records),
        "records": [_record_payload(item) for item in records],
    }

    changed = True
    existing = load_archive(path) if path.exists() else {}
    if existing:
        previous_snapshot = {
            "day": existing.get("day"),
            "scope": existing.get("scope"),
            "source": existing.get("source"),
            "timezone": existing.get("timezone"),
            "records": existing.get("records"),
        }
        current_snapshot = {
            "day": payload.get("day"),
            "scope": payload.get("scope"),
            "source": payload.get("source"),
            "timezone": payload.get("timezone"),
            "records": payload.get("records"),
        }
        if previous_snapshot == current_snapshot:
            changed = False
            payload["updated_at"] = existing.get("updated_at") or payload["updated_at"]

    if changed:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "path": str(path),
        "day": payload["day"],
        "scope": payload["scope"],
        "record_count": payload["record_count"],
        "updated_at": payload["updated_at"],
        "changed": changed,
    }


def list_archive_files() -> list[Path]:
    root = _base_dir()
    return sorted(root.glob("*.json"), key=lambda p: p.name)


def load_archive(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}
