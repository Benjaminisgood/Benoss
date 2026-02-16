from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from flask import current_app

from ..oss import get_object_bytes
from .runtime_settings import get_setting_int, get_setting_str


_TEXT_FILE_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".kts",
    ".dart",
    ".vue",
    ".svelte",
    ".env",
    ".log",
}
_TEXT_FILE_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/x-toml",
    "application/javascript",
    "application/x-javascript",
    "application/sql",
    "application/csv",
    "application/x-sh",
    "application/x-httpd-php",
}


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


def _normalize_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _content_media_type(*, content_type: str, filename: str) -> str:
    ctype = str(content_type or "").lower()
    name = str(filename or "").lower()
    if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
        return "image"
    if ctype.startswith("video/") or name.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi")):
        return "video"
    if ctype.startswith("audio/") or name.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
        return "audio"
    if ctype.startswith("text/") or name.endswith((".txt", ".md", ".json", ".py", ".js", ".html", ".css", ".csv")):
        return "text"
    return "file"


def _is_text_like_file(*, content_type: str, filename: str, media_type: str) -> bool:
    if media_type == "text":
        return True
    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(str(filename or "").lower()).suffix
    if ctype in _TEXT_FILE_MIME_TYPES:
        return True
    if suffix in _TEXT_FILE_EXTENSIONS:
        return True
    return False


def _decoded_text_quality(text: str) -> float:
    if not text:
        return 0.0
    probe = text[:4000]
    if not probe:
        return 0.0
    readable = sum(1 for ch in probe if ch.isprintable() or ch in {"\n", "\t"})
    return readable / max(1, len(probe))


def _decode_text_bytes(raw: bytes) -> tuple[str, str]:
    sample = raw[:4096]
    if b"\x00" in sample:
        return "", ""

    for encoding in ("utf-8", "utf-8-sig", "gb18030", "gbk", "big5"):
        try:
            text = raw.decode(encoding)
        except Exception:
            continue
        if _decoded_text_quality(text) >= 0.92:
            return text, encoding

    fallback = raw.decode("utf-8", errors="replace")
    if _decoded_text_quality(fallback) >= 0.75:
        return fallback, "utf-8-replace"
    return "", ""


def _extract_file_text(content_payload: dict, *, max_bytes: int) -> dict:
    filename = str(content_payload.get("filename") or "file")
    content_type = str(content_payload.get("content_type") or "application/octet-stream")
    media_type = str(content_payload.get("media_type") or "file")
    oss_key = str(content_payload.get("oss_key") or "")
    size_bytes = int(content_payload.get("size_bytes") or 0)

    result = {
        "status": "not_attempted",
        "text": "",
        "encoding": "",
        "truncated": False,
        "bytes_read": 0,
        "message": "",
    }

    if not oss_key:
        result["status"] = "missing_oss_key"
        result["message"] = "file key unavailable"
        return result
    if not _is_text_like_file(content_type=content_type, filename=filename, media_type=media_type):
        result["status"] = "skipped_non_text"
        result["message"] = "non-text media"
        return result

    try:
        raw = get_object_bytes(oss_key, max_bytes=max_bytes)
    except Exception as exc:
        result["status"] = "read_failed"
        result["message"] = f"read failed: {exc}"
        return result

    result["bytes_read"] = len(raw)
    result["truncated"] = bool(size_bytes and size_bytes > len(raw))

    decoded, encoding = _decode_text_bytes(raw)
    if not decoded:
        result["status"] = "decode_failed"
        result["message"] = "decode failed"
        return result

    text = _normalize_text(decoded)
    if not text:
        result["status"] = "empty_text"
        result["message"] = "decoded text is empty"
        return result

    result["status"] = "ok"
    result["encoding"] = encoding
    result["text"] = text
    return result


def _record_text(record, *, content_payload: dict, extraction: dict) -> str:
    content = getattr(record, "content", None)
    if not content:
        return ""

    if getattr(content, "kind", "") == "text":
        text = _normalize_text(str(getattr(content, "text_content", "") or ""))
        return text or _normalize_text(str(getattr(record, "preview", "") or ""))

    filename = str(content_payload.get("filename") or "file")
    content_type = str(content_payload.get("content_type") or "application/octet-stream")
    preview = _normalize_text(str(getattr(record, "preview", "") or ""))
    header = f"[FILE] {filename} ({content_type})"

    extracted_text = _normalize_text(str(extraction.get("text") or ""))
    if extracted_text:
        title = "[FILE-TEXT]"
        if extraction.get("encoding"):
            title = f"[FILE-TEXT encoding={extraction['encoding']}]"
        file_text = f"{title}\n{extracted_text}"
        if extraction.get("truncated"):
            file_text = f"{file_text}\n...[文件内容按 {int(extraction.get('bytes_read') or 0)} bytes 截断]..."
        if preview:
            return f"{preview}\n\n{file_text}"
        return file_text

    if preview:
        return f"{header}\n{preview}"
    return header


def _record_payload(record, *, max_file_bytes: int) -> dict:
    user = getattr(record, "user", None)
    created_at = getattr(record, "created_at", None)
    updated_at = getattr(record, "updated_at", None)
    content = getattr(record, "content", None)
    preview = _normalize_text(str(getattr(record, "preview", "") or ""))
    kind = str(getattr(content, "kind", "") or "")

    if kind == "text":
        text_value = _normalize_text(str(getattr(content, "text_content", "") or ""))
        content_payload = {
            "kind": "text",
            "text": text_value,
            "media_type": "text",
        }
        extraction = {
            "status": "inline_text",
            "text": "",
            "encoding": "",
            "truncated": False,
            "bytes_read": 0,
            "message": "",
        }
    else:
        filename = str(getattr(content, "filename", "") or "").strip() or "file"
        content_type = str(getattr(content, "content_type", "") or "").strip() or "application/octet-stream"
        content_payload = {
            "kind": "file",
            "filename": filename,
            "content_type": content_type,
            "media_type": _content_media_type(content_type=content_type, filename=filename),
            "size_bytes": int(getattr(content, "size_bytes", 0) or 0),
            "sha256": str(getattr(content, "sha256", "") or ""),
            "oss_key": str(getattr(content, "oss_key", "") or ""),
        }
        extraction = _extract_file_text(content_payload, max_bytes=max_file_bytes)

    text_value = _record_text(record, content_payload=content_payload, extraction=extraction)
    return {
        "id": int(getattr(record, "id", 0) or 0),
        "record_no": int(getattr(record, "id", 0) or 0),
        "format": str(getattr(record, "format", "") or ""),
        "visibility": str(getattr(record, "visibility", "") or "private"),
        "preview": preview,
        "tags": list(getattr(record, "get_tags", lambda: [])() or []),
        "created_at": created_at.isoformat() + "Z" if created_at else None,
        "updated_at": updated_at.isoformat() + "Z" if updated_at else None,
        "user": {
            "id": int(getattr(user, "id", 0) or int(getattr(record, "user_id", 0) or 0)),
            "username": str(getattr(user, "username", "") or ""),
        },
        "content": content_payload,
        "extraction": extraction,
        "text": text_value,
    }


def _record_cache_key_from_model(record) -> str:
    record_id = int(getattr(record, "id", 0) or 0)
    updated_at = getattr(record, "updated_at", None)
    updated = updated_at.isoformat() + "Z" if updated_at else ""
    return f"{record_id}:{updated}"


def _record_cache_key_from_row(row: dict) -> str:
    record_id = int(row.get("id") or row.get("record_no") or 0)
    updated = str(row.get("updated_at") or "")
    return f"{record_id}:{updated}"


def save_daily_archive(
    day_value: date,
    records: Sequence,
    *,
    scope: str = "public",
    source: str = "home_today",
    timezone_name: str = "UTC",
) -> dict:
    path = archive_file_path(day_value)
    max_file_bytes = max(65536, min(get_setting_int("AI_NOTICE_FILE_READ_MAX_BYTES", default=524288), 8 * 1024 * 1024))
    existing = load_archive(path) if path.exists() else {}
    existing_rows = existing.get("records") if isinstance(existing, dict) else []
    existing_lookup: dict[str, dict] = {}
    if isinstance(existing_rows, list):
        for row in existing_rows:
            if not isinstance(row, dict):
                continue
            key = _record_cache_key_from_row(row)
            if not isinstance(row.get("content"), dict) or not isinstance(row.get("extraction"), dict):
                continue
            if key and key not in existing_lookup:
                existing_lookup[key] = row

    record_rows: list[dict] = []
    for item in records:
        key = _record_cache_key_from_model(item)
        cached = existing_lookup.get(key)
        if cached:
            record_rows.append(cached)
            continue
        record_rows.append(_record_payload(item, max_file_bytes=max_file_bytes))

    payload = {
        "schema_version": 2,
        "day": day_value.isoformat(),
        "scope": str(scope or "public"),
        "source": str(source or "unknown"),
        "timezone": str(timezone_name or "UTC"),
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "record_count": len(records),
        "records": record_rows,
    }

    changed = True
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
