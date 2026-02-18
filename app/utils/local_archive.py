from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

import requests
from flask import current_app

from ..oss import get_object_bytes, get_object_to_file, sign_get_url
from .provider_config import (
    normalize_provider as normalize_ai_provider,
    provider_connection_settings,
    provider_model,
    provider_model_setting,
)
from .runtime_settings import get_setting_bool, get_setting_int, get_setting_str


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
_MODEL_PLACEHOLDERS = {"", "none", "n/a", "na", "-", "unsupported", "not-supported", "not_supported"}
_SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_TEXT_CHARS_LIMIT = 120000


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


def _objects_root() -> Path:
    path = _base_dir() / "_objects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_file_path(day_value: date) -> Path:
    return _base_dir() / f"{day_value.isoformat()}.json"


def _safe_archive_filename(filename: str) -> str:
    name = Path(str(filename or "file")).name.strip() or "file"
    safe = _SAFE_FILENAME_PATTERN.sub("_", name).strip("._") or "file"
    if len(safe) > 120:
        suffix = Path(safe).suffix
        stem = safe[: max(1, 120 - len(suffix))]
        safe = f"{stem}{suffix}"
    return safe


def _archive_blob_relative_path(*, day_value: date, record_id: int, content_sha256: str, filename: str) -> str:
    day_text = day_value.isoformat()
    digest = str(content_sha256 or "").strip().lower()[:16] or "nosha"
    safe_name = _safe_archive_filename(filename)
    rid = int(record_id or 0)
    return f"_objects/{day_text}/{rid}-{digest}-{safe_name}"


def _normalize_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _trim_text(value: str, *, limit: int) -> tuple[str, bool]:
    cleaned = _normalize_text(value)
    size = max(200, min(int(limit or 0), _MAX_TEXT_CHARS_LIMIT))
    if len(cleaned) <= size:
        return cleaned, False
    return cleaned[:size].rstrip(), True


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


def _normalize_provider(value: str) -> str:
    return normalize_ai_provider(value)


def _model_placeholder(value: str) -> bool:
    return str(value or "").strip().lower() in _MODEL_PLACEHOLDERS


def _transcribe_model_setting(provider: str) -> tuple[str, str]:
    return provider_model_setting(provider, "transcribe")


def _archive_ai_settings() -> dict | None:
    provider = _normalize_provider(get_setting_str("AI_CHAT_PROVIDER", default=""))
    if not provider:
        return None

    selected = provider_connection_settings(provider)
    if not selected:
        return None

    model = provider_model(provider, "chat")
    if _model_placeholder(model):
        return None
    return {
        "provider": str(selected.get("provider") or provider),
        "api_key": str(selected.get("api_key") or ""),
        "base_url": str(selected.get("base_url") or ""),
        "model": model,
    }


def _ai_request_json(endpoint: str, *, payload: dict, settings: dict, timeout: int) -> dict:
    url = settings["base_url"] + endpoint
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"request error: {exc}") from exc
    if not response.ok:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 320:
            detail = detail[:320] + "..."
        raise RuntimeError(f"request failed ({response.status_code}): {detail}")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("response is not json") from exc
    if not isinstance(data, dict):
        raise RuntimeError("response payload is not an object")
    return data


def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        part_type = str(item.get("type") or "").lower()
        if part_type in {"text", "output_text"}:
            chunks.append(str(item.get("text") or ""))
    return "\n".join(part for part in chunks if part).strip()


def _chat_response_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        return ""
    return _message_content_to_text(message.get("content"))


def _archive_parse_timeout() -> int:
    return max(10, min(get_setting_int("AI_ARCHIVE_PARSE_TIMEOUT_SECONDS", default=90), 1800))


def _archive_parse_max_chars() -> int:
    return max(600, min(get_setting_int("AI_ARCHIVE_PARSE_MAX_CHARS", default=8000), _MAX_TEXT_CHARS_LIMIT))


def _extract_image_text_with_ai(*, result: dict, content_payload: dict, settings: dict, local_blob_path: str = "") -> dict:
    oss_key = str(content_payload.get("oss_key") or "")

    expires = max(300, min(get_setting_int("AI_NOTICE_IMAGE_URL_EXPIRES_SECONDS", default=1800), 86400))
    image_url = sign_get_url(oss_key, expires=expires) if oss_key else ""
    if not image_url and local_blob_path and Path(local_blob_path).exists():
        try:
            raw = Path(local_blob_path).read_bytes()
        except Exception as exc:
            result["status"] = "read_failed"
            result["message"] = f"local image read failed: {exc}"
            return result
        if raw:
            ctype = str(content_payload.get("content_type") or "application/octet-stream")
            image_url = f"data:{ctype};base64,{base64.b64encode(raw).decode('ascii')}"
    if not image_url:
        result["status"] = "parse_unavailable"
        result["message"] = "signed image url unavailable"
        return result

    payload = {
        "model": settings["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是归档解析助手。请提取图片中的文字和关键信息，"
                    "输出纯文本，不要使用 markdown。若无可读文本，返回一句简短场景描述。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请输出用于归档检索的文本摘要（中文，尽量客观）。"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 900,
    }

    data = _ai_request_json("/chat/completions", payload=payload, settings=settings, timeout=_archive_parse_timeout())
    extracted = _chat_response_text(data)
    if not extracted:
        result["status"] = "parse_failed"
        result["message"] = "image parse returned empty text"
        return result

    text, truncated = _trim_text(extracted, limit=_archive_parse_max_chars())
    if not text:
        result["status"] = "parse_failed"
        result["message"] = "image parse returned empty text"
        return result

    result["status"] = "ok_ai_image"
    result["text"] = text
    result["encoding"] = f"ai:{settings['provider']}:{settings['model']}"
    result["truncated"] = bool(truncated)
    result["bytes_read"] = int(content_payload.get("size_bytes") or result.get("bytes_read") or 0)
    result["message"] = "parsed by vision chat"
    return result


def _extract_transcription_text(
    *,
    result: dict,
    content_payload: dict,
    settings: dict,
    local_blob_path: Path,
) -> dict:
    provider = str(settings.get("provider") or "").strip().lower()
    model_key, model_default = _transcribe_model_setting(provider)
    model = get_setting_str(model_key, default=model_default).strip() if model_key else ""
    if _model_placeholder(model):
        result["status"] = "parse_unavailable"
        result["message"] = f"transcribe model unavailable for provider={provider}"
        return result

    filename = str(content_payload.get("filename") or local_blob_path.name or "file")
    content_type = str(content_payload.get("content_type") or "application/octet-stream")
    endpoint = settings["base_url"] + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {settings['api_key']}"}

    try:
        with local_blob_path.open("rb") as fp:
            response = requests.post(
                endpoint,
                headers=headers,
                data={"model": model},
                files={"file": (filename, fp, content_type)},
                timeout=_archive_parse_timeout(),
            )
    except requests.RequestException as exc:
        result["status"] = "parse_failed"
        result["message"] = f"transcription request error: {exc}"
        return result

    if not response.ok:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 320:
            detail = detail[:320] + "..."
        result["status"] = "parse_failed"
        result["message"] = f"transcription failed ({response.status_code}): {detail}"
        return result

    text = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            text = str(data.get("text") or "").strip()
            if not text and isinstance(data.get("segments"), list):
                text = "\n".join(str(item.get("text") or "").strip() for item in data["segments"] if isinstance(item, dict))
    except Exception:
        text = response.text.strip()

    if not text:
        result["status"] = "parse_failed"
        result["message"] = "transcription returned empty text"
        return result

    normalized, truncated = _trim_text(text, limit=_archive_parse_max_chars())
    if not normalized:
        result["status"] = "parse_failed"
        result["message"] = "transcription returned empty text"
        return result

    result["status"] = "ok_ai_transcribe"
    result["text"] = normalized
    result["encoding"] = f"ai:{settings['provider']}:{model}"
    result["truncated"] = bool(truncated)
    result["message"] = "parsed by audio transcription"
    return result


def _extract_non_text_file(
    *,
    result: dict,
    content_payload: dict,
    local_blob_path: str,
    max_bytes: int,
) -> dict:
    if not get_setting_bool("AI_ARCHIVE_MULTIMODAL_PARSE", default=True):
        result["status"] = "skipped_non_text"
        result["message"] = "multimodal parse disabled"
        return result

    settings = _archive_ai_settings()
    if not settings:
        result["status"] = "skipped_non_text"
        result["message"] = "ai provider not configured"
        return result

    media_type = str(content_payload.get("media_type") or "file").strip().lower()
    if media_type == "image":
        try:
            return _extract_image_text_with_ai(
                result=result,
                content_payload=content_payload,
                settings=settings,
                local_blob_path=local_blob_path,
            )
        except RuntimeError as exc:
            result["status"] = "parse_failed"
            result["message"] = str(exc)
            return result

    if media_type in {"audio", "video"}:
        temp_path: Path | None = None
        target = Path(local_blob_path).resolve() if local_blob_path else None
        if not target or not target.exists():
            oss_key = str(content_payload.get("oss_key") or "")
            if not oss_key:
                result["status"] = "missing_oss_key"
                result["message"] = "file key unavailable"
                return result
            suffix = Path(str(content_payload.get("filename") or "")).suffix
            with tempfile.NamedTemporaryFile(prefix="benoss-archive-media-", suffix=suffix, delete=False) as tmp:
                temp_path = Path(tmp.name)
            try:
                get_object_to_file(oss_key, str(temp_path), max_bytes=max_bytes if max_bytes > 0 else None)
            except Exception as exc:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                result["status"] = "read_failed"
                result["message"] = f"read failed: {exc}"
                return result
            target = temp_path

        result["bytes_read"] = int(content_payload.get("size_bytes") or 0)
        try:
            output = _extract_transcription_text(
                result=result,
                content_payload=content_payload,
                settings=settings,
                local_blob_path=target,
            )
        finally:
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
        return output

    result["status"] = "skipped_non_text"
    result["message"] = f"unsupported media type: {media_type}"
    return result


def _extract_file_text(content_payload: dict, *, max_bytes: int, local_blob_path: str = "") -> dict:
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

    if not oss_key and not local_blob_path:
        result["status"] = "missing_oss_key"
        result["message"] = "file key unavailable"
        return result

    if not _is_text_like_file(content_type=content_type, filename=filename, media_type=media_type):
        return _extract_non_text_file(
            result=result,
            content_payload=content_payload,
            local_blob_path=local_blob_path,
            max_bytes=max_bytes,
        )

    try:
        if local_blob_path and Path(local_blob_path).exists():
            with Path(local_blob_path).open("rb") as fp:
                raw = fp.read(max_bytes if max_bytes > 0 else -1)
        else:
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _archive_file_blob(*, day_value: date, record, content_payload: dict) -> dict:
    info = {
        "saved": False,
        "relative_path": "",
        "absolute_path": "",
        "size_bytes": 0,
        "sha256": str(content_payload.get("sha256") or ""),
        "message": "",
    }
    if not get_setting_bool("ARCHIVE_STORE_FILE_BLOB", default=True):
        info["message"] = "archive file blob disabled"
        return info

    oss_key = str(content_payload.get("oss_key") or "")
    if not oss_key:
        info["message"] = "file key unavailable"
        return info

    record_id = int(getattr(record, "id", 0) or 0)
    filename = str(content_payload.get("filename") or "file")
    content_sha = str(content_payload.get("sha256") or "").strip().lower()
    relative_path = _archive_blob_relative_path(
        day_value=day_value,
        record_id=record_id,
        content_sha256=content_sha,
        filename=filename,
    )
    absolute_path = (_base_dir() / relative_path).resolve()
    absolute_path.parent.mkdir(parents=True, exist_ok=True)

    if absolute_path.exists() and absolute_path.stat().st_size > 0:
        info["saved"] = True
        info["relative_path"] = relative_path
        info["absolute_path"] = str(absolute_path)
        info["size_bytes"] = int(absolute_path.stat().st_size)
        if not info["sha256"]:
            info["sha256"] = _file_sha256(absolute_path)
        return info

    try:
        size = get_object_to_file(oss_key, str(absolute_path))
    except Exception as exc:
        info["message"] = f"blob archive failed: {exc}"
        try:
            absolute_path.unlink(missing_ok=True)
        except Exception:
            pass
        return info

    if size <= 0:
        info["message"] = "blob archive failed: empty data"
        try:
            absolute_path.unlink(missing_ok=True)
        except Exception:
            pass
        return info

    info["saved"] = True
    info["relative_path"] = relative_path
    info["absolute_path"] = str(absolute_path)
    info["size_bytes"] = int(size)
    if not info["sha256"]:
        info["sha256"] = _file_sha256(absolute_path)
    return info


def _read_text_content(content) -> str:
    fallback = _normalize_text(str(getattr(content, "text_content", "") or ""))
    oss_key = str(getattr(content, "oss_key", "") or "").strip()
    if not oss_key:
        return fallback

    try:
        raw = get_object_bytes(oss_key)
    except Exception:
        return fallback
    if not raw:
        return fallback

    decoded, _ = _decode_text_bytes(raw)
    if not decoded:
        decoded = raw.decode("utf-8", errors="replace")
    text = _normalize_text(decoded)
    return text or fallback


def _record_text(record, *, content_payload: dict, extraction: dict) -> str:
    content = getattr(record, "content", None)
    if not content:
        return ""

    if getattr(content, "kind", "") == "text":
        text = _read_text_content(content)
        return text or _normalize_text(str(getattr(record, "preview", "") or ""))

    filename = str(content_payload.get("filename") or "file")
    content_type = str(content_payload.get("content_type") or "application/octet-stream")
    preview = _normalize_text(str(getattr(record, "preview", "") or ""))
    header = f"[FILE] {filename} ({content_type})"

    extracted_text = _normalize_text(str(extraction.get("text") or ""))
    if extracted_text:
        encoding = str(extraction.get("encoding") or "").strip()
        if encoding.startswith("ai:"):
            title = f"[FILE-AI {encoding}]"
        elif encoding:
            title = f"[FILE-TEXT encoding={encoding}]"
        else:
            title = "[FILE-TEXT]"
        file_text = f"{title}\n{extracted_text}"
        if extraction.get("truncated"):
            file_text = f"{file_text}\n...[文件内容按 {int(extraction.get('bytes_read') or 0)} bytes/字符截断]..."
        if preview:
            return f"{preview}\n\n{file_text}"
        return file_text

    if preview:
        return f"{header}\n{preview}"
    return header


def _record_payload(record, *, max_file_bytes: int, day_value: date) -> dict:
    user = getattr(record, "user", None)
    created_at = getattr(record, "created_at", None)
    updated_at = getattr(record, "updated_at", None)
    content = getattr(record, "content", None)
    preview = _normalize_text(str(getattr(record, "preview", "") or ""))
    kind = str(getattr(content, "kind", "") or "")

    if kind == "text":
        text_value = _read_text_content(content)
        content_payload = {
            "kind": "text",
            "text": text_value,
            "media_type": "text",
        }
        oss_key = str(getattr(content, "oss_key", "") or "").strip()
        if oss_key:
            content_payload["oss_key"] = oss_key
            content_payload["content_type"] = str(getattr(content, "content_type", "") or "text/plain; charset=utf-8")
            content_payload["size_bytes"] = int(getattr(content, "size_bytes", 0) or 0)
            content_payload["sha256"] = str(getattr(content, "sha256", "") or "")
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
        blob_info = _archive_file_blob(day_value=day_value, record=record, content_payload=content_payload)
        if blob_info.get("saved"):
            content_payload["archive_blob_relpath"] = str(blob_info.get("relative_path") or "")
            content_payload["archive_blob_size_bytes"] = int(blob_info.get("size_bytes") or 0)
            if blob_info.get("sha256"):
                content_payload["archive_blob_sha256"] = str(blob_info.get("sha256") or "")
        if blob_info.get("message"):
            content_payload["archive_blob_message"] = str(blob_info.get("message") or "")
        extraction = _extract_file_text(
            content_payload,
            max_bytes=max_file_bytes,
            local_blob_path=str(blob_info.get("absolute_path") or ""),
        )

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


def _retention_days() -> int:
    default_days = int(current_app.config.get("ARCHIVE_RETENTION_DAYS") or 7)
    return max(0, min(get_setting_int("ARCHIVE_RETENTION_DAYS", default=default_days), 3650))


def _retention_cutoff_day(retention_days: int) -> date | None:
    if retention_days <= 0:
        return None
    timezone_name = get_setting_str(
        "DIGEST_TIMEZONE",
        default=str(current_app.config.get("DIGEST_TIMEZONE") or "Asia/Shanghai"),
    ).strip() or "Asia/Shanghai"
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
    today = datetime.now(tz).date()
    return today - timedelta(days=max(0, retention_days - 1))


def apply_archive_retention_policy() -> dict:
    root = _base_dir()
    objects_root = _objects_root()
    retention_days = _retention_days()
    cutoff_day = _retention_cutoff_day(retention_days)
    result = {
        "enabled": retention_days > 0,
        "retention_days": retention_days,
        "cutoff_day": cutoff_day.isoformat() if cutoff_day else "",
        "deleted_count": 0,
        "deleted_days": [],
        "errors": [],
    }
    if not cutoff_day:
        return result

    deleted_days: list[str] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name):
        day_text = path.stem
        if not _DAY_PATTERN.match(day_text):
            continue
        try:
            day_value = date.fromisoformat(day_text)
        except Exception:
            continue
        if day_value >= cutoff_day:
            continue

        try:
            path.unlink(missing_ok=True)
            deleted_days.append(day_text)
        except Exception as exc:
            result["errors"].append(f"{day_text}: {exc}")
            continue

        blob_day_dir = objects_root / day_text
        if blob_day_dir.exists():
            try:
                shutil.rmtree(blob_day_dir)
            except Exception as exc:
                result["errors"].append(f"{day_text} blobs: {exc}")

    # 清理已无对应 json 的孤立文件目录，避免长期占用磁盘。
    for blob_day_dir in sorted(objects_root.glob("*"), key=lambda item: item.name):
        if not blob_day_dir.is_dir():
            continue
        day_text = blob_day_dir.name
        if not _DAY_PATTERN.match(day_text):
            continue
        try:
            day_value = date.fromisoformat(day_text)
        except Exception:
            continue
        if day_value >= cutoff_day:
            continue
        if (root / f"{day_text}.json").exists():
            continue
        try:
            shutil.rmtree(blob_day_dir)
            if day_text not in deleted_days:
                deleted_days.append(day_text)
        except Exception as exc:
            result["errors"].append(f"{day_text} orphan blobs: {exc}")

    result["deleted_days"] = sorted(set(deleted_days))
    result["deleted_count"] = len(result["deleted_days"])
    return result


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
            extraction_status = str((row.get("extraction") or {}).get("status") or "").strip().lower()
            if extraction_status in {"read_failed", "parse_failed", "decode_failed", "empty_text"}:
                continue
            content_row = row.get("content") or {}
            if (
                get_setting_bool("ARCHIVE_STORE_FILE_BLOB", default=True)
                and str(content_row.get("kind") or "") == "file"
                and not str(content_row.get("archive_blob_relpath") or "").strip()
            ):
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
        record_rows.append(_record_payload(item, max_file_bytes=max_file_bytes, day_value=day_value))

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

    retention = apply_archive_retention_policy()
    return {
        "path": str(path),
        "day": payload["day"],
        "scope": payload["scope"],
        "record_count": payload["record_count"],
        "updated_at": payload["updated_at"],
        "changed": changed,
        "retention": retention,
    }


def list_archive_files() -> list[Path]:
    root = _base_dir()
    apply_archive_retention_policy()
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
