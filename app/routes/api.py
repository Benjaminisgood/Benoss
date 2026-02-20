from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, Response, current_app, g, jsonify, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, func, not_, or_
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Comment, Content, DailyDigestJob, GeneratedAsset, Record, User
from ..oss import (
    copy_object,
    delete_object,
    get_object_bytes,
    has_remote_backend,
    object_exists,
    put_object_bytes,
    put_object_from_file,
    sign_get_url,
    sign_put_url,
)
from ..utils.ids import new_uuid
from ..utils.local_archive import apply_archive_retention_policy, archive_file_path, load_archive, save_daily_archive
from ..utils.local_vector_db import (
    build_chat_context,
    build_index,
    ensure_index,
    index_meta,
    search as vector_search,
)
from ..utils.oss_paths import generated_asset_key, record_content_key
from ..utils.provider_config import (
    normalize_provider as normalize_ai_provider,
    provider_connection_settings,
    provider_model,
    provider_model_setting,
)
from ..utils.runtime_settings import (
    DEFAULT_NOTICE_BLOG_TASK,
    DEFAULT_NOTICE_PODCAST_TASK,
    DEFAULT_NOTICE_POSTER_TASK,
    DEFAULT_NOTICE_SYSTEM_PROMPT,
    DEFAULT_POSTER_SYSTEM_PROMPT,
    DEFAULT_POSTER_USER_TEMPLATE,
    DEFAULT_VECTOR_CHAT_SYSTEM_PROMPT,
    format_prompt_template,
    get_setting_bool,
    get_setting_int,
    get_setting_str,
)
from ..utils.session_auth import login_required


api_bp = Blueprint("api", __name__)

_VALID_VISIBILITY = {"public", "private"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AUTO_TAG_PATTERN = re.compile(r"(?:^|[^0-9A-Za-z_:/#])#([0-9A-Za-z_\-\u3400-\u9fff]{1,40})")
_URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>'\"`]+", re.IGNORECASE)
_URL_TRAILING_CHARS = ".,;:!?)]}，。；：！？）】》、"
_MARKDOWN_CODE_FENCE_RE = re.compile(r"^\s*```(?:[a-zA-Z0-9_-]+)?\s*([\s\S]*?)\s*```\s*$", re.IGNORECASE)
_MARKDOWN_CODE_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*([\s\S]*?)```", re.IGNORECASE)
_DIGEST_MEDIA_APPENDIX_RE = re.compile(
    r"<section\s+class=[\"']benoss-media-appendix[\"'][\s\S]*?</section>",
    re.IGNORECASE,
)
_BLOG_SLOT_MARKER_RE = re.compile(r"<!--\s*BENOSS_SLOT:([a-z0-9][a-z0-9_-]{0,63})\s*-->", re.IGNORECASE)
_BLOG_MEDIA_SIGNED_URL_EXPIRES_SECONDS = 1800
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIRECT_UPLOAD_TOKEN_SALT = "benoss.direct-upload.v1"
_PODCAST_STYLE_GUIDE = {
    "dialogue": "对话式：双人主持人口吻，包含主持人A/主持人B轮流发言，节奏自然。",
    "speech": "演讲式：单人演讲结构，逻辑清晰，重点突出。",
    "interview": "访谈式：主持人提问、嘉宾回答，问题与观点递进。",
    "news": "播报式：新闻播报口吻，先摘要后分点，语言准确克制。",
}
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
_WEB_FILE_EXTENSIONS = (".html", ".htm", ".xhtml")
_WEB_FILE_MIME_TYPES = {"text/html", "application/xhtml+xml"}
_IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
_VIDEO_FILE_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi")
_AUDIO_FILE_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
_LOG_FILE_EXTENSIONS = (".log", ".out", ".err")
_DATABASE_FILE_EXTENSIONS = (".db", ".sqlite", ".sqlite3", ".db3")
_DATABASE_FILE_MIME_TYPES = {"application/x-sqlite3", "application/vnd.sqlite3"}
_ARCHIVE_FILE_EXTENSIONS = (
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".tbz",
    ".tbz2",
    ".xz",
    ".txz",
    ".7z",
    ".rar",
)
_ARCHIVE_FILE_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/x-7z-compressed",
    "application/vnd.rar",
    "application/x-rar-compressed",
    "application/x-bzip2",
    "application/x-xz",
}
_DOCUMENT_FILE_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".odt",
    ".ods",
    ".odp",
)
_DOCUMENT_FILE_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
}
_ECHO_FILE_TYPES = {"text", "web", "image", "video", "audio", "log", "database", "archive", "document", "file"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _preview_text(value: str, *, limit: int = 220) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _human_size(size_bytes: int | None) -> str:
    value = int(size_bytes or 0)
    if value <= 0:
        return ""

    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        size /= 1024
    return f"{value} B"


def _notice_file_meta_html(payload: dict) -> str:
    parts: list[str] = []
    content_type = str(payload.get("content_type") or "").strip()
    size_text = _human_size(payload.get("size_bytes"))
    if content_type:
        parts.append(html.escape(content_type))
    if size_text:
        parts.append(size_text)
    if not parts:
        return ""
    return f"<p class=\"notice-file-meta\">{' | '.join(parts)}</p>"


def _notice_tag_link(tag_text: str, *, class_name: str = "tag-pill notice-tag-link") -> str:
    text = str(tag_text or "").strip()
    if not text:
        return ""
    href = url_for("site.notice", tag=text)
    escaped_href = html.escape(href, quote=True)
    escaped_tag = html.escape(text)
    escaped_tag_attr = html.escape(text, quote=True)
    escaped_class = html.escape(class_name, quote=True)
    return (
        f"<a class=\"{escaped_class}\" href=\"{escaped_href}\" data-notice-tag=\"{escaped_tag_attr}\">"
        f"#{escaped_tag}</a>"
    )


def _parse_tags(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw or "")
        items = [part.strip() for part in text.split(",")]

    result: list[str] = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 40:
            text = text[:40]
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(text)
        if len(result) >= 20:
            break
    return result


def _auto_tags_from_text(raw: str) -> list[str]:
    text = str(raw or "")
    if not text:
        return []
    items: list[str] = []
    for match in _AUTO_TAG_PATTERN.finditer(text):
        tag = str(match.group(1) or "").strip()
        if not tag:
            continue
        items.append(tag)
        if len(items) >= 60:
            break
    return _parse_tags(items)


def _read_text_content(content: Content | None, *, max_bytes: int | None = None) -> str:
    if not content:
        return ""
    fallback = str(content.text_content or "")
    if not content.oss_key:
        return fallback
    try:
        data = get_object_bytes(content.oss_key, max_bytes=max_bytes)
    except Exception:
        return fallback
    if not data:
        return fallback
    decoded, _ = _decode_text_bytes(data)
    if decoded:
        return decoded
    return data.decode("utf-8", errors="replace") or fallback


def _trim_link_suffix(raw_url: str) -> tuple[str, str]:
    core = str(raw_url or "")
    suffix = ""
    while core and core[-1] in _URL_TRAILING_CHARS:
        last_char = core[-1]
        if last_char == ")" and core.count("(") >= core.count(")"):
            break
        core = core[:-1]
        suffix = last_char + suffix
    return core, suffix


def _linkify_text_html(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""

    parts: list[str] = []
    cursor = 0
    for match in _URL_PATTERN.finditer(text):
        start, end = match.span()
        if start > cursor:
            parts.append(html.escape(text[cursor:start]))

        matched = match.group(0)
        clean_url, suffix = _trim_link_suffix(matched)
        if not clean_url:
            parts.append(html.escape(matched))
            cursor = end
            continue

        href = clean_url if clean_url.lower().startswith(("http://", "https://")) else f"https://{clean_url}"
        parts.append(
            f"<a href=\"{html.escape(href, quote=True)}\" target=\"_blank\" rel=\"noreferrer noopener\">"
            f"{html.escape(clean_url)}</a>"
        )
        if suffix:
            parts.append(html.escape(suffix))
        cursor = end

    if cursor < len(text):
        parts.append(html.escape(text[cursor:]))
    return "".join(parts)


def _text_to_content(text_value: str) -> Content:
    text = str(text_value or "")
    data = text.encode("utf-8")
    object_id = new_uuid()
    oss_key = record_content_key(object_id, "text.txt")
    put_object_bytes(oss_key, data, content_type="text/plain; charset=utf-8")
    return Content(
        kind="text",
        file_type="text",
        text_content=_preview_text(text, limit=2000),
        oss_key=oss_key,
        filename="text.txt",
        content_type="text/plain; charset=utf-8",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _normalize_visibility(raw, *, default: str = "private") -> str:
    value = str(raw or "").strip().lower() or default
    if value not in _VALID_VISIBILITY:
        return default
    return value


def _parse_day(day_str: str | None) -> date | None:
    value = str(day_str or "").strip()
    if not value:
        return None
    if not _DATE_PATTERN.match(value):
        raise ValueError("invalid day format")
    return datetime.strptime(value, "%Y-%m-%d").date()


def _day_bounds(day_value: date) -> tuple[datetime, datetime]:
    start = datetime(day_value.year, day_value.month, day_value.day)
    end = start + timedelta(days=1)
    return start, end


def _digest_timezone() -> str:
    raw = get_setting_str("DIGEST_TIMEZONE", default="Asia/Shanghai").strip()
    return raw or "Asia/Shanghai"


def _utc_bounds_for_local_day(day_value: date, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc

    local_start = datetime(day_value.year, day_value.month, day_value.day, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_start, utc_end


def _digest_owner_user() -> User:
    admin = User.query.filter(User.role == "admin", User.is_active.is_(True)).order_by(User.id.asc()).first()
    if admin:
        return admin
    fallback = User.query.order_by(User.id.asc()).first()
    if fallback:
        return fallback
    raise RuntimeError("no user found for digest assets")


def _visible_filter(user_id: int, *, public_only: bool = False):
    if public_only:
        return Record.visibility == "public"
    return or_(Record.visibility == "public", Record.user_id == user_id)


def _is_record_visible(record: Record, user: User) -> bool:
    if record.visibility == "public":
        return True
    return bool(user and record.user_id == user.id)


def _is_generated_asset_visible(asset: GeneratedAsset, user: User) -> bool:
    if asset.visibility == "public":
        return True
    return bool(user and asset.user_id == user.id)


def _can_manage_generated_asset(asset: GeneratedAsset, user: User) -> bool:
    if not user:
        return False
    if asset.visibility == "public":
        return True
    return asset.user_id == user.id


def _normalized_file_meta(content_type: str | None, filename_or_ext: str | None) -> tuple[str, str, str]:
    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    name = str(filename_or_ext or "").strip().lower()
    suffix = Path(name).suffix.lower() if name else ""
    return ctype, name, suffix


def _detect_file_type(content_type: str | None, filename_or_ext: str | None) -> str:
    ctype, name, suffix = _normalized_file_meta(content_type, filename_or_ext)

    if ctype.startswith("image/") or suffix in _IMAGE_FILE_EXTENSIONS:
        return "image"
    if ctype.startswith("video/") or suffix in _VIDEO_FILE_EXTENSIONS:
        return "video"
    if ctype.startswith("audio/") or suffix in _AUDIO_FILE_EXTENSIONS:
        return "audio"
    if (
        ctype in _WEB_FILE_MIME_TYPES
        or ctype.startswith("text/html")
        or ctype.startswith("application/xhtml+xml")
        or suffix in _WEB_FILE_EXTENSIONS
        or name.endswith(_WEB_FILE_EXTENSIONS)
    ):
        return "web"
    if suffix in _LOG_FILE_EXTENSIONS:
        return "log"
    if ctype in _DATABASE_FILE_MIME_TYPES or suffix in _DATABASE_FILE_EXTENSIONS:
        return "database"
    if ctype in _ARCHIVE_FILE_MIME_TYPES or suffix in _ARCHIVE_FILE_EXTENSIONS:
        return "archive"
    if ctype in _DOCUMENT_FILE_MIME_TYPES or suffix in _DOCUMENT_FILE_EXTENSIONS:
        return "document"
    if ctype.startswith("text/") or ctype in _TEXT_FILE_MIME_TYPES or suffix in _TEXT_FILE_EXTENSIONS:
        return "text"
    return "file"


def _normalize_stored_file_type(raw: str | None, *, fallback: str = "file") -> str:
    value = str(raw or "").strip().lower()
    if value in _ECHO_FILE_TYPES:
        return value
    return fallback


def _detect_content_file_type(content: Content | None) -> str:
    if not content:
        return "file"
    if str(content.kind or "").strip().lower() == "text":
        return "text"
    return _detect_file_type(content.content_type, content.filename)


def _detect_asset_file_type_from_values(kind: str | None, content_type: str | None, ext: str | None) -> str:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "blog_html":
        return "web"
    return _detect_file_type(content_type, ext)


def _detect_asset_file_type(asset: GeneratedAsset | None) -> str:
    if not asset:
        return "file"
    return _detect_asset_file_type_from_values(asset.kind, asset.content_type, asset.ext)


def _rechecked_content_file_type(content: Content | None) -> str:
    if not content:
        return "file"
    detected = _detect_content_file_type(content)
    stored = _normalize_stored_file_type(getattr(content, "file_type", ""), fallback="")
    return detected if stored != detected else stored


def _rechecked_asset_file_type(asset: GeneratedAsset | None) -> str:
    if not asset:
        return "file"
    detected = _detect_asset_file_type(asset)
    stored = _normalize_stored_file_type(getattr(asset, "file_type", ""), fallback="")
    return detected if stored != detected else stored


def _content_media_type(content: Content) -> str:
    inferred = _rechecked_content_file_type(content)
    if inferred in {"image", "video", "audio", "text"}:
        return inferred
    if inferred in {"web", "log"}:
        # HTML/log keep file rendering but are still text-like for extraction.
        return "text"
    return "file"


def _normalize_echo_file_type(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"", "all"}:
        return ""
    if value not in _ECHO_FILE_TYPES:
        raise ValueError("invalid file_type")
    return value


def _normalize_content_text_source(raw: str | None, *, default: str = "summary") -> str:
    value = str(raw or "").strip().lower()
    if value in {"db", "summary", "preview", "db_preview"}:
        return "db_preview"
    if value in {"full", "oss"}:
        return "oss"
    fallback = str(default or "summary").strip().lower()
    return "oss" if fallback in {"full", "oss"} else "db_preview"


def _normalize_signed_url_expires(raw, *, default: int = 300) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    # Keep short-lived links when enabled.
    return min(max(value, 60), 3600)


def _truthy(raw) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _direct_upload_enabled() -> bool:
    return _truthy(current_app.config.get("OSS_DIRECT_UPLOAD_ENABLED", "1"))


def _direct_upload_expires_seconds() -> int:
    try:
        value = int(current_app.config.get("OSS_DIRECT_UPLOAD_EXPIRES_SECONDS") or 900)
    except (TypeError, ValueError):
        value = 900
    return min(max(value, 60), 3600)


def _direct_upload_token_max_age_seconds() -> int:
    try:
        value = int(current_app.config.get("OSS_DIRECT_UPLOAD_TOKEN_MAX_AGE_SECONDS") or 1800)
    except (TypeError, ValueError):
        value = 1800
    return min(max(value, 120), 24 * 3600)


def _normalize_upload_filename(raw_filename: str | None, *, allow_empty: bool = False) -> str:
    filename = secure_filename(str(raw_filename or "").strip())
    if filename:
        return filename
    if allow_empty:
        return f"upload-{new_uuid()}"
    raise ValueError("invalid filename")


def _normalize_upload_content_type(content_type: str | None, filename: str) -> str:
    guessed = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    normalized = str(content_type or "").split(";", 1)[0].strip().lower() or guessed
    normalized = normalized[:255]
    return normalized or "application/octet-stream"


def _normalize_upload_sha256(raw_sha256: str | None) -> str:
    value = str(raw_sha256 or "").strip().lower()
    if not value:
        return ""
    if not _SHA256_RE.match(value):
        raise ValueError("invalid sha256")
    return value


def _normalize_upload_size_bytes(raw_size, *, allow_empty: bool = True) -> int:
    if raw_size in (None, ""):
        if allow_empty:
            return 0
        raise ValueError("size_bytes is required")
    try:
        size_bytes = int(raw_size)
    except (TypeError, ValueError):
        raise ValueError("invalid size_bytes") from None
    if size_bytes < 0:
        raise ValueError("invalid size_bytes")
    max_content_length = int(current_app.config.get("MAX_CONTENT_LENGTH") or 0)
    if max_content_length > 0 and size_bytes > max_content_length:
        raise ValueError("file too large")
    return size_bytes


def _direct_upload_serializer() -> URLSafeTimedSerializer:
    secret = str(current_app.config.get("SECRET_KEY") or "")
    return URLSafeTimedSerializer(secret, salt=_DIRECT_UPLOAD_TOKEN_SALT)


def _issue_direct_upload_token(
    *,
    user_id: int,
    oss_key: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str = "",
) -> str:
    payload = {
        "v": 1,
        "user_id": int(user_id),
        "oss_key": str(oss_key or "").strip(),
        "filename": str(filename or "").strip(),
        "content_type": str(content_type or "").strip(),
        "size_bytes": int(size_bytes or 0),
    }
    if sha256:
        payload["sha256"] = sha256
    return _direct_upload_serializer().dumps(payload)


def _parse_direct_upload_token(token: str, *, user_id: int) -> dict:
    max_age = _direct_upload_token_max_age_seconds()
    try:
        payload = _direct_upload_serializer().loads(str(token or "").strip(), max_age=max_age)
    except SignatureExpired:
        raise ValueError("uploaded_file_token expired") from None
    except BadSignature:
        raise ValueError("invalid uploaded_file_token") from None

    if not isinstance(payload, dict):
        raise ValueError("invalid uploaded_file_token")
    if int(payload.get("v") or 0) != 1:
        raise ValueError("invalid uploaded_file_token")
    if int(payload.get("user_id") or 0) != int(user_id):
        raise ValueError("uploaded_file_token does not belong to current user")

    oss_key = str(payload.get("oss_key") or "").strip()
    try:
        filename = _normalize_upload_filename(str(payload.get("filename") or ""))
    except ValueError:
        raise ValueError("invalid uploaded_file_token") from None
    content_type = _normalize_upload_content_type(str(payload.get("content_type") or ""), filename)
    size_bytes = _normalize_upload_size_bytes(payload.get("size_bytes"), allow_empty=True)
    sha256 = _normalize_upload_sha256(payload.get("sha256") or "")
    if not oss_key:
        raise ValueError("invalid uploaded_file_token")
    return {
        "oss_key": oss_key,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


def _content_from_uploaded_file_token(token: str, *, user_id: int) -> tuple[Content, str]:
    parsed = _parse_direct_upload_token(token, user_id=user_id)
    oss_key = str(parsed["oss_key"])
    if not object_exists(oss_key):
        raise ValueError("uploaded file not found, please retry upload")

    filename = str(parsed["filename"])
    content_type = str(parsed["content_type"])
    content = Content(
        kind="file",
        file_type=_detect_file_type(content_type, filename),
        filename=filename,
        content_type=content_type,
        size_bytes=int(parsed["size_bytes"] or 0),
        sha256=str(parsed["sha256"] or ""),
        oss_key=oss_key,
    )
    return content, filename


def _request_uploaded_file_tokens(payload: dict) -> list[str]:
    raw_values: list[object] = []
    if request.form:
        raw_values.extend(request.form.getlist("uploaded_file_token"))
        raw_values.extend(request.form.getlist("uploaded_file_token[]"))

    raw_values.append(payload.get("uploaded_file_token"))
    list_value = payload.get("uploaded_file_tokens")
    if isinstance(list_value, list):
        raw_values.extend(list_value)
    elif isinstance(list_value, str):
        text = list_value.strip()
        if text:
            try:
                parsed = json.loads(text)
            except Exception:
                raw_values.extend([part.strip() for part in text.split(",") if part.strip()])
            else:
                if isinstance(parsed, list):
                    raw_values.extend(parsed)
                elif parsed:
                    raw_values.append(parsed)

    tokens: list[str] = []
    seen = set()
    for value in raw_values:
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _request_single_uploaded_file_token(payload: dict) -> str:
    tokens = _request_uploaded_file_tokens(payload)
    if not tokens:
        return ""
    if len(tokens) > 1:
        raise ValueError("only one uploaded_file_token is allowed")
    return tokens[0]


def _signed_url_if_enabled(oss_key: str | None, *, enabled: bool, expires: int) -> str:
    key = str(oss_key or "").strip()
    if not enabled or not key:
        return ""
    try:
        return sign_get_url(key, expires=expires) or ""
    except Exception:
        return ""


def _parse_iso_datetime(raw: str) -> datetime:
    text = str(raw or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(tzinfo=None)


def _suffix_like_filter(column, suffixes: tuple[str, ...]):
    clauses = [column.like(f"%{suffix}") for suffix in suffixes]
    if not clauses:
        return column == "__never_match__"
    return or_(*clauses)


def _mime_match_filter(column, mime_values: tuple[str, ...]):
    clauses = [column.in_(mime_values)]
    clauses.extend(column.like(f"{value};%") for value in mime_values)
    if not clauses:
        return column == "__never_match__"
    return or_(*clauses)


def _value_in_filter(column, values: tuple[str, ...]):
    if not values:
        return column == "__never_match__"
    return column.in_(values)


def _sql_file_type_matchers(*, ctype, filename_or_ext, filename_mode: str, include_blog_html_kind=None):
    def filename_match(values: tuple[str, ...]):
        if filename_mode == "suffix":
            return _suffix_like_filter(filename_or_ext, values)
        return _value_in_filter(filename_or_ext, values)

    text_mime_values = tuple(sorted(_TEXT_FILE_MIME_TYPES))
    text_suffix_values = tuple(sorted(_TEXT_FILE_EXTENSIONS))
    web_mime_values = tuple(sorted(_WEB_FILE_MIME_TYPES))
    database_mime_values = tuple(sorted(_DATABASE_FILE_MIME_TYPES))
    archive_mime_values = tuple(sorted(_ARCHIVE_FILE_MIME_TYPES))
    document_mime_values = tuple(sorted(_DOCUMENT_FILE_MIME_TYPES))

    image_match = or_(ctype.like("image/%"), filename_match(_IMAGE_FILE_EXTENSIONS))
    video_match = or_(ctype.like("video/%"), filename_match(_VIDEO_FILE_EXTENSIONS))
    audio_match = or_(ctype.like("audio/%"), filename_match(_AUDIO_FILE_EXTENSIONS))
    web_match = or_(
        _mime_match_filter(ctype, web_mime_values),
        ctype.like("text/html%"),
        ctype.like("application/xhtml+xml%"),
        filename_match(_WEB_FILE_EXTENSIONS),
    )
    if include_blog_html_kind is not None:
        web_match = or_(include_blog_html_kind == "blog_html", web_match)
    log_match = filename_match(_LOG_FILE_EXTENSIONS)
    database_match = or_(
        _mime_match_filter(ctype, database_mime_values),
        filename_match(_DATABASE_FILE_EXTENSIONS),
    )
    archive_match = or_(
        _mime_match_filter(ctype, archive_mime_values),
        filename_match(_ARCHIVE_FILE_EXTENSIONS),
    )
    document_match = or_(
        _mime_match_filter(ctype, document_mime_values),
        filename_match(_DOCUMENT_FILE_EXTENSIONS),
    )
    text_match = and_(
        or_(
            ctype.like("text/%"),
            _mime_match_filter(ctype, text_mime_values),
            filename_match(text_suffix_values),
        ),
        not_(web_match),
    )

    known_match = or_(
        image_match,
        video_match,
        audio_match,
        web_match,
        log_match,
        database_match,
        archive_match,
        document_match,
        text_match,
    )
    return {
        "image": image_match,
        "video": video_match,
        "audio": audio_match,
        "web": web_match,
        "log": log_match,
        "database": database_match,
        "archive": archive_match,
        "document": document_match,
        "text": text_match,
        "known": known_match,
    }


def _legacy_record_file_type_filter(file_type: str):
    ctype = func.lower(func.coalesce(Content.content_type, ""))
    filename = func.lower(func.coalesce(Content.filename, ""))
    matchers = _sql_file_type_matchers(ctype=ctype, filename_or_ext=filename, filename_mode="suffix")

    if file_type == "text":
        return or_(
            Content.kind == "text",
            and_(Content.kind == "file", matchers["text"]),
        )
    if file_type in {"web", "image", "video", "audio", "log", "database", "archive", "document"}:
        return and_(Content.kind == "file", matchers[file_type])
    if file_type == "file":
        return and_(
            Content.kind == "file",
            not_(matchers["known"]),
        )
    return None


def _record_file_type_filter(file_type: str):
    stored = func.lower(func.coalesce(Content.file_type, ""))
    legacy = _legacy_record_file_type_filter(file_type)
    if legacy is None:
        return None
    return or_(stored == file_type, legacy)


def _legacy_asset_file_type_filter(file_type: str):
    kind = func.lower(func.coalesce(GeneratedAsset.kind, ""))
    ctype = func.lower(func.coalesce(GeneratedAsset.content_type, ""))
    ext = func.lower(func.coalesce(GeneratedAsset.ext, ""))
    matchers = _sql_file_type_matchers(
        ctype=ctype,
        filename_or_ext=ext,
        filename_mode="exact",
        include_blog_html_kind=kind,
    )

    if file_type in {"text", "web", "image", "video", "audio", "log", "database", "archive", "document"}:
        return matchers[file_type]
    if file_type == "file":
        return and_(
            kind != "blog_html",
            not_(matchers["known"]),
        )
    return None


def _asset_file_type_filter(file_type: str):
    stored = func.lower(func.coalesce(GeneratedAsset.file_type, ""))
    legacy = _legacy_asset_file_type_filter(file_type)
    if legacy is None:
        return None
    return or_(stored == file_type, legacy)


def _record_echo_file_type(record: Record) -> str:
    return _rechecked_content_file_type(record.content if record else None)


def _asset_echo_file_type(asset: GeneratedAsset) -> str:
    return _rechecked_asset_file_type(asset)


def _content_payload(
    content: Content,
    *,
    text_source: str = "oss",
    include_signed_url: bool = False,
    signed_url_expires: int = 300,
) -> dict:
    normalized_text_source = _normalize_content_text_source(text_source, default="full")
    normalized_expires = _normalize_signed_url_expires(signed_url_expires, default=300)
    signed_url = _signed_url_if_enabled(
        content.oss_key,
        enabled=bool(include_signed_url),
        expires=normalized_expires,
    )
    file_type = _rechecked_content_file_type(content)
    payload = {
        "id": content.id,
        "kind": content.kind,
        "file_type": file_type,
        "created_at": _iso(content.created_at),
        "updated_at": _iso(content.updated_at),
    }
    if content.kind == "text":
        if normalized_text_source == "db_preview":
            payload["text"] = str(content.text_content or "")
        else:
            payload["text"] = _read_text_content(content)
        payload["media_type"] = "text"
        payload["size_bytes"] = int(content.size_bytes or 0)
        payload["sha256"] = content.sha256 or ""
        payload["blob_url"] = url_for("api.get_content_blob", content_id=content.id)
        payload["signed_url"] = signed_url
        return payload

    payload.update(
        {
            "filename": content.filename or "",
            "content_type": content.content_type or "",
            "size_bytes": int(content.size_bytes or 0),
            "sha256": content.sha256 or "",
            "media_type": _content_media_type(content),
            "blob_url": url_for("api.get_content_blob", content_id=content.id),
            "signed_url": signed_url,
        }
    )
    return payload


def _comment_payload(comment: Comment) -> dict:
    return {
        "id": comment.id,
        "body": comment.body,
        "created_at": _iso(comment.created_at),
        "updated_at": _iso(comment.updated_at),
        "user": {
            "id": comment.user.id if comment.user else comment.user_id,
            "username": comment.user.username if comment.user else "",
        },
    }


def _record_payload(
    record: Record,
    *,
    viewer: User,
    include_content: bool,
    include_comments: bool,
    content_text_source: str = "oss",
    include_signed_url: bool = False,
    signed_url_expires: int = 300,
) -> dict:
    payload = {
        "id": record.id,
        "record_no": record.id,
        "visibility": record.visibility,
        "tags": record.get_tags(),
        "preview": record.preview or "",
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "can_edit": bool(viewer and record.user_id == viewer.id),
        "can_clone": _is_record_visible(record, viewer),
        "can_comment": _is_record_visible(record, viewer),
        "user": {
            "id": record.user.id if record.user else record.user_id,
            "username": record.user.username if record.user else "",
        },
    }

    if include_content:
        payload["content"] = _content_payload(
            record.content,
            text_source=content_text_source,
            include_signed_url=include_signed_url,
            signed_url_expires=signed_url_expires,
        )

    if include_comments:
        comments = sorted(record.comments, key=lambda item: item.created_at or datetime.utcnow())
        payload["comments"] = [_comment_payload(item) for item in comments]

    return payload


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _form_or_json_payload() -> dict:
    payload: dict = {}
    if request.is_json:
        payload.update(request.get_json(silent=True) or {})
    if request.form:
        payload.update(request.form.to_dict(flat=True))
    return payload


def _record_query_for(user: User, *, include_comments: bool = False, public_only: bool = False):
    options = [
        joinedload(Record.user),
        joinedload(Record.content),
    ]
    if include_comments:
        options.append(joinedload(Record.comments).joinedload(Comment.user))

    query = Record.query.options(*options)
    query = query.filter(_visible_filter(user.id, public_only=public_only))
    return query


def _apply_filter_values(query, *, user_id: str = "", tag: str = "", day: str = ""):
    user_id = str(user_id or "").strip()
    if user_id:
        if not user_id.isdigit():
            raise ValueError("invalid user_id")
        query = query.filter(Record.user_id == int(user_id))

    tag = str(tag or "").strip()
    if tag:
        query = query.filter(Record.tags_json.contains(f'"{tag}"'))

    day = str(day or "").strip()
    if day:
        day_value = _parse_day(day)
        start, end = _day_bounds(day_value)
        query = query.filter(Record.created_at >= start, Record.created_at < end)

    return query


def _apply_record_filters(query):
    return _apply_filter_values(
        query,
        user_id=str(request.args.get("user_id") or ""),
        tag=str(request.args.get("tag") or ""),
        day=str(request.args.get("day") or ""),
    )


def _file_to_content(file_obj) -> tuple[Content, str]:
    upload_dir = Path(current_app.config["UPLOAD_TMP_DIR"])
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = secure_filename(file_obj.filename or "") or f"upload-{new_uuid()}"
    suffix = Path(filename).suffix
    tmp_name = f"{new_uuid()}{suffix}"
    tmp_path = upload_dir / tmp_name

    file_obj.save(tmp_path)
    try:
        size_bytes = int(tmp_path.stat().st_size)
        sha256 = _sha256_file(tmp_path)
        content_type = file_obj.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        object_id = new_uuid()
        oss_key = record_content_key(object_id, filename)
        put_object_from_file(oss_key, str(tmp_path), content_type=content_type)

        content = Content(
            kind="file",
            file_type=_detect_file_type(content_type, filename),
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            oss_key=oss_key,
        )
        return content, filename
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _request_upload_files() -> list:
    if not request.files:
        return []

    files = []
    seen = set()
    preferred_fields = ("file", "files", "file[]")
    field_names = list(preferred_fields)
    field_names.extend(name for name in request.files.keys() if name not in preferred_fields)

    for field_name in field_names:
        for file_obj in request.files.getlist(field_name):
            if not file_obj:
                continue
            filename = str(file_obj.filename or "").strip()
            if not filename:
                continue
            marker = id(file_obj)
            if marker in seen:
                continue
            seen.add(marker)
            files.append(file_obj)
    return files


def _create_record_for_user(user: User):
    payload = _form_or_json_payload()
    manual_tags = _parse_tags(payload.get("tags"))
    visibility = _normalize_visibility(payload.get("visibility"), default="private")

    text_value = str(payload.get("text") or "").strip()
    auto_tags = _auto_tags_from_text(text_value) if text_value else []
    tags = _parse_tags(manual_tags + auto_tags)
    file_items = _request_upload_files()
    uploaded_file_tokens = _request_uploaded_file_tokens(payload)
    if not text_value and not file_items and not uploaded_file_tokens:
        return jsonify({"error": "missing text or file"}), 400

    direct_file_items: list[tuple[Content, str]] = []
    for upload_token in uploaded_file_tokens:
        try:
            direct_file_items.append(_content_from_uploaded_file_token(upload_token, user_id=user.id))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    created_records: list[Record] = []
    uploaded_oss_keys: list[str] = []

    if text_value:
        text_content = _text_to_content(text_value)
        text_record = Record(
            user_id=user.id,
            content=text_content,
            visibility=visibility,
            preview=_preview_text(text_value),
        )
        text_record.set_tags(tags)
        created_records.append(text_record)
        db.session.add(text_content)
        db.session.add(text_record)
        if text_content.oss_key:
            uploaded_oss_keys.append(text_content.oss_key)

    for file_obj in file_items:
        file_content, preview = _file_to_content(file_obj)
        file_record = Record(
            user_id=user.id,
            content=file_content,
            visibility=visibility,
            preview=preview,
        )
        file_record.set_tags(tags)
        created_records.append(file_record)
        db.session.add(file_content)
        db.session.add(file_record)
        if file_content.oss_key:
            uploaded_oss_keys.append(file_content.oss_key)

    for file_content, preview in direct_file_items:
        file_record = Record(
            user_id=user.id,
            content=file_content,
            visibility=visibility,
            preview=preview,
        )
        file_record.set_tags(tags)
        created_records.append(file_record)
        db.session.add(file_content)
        db.session.add(file_record)
        if file_content.oss_key:
            uploaded_oss_keys.append(file_content.oss_key)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        for oss_key in set(uploaded_oss_keys):
            try:
                delete_object(oss_key)
            except Exception:
                pass
        raise

    created_ids = [int(record.id) for record in created_records if record.id]
    loaded_rows = (
        Record.query.options(joinedload(Record.user), joinedload(Record.content))
        .filter(Record.id.in_(created_ids))
        .all()
    )
    loaded_map = {row.id: row for row in loaded_rows}
    record_items = [
        _record_payload(loaded_map[record_id], viewer=user, include_content=False, include_comments=False)
        for record_id in created_ids
        if record_id in loaded_map
    ]

    if len(record_items) == 1:
        return jsonify({"record": record_items[0], "count": 1}), 201
    return jsonify({"records": record_items, "count": len(record_items)}), 201


def _record_html_content(content: Content | None) -> str:
    if not content:
        return "<p class=\"muted\">内容缺失</p>"

    payload = _content_payload(content)
    if payload["kind"] == "text":
        return f"<pre>{_linkify_text_html(payload.get('text') or '')}</pre>"

    media_type = payload.get("media_type")
    src = payload.get("blob_url") or payload.get("signed_url") or ""
    if not src:
        return "<p class=\"muted\">文件不可用</p>"

    escaped_src = html.escape(src, quote=True)
    escaped_name = html.escape(payload.get("filename") or "file")
    meta_html = _notice_file_meta_html(payload)
    action_open = (
        f"<a class=\"notice-media-action\" href=\"{escaped_src}\" target=\"_blank\" rel=\"noreferrer noopener\">打开原文件</a>"
    )

    if media_type == "image":
        return (
            "<figure class=\"notice-media notice-media-image\">"
            "<div class=\"notice-media-stage\">"
            f"<a class=\"notice-media-link\" href=\"{escaped_src}\" target=\"_blank\" rel=\"noreferrer noopener\">"
            f"<img loading=\"lazy\" decoding=\"async\" src=\"{escaped_src}\" alt=\"{escaped_name}\">"
            "</a>"
            "</div>"
            "<figcaption class=\"notice-media-caption\">"
            "<div class=\"notice-media-caption-main\">"
            f"<p class=\"notice-media-name\">{escaped_name}</p>"
            f"{meta_html}"
            "</div>"
            f"{action_open}"
            "</figcaption>"
            "</figure>"
        )
    if media_type == "video":
        return (
            "<figure class=\"notice-media notice-media-video\">"
            "<div class=\"notice-media-stage\">"
            f"<video controls preload=\"metadata\" playsinline src=\"{escaped_src}\"></video>"
            "</div>"
            "<figcaption class=\"notice-media-caption\">"
            "<div class=\"notice-media-caption-main\">"
            f"<p class=\"notice-media-name\">{escaped_name}</p>"
            f"{meta_html}"
            "</div>"
            f"{action_open}"
            "</figcaption>"
            "</figure>"
        )
    if media_type == "audio":
        return (
            "<section class=\"notice-media notice-media-audio\">"
            "<header class=\"notice-audio-head\">"
            "<p class=\"notice-audio-label\">AUDIO</p>"
            "<div class=\"notice-audio-main\">"
            f"<p class=\"notice-media-name\">{escaped_name}</p>"
            f"{meta_html}"
            "</div>"
            f"{action_open}"
            "</header>"
            f"<audio class=\"notice-audio-control\" controls preload=\"none\" src=\"{escaped_src}\"></audio>"
            "</section>"
        )
    return (
        "<section class=\"notice-file-card\">"
        f"<p class=\"notice-file-name\">{escaped_name}</p>"
        f"{meta_html}"
        f"<p class=\"notice-file-actions\">{action_open}</p>"
        "</section>"
    )


def _render_notice_html(
    records: list[Record], *, day: str, user_id: str, tag: str, viewer: User | None = None
) -> str:
    if not records:
        return "<article class=\"notice-render\"><p>没有匹配到记录。</p></article>"

    top_tags = Counter()
    for item in records:
        top_tags.update(item.get_tags())

    filter_badges: list[str] = []
    if day:
        filter_badges.append(f"日期: {html.escape(day)}")
    if user_id:
        filter_badges.append(f"用户ID: {html.escape(user_id)}")
    if tag:
        filter_badges.append(f"标签: #{html.escape(tag)}")

    side_lines: list[str] = [
        "<aside class=\"notice-context-rail\">",
        "<div class=\"notice-context-head\">",
        "<p class=\"notice-context-title\">筛选与索引</p>",
        f"<p class=\"notice-summary-line\">总记录数: {len(records)}</p>",
    ]
    if filter_badges:
        side_lines.append(
            "<div class=\"notice-context-badges\">"
            + "".join(f"<span class=\"notice-context-badge\">{item}</span>" for item in filter_badges)
            + "</div>"
        )
    if top_tags:
        top_tag_links = [_notice_tag_link(key, class_name="tag-pill notice-side-tag") for key, _ in top_tags.most_common(10)]
        top_tag_links = [item for item in top_tag_links if item]
        if top_tag_links:
            side_lines.append("<div class=\"notice-context-hot-tags\">" + "".join(top_tag_links) + "</div>")
    side_lines.append("</div>")
    side_lines.append("<div class=\"notice-context-list\">")

    lines: list[str] = [
        "<article class=\"notice-render notice-reader-layout\">",
        "<section class=\"notice-main-flow\">",
    ]

    current_day = ""
    for record in records:
        day_key = (record.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
        if day_key != current_day:
            if current_day:
                lines.append("</section>")
            lines.append(f"<section class=\"notice-day\" data-notice-day=\"{html.escape(day_key, quote=True)}\">")
            side_lines.append(f"<p class=\"notice-context-day\">{html.escape(day_key)}</p>")
            current_day = day_key

        user_name = html.escape(record.user.username if record.user else "")
        stamp = html.escape((record.created_at or datetime.utcnow()).strftime("%H:%M"))
        visibility_text = html.escape(record.visibility or "")
        record_anchor = f"notice-record-{int(record.id)}"
        record_tag_links = [_notice_tag_link(tag_text, class_name="tag-pill notice-side-tag") for tag_text in record.get_tags()]
        record_tag_links = [item for item in record_tag_links if item]

        preview_text = (record.preview or "").strip()
        content_kind = (record.content.kind if record.content else "").strip().lower()

        lines.extend(
            [
                f"<article class=\"notice-block\" id=\"{record_anchor}\">",
            ]
        )
        if preview_text and content_kind != "text":
            lines.append(f"<p class=\"notice-preview\">{_linkify_text_html(preview_text)}</p>")
        lines.extend(
            [
                f"<div class=\"notice-block-body\">{_record_html_content(record.content)}</div>",
                "</article>",
            ]
        )

        side_lines.extend(
            [
                "<article class=\"notice-context-item\">",
                f"<a class=\"notice-context-link\" href=\"#{record_anchor}\" data-notice-anchor=\"{record_anchor}\">",
                f"<span class=\"notice-context-time\">{stamp}</span>",
                f"<span class=\"notice-context-user\">{user_name or '匿名用户'}</span>",
                f"<span class=\"notice-context-record\">#{int(record.id)} · {visibility_text or 'record'}</span>",
                "</a>",
                "<p class=\"notice-context-tags\">"
                + (" ".join(record_tag_links) if record_tag_links else "<span class=\"muted\">无标签</span>")
                + "</p>",
            ]
        )
        can_edit = bool(viewer and record.user_id == viewer.id)
        if can_edit:
            side_lines.append(
                "<p class=\"notice-context-actions\">"
                f"<button type=\"button\" class=\"bubble notice-context-action-edit\" data-action=\"edit-record\" data-record-id=\"{int(record.id)}\">编辑</button>"
                "</p>"
            )
        side_lines.append("</article>")

    lines.append("</section>")
    lines.append("</section>")
    side_lines.append("</div>")
    side_lines.append("</aside>")
    lines.extend(side_lines)
    lines.append("</article>")
    return "\n".join(lines)


def _ai_provider_settings() -> dict | None:
    provider = _chat_provider()
    return _provider_settings_for_chat(provider)


def _normalize_provider(raw: str) -> str:
    return normalize_ai_provider(raw)


def _chat_provider() -> str:
    provider = _normalize_provider(get_setting_str("AI_CHAT_PROVIDER", default=""))
    return provider


def _provider_raw_config(provider: str) -> dict | None:
    normalized = _normalize_provider(provider)
    selected = provider_connection_settings(normalized)
    if not selected:
        return None

    chat_model = provider_model(normalized, "chat")
    if _model_placeholder(chat_model):
        return None
    return {
        "provider": str(selected.get("provider") or normalized),
        "api_key": str(selected.get("api_key") or ""),
        "base_url": str(selected.get("base_url") or ""),
        "chat_model": chat_model,
    }


def _provider_settings_for_chat(provider: str) -> dict | None:
    config = _provider_raw_config(provider)
    if not config:
        return None
    model = str(config.get("chat_model") or "").strip()
    if not model:
        return None
    return {
        "provider": str(config.get("provider") or ""),
        "api_key": str(config.get("api_key") or ""),
        "base_url": str(config.get("base_url") or ""),
        "model": model,
    }


def _provider_settings_with_model(provider: str, model: str) -> dict | None:
    config = _provider_raw_config(provider)
    selected_model = str(model or "").strip()
    if not config or not selected_model:
        return None
    return {
        "provider": str(config.get("provider") or ""),
        "api_key": str(config.get("api_key") or ""),
        "base_url": str(config.get("base_url") or ""),
        "model": selected_model,
    }


def _model_placeholder(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "none", "n/a", "na", "-", "unsupported", "not-supported", "not_supported"}


def _capability_default_model(capability: str, provider: str) -> str:
    _, default_model = provider_model_setting(provider, capability)
    return str(default_model or "")


def _capability_provider_order(capability: str) -> list[str]:
    provider_key = {
        "tts": "AI_TTS_PROVIDER",
        "image": "AI_IMAGE_PROVIDER",
    }.get(capability, "")

    explicit_provider = _normalize_provider(get_setting_str(provider_key, default="")) if provider_key else ""
    if not explicit_provider:
        return []

    ordered: list[str] = []
    ordered.append(explicit_provider)
    # Common fallback providers for media generation.
    ordered.extend(["openai", "chatanywhere", "aliyun", "deepseek"])

    result: list[str] = []
    seen: set[str] = set()
    for provider in ordered:
        if not provider or provider in seen:
            continue
        seen.add(provider)
        result.append(provider)
    return result


def _capability_provider_model_key(capability: str, provider: str) -> str:
    key, _ = provider_model_setting(provider, capability)
    return str(key or "")


def _capability_provider_model(capability: str, provider: str) -> str:
    setting_key = _capability_provider_model_key(capability, provider)
    if not setting_key:
        return ""
    default_model = _capability_default_model(capability, provider)
    return get_setting_str(setting_key, default=default_model).strip()


def _capability_settings_candidates(capability: str) -> list[dict]:
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for provider in _capability_provider_order(capability):
        model = _capability_provider_model(capability, provider)
        if _model_placeholder(model):
            continue
        pair = (provider, model)
        if pair in seen:
            continue
        seen.add(pair)
        settings = _provider_settings_with_model(provider, model)
        if settings:
            candidates.append(settings)
    return candidates


def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                text = part.strip()
                if text:
                    chunks.append(text)
                continue
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").strip().lower()
            if part_type == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    chunks.append(text)
                continue
            if part_type == "image_url":
                image_payload = part.get("image_url") or {}
                if isinstance(image_payload, dict):
                    image_url = str(image_payload.get("url") or "").strip()
                else:
                    image_url = str(image_payload or "").strip()
                if image_url:
                    chunks.append(f"[IMAGE] {image_url}")
                continue

            text = str(part.get("text") or "").strip()
            if text:
                chunks.append(text)
        return "\n".join(chunks).strip()
    return str(content or "").strip()


def _messages_have_non_text_content(messages: list[dict]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and str(part.get("type") or "").strip().lower() != "text":
                    return True
    return False


def _messages_to_text_only(messages: list[dict]) -> list[dict]:
    compact: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "user").strip() or "user"
        compact.append(
            {
                "role": role,
                "content": _message_content_to_text(message.get("content")),
            }
        )
    return compact


def _ai_chat(*, messages: list[dict], temperature: float = 0.2, max_tokens: int = 1800) -> tuple[str, dict]:
    settings = _ai_provider_settings()
    if not settings:
        raise RuntimeError("AI provider not configured")

    endpoint = settings["base_url"] + "/chat/completions"
    timeout = get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45)
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }

    def _request_once(payload_messages: list[dict]) -> str:
        payload = {
            "model": settings["model"],
            "messages": payload_messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"provider request error: {exc}") from exc
        if not response.ok:
            detail = response.text.strip().replace("\n", " ")
            if len(detail) > 320:
                detail = detail[:320] + "..."
            raise RuntimeError(f"provider request failed ({response.status_code}): {detail}")

        data = response.json()
        choices = data.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}
        content = _message_content_to_text(message.get("content"))
        content = str(content or "").strip()
        if not content:
            raise RuntimeError("provider returned empty content")
        return content

    try:
        output = _request_once(messages)
    except RuntimeError as exc:
        if not _messages_have_non_text_content(messages):
            raise
        fallback_messages = _messages_to_text_only(messages)
        if fallback_messages == messages:
            raise
        current_app.logger.warning(
            "chat request with media payload failed, retrying with text-only fallback: %s",
            exc,
        )
        output = _request_once(fallback_messages)

    return output, settings


def _normalize_prompt_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _trim_text_balanced(value: str, *, limit: int) -> str:
    text = _normalize_prompt_text(value)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    if limit <= 360:
        return text[:limit].rstrip()

    head = int(limit * 0.72)
    tail = max(120, limit - head - 48)
    if head + tail >= len(text):
        return text[:limit].rstrip()

    omitted = len(text) - head - tail
    return f"{text[:head].rstrip()}\n...[省略 {omitted} 字]...\n{text[-tail:].lstrip()}"


def _is_text_like_content(content: Content) -> bool:
    inferred = _rechecked_content_file_type(content)
    return inferred in {"text", "web", "log"}


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


def _extract_file_text_for_prompt(content: Content, *, max_bytes: int) -> tuple[str, str]:
    filename = str(content.filename or "").strip() or "unknown"
    ctype = str(content.content_type or "").split(";", 1)[0].strip().lower() or "application/octet-stream"

    if not content.oss_key:
        return "", f"[FILE] {filename} ({ctype})"
    if not _is_text_like_content(content):
        return "", f"[FILE] {filename} ({ctype})"

    size_bytes = int(content.size_bytes or 0)

    try:
        raw = get_object_bytes(content.oss_key, max_bytes=max_bytes)
    except Exception as exc:
        current_app.logger.warning("failed to read record file for ai prompt: %s", exc)
        return "", f"[FILE] {filename} ({ctype}) 读取失败。"

    truncated = bool(size_bytes and size_bytes > len(raw))

    decoded, encoding = _decode_text_bytes(raw)
    if not decoded:
        return "", f"[FILE] {filename} ({ctype}) 无法提取可读文本。"

    body = _normalize_prompt_text(decoded)
    if not body:
        return "", f"[FILE] {filename} ({ctype}) 文本为空。"

    title = f"[FILE-TEXT] {filename} ({ctype}; encoding={encoding})"
    if truncated:
        body = f"{body}\n...[文件内容按 {max_bytes} bytes 截断]..."
    return f"{title}\n{body}", title


def _record_full_text_for_prompt(record: Record, *, max_file_bytes: int) -> str:
    if record.content.kind == "text":
        text = _normalize_prompt_text(_read_text_content(record.content, max_bytes=max_file_bytes))
        return text or _normalize_prompt_text(record.preview or "")

    preview = _normalize_prompt_text(record.preview or "")
    extracted, fallback = _extract_file_text_for_prompt(record.content, max_bytes=max_file_bytes)
    if extracted and preview:
        return f"{preview}\n\n{extracted}"
    if extracted:
        return extracted
    if preview and fallback:
        return f"{fallback}\n{preview}"
    return preview or fallback


def _records_for_ai_prompt(records: list[Record], *, max_chars: int | None = None) -> str:
    total_limit = int(max_chars or get_setting_int("AI_NOTICE_CONTEXT_MAX_CHARS", default=60000))
    total_limit = max(8000, min(total_limit, 260000))
    per_record_limit = get_setting_int("AI_NOTICE_RECORD_MAX_CHARS", default=3200)
    per_record_limit = max(600, min(per_record_limit, 24000))
    max_file_bytes = get_setting_int("AI_NOTICE_FILE_READ_MAX_BYTES", default=524288)
    max_file_bytes = max(65536, min(max_file_bytes, 8 * 1024 * 1024))

    chunks: list[str] = []
    used_chars = 0
    for record in records:
        tags = ",".join(record.get_tags()) or "-"
        created = (record.created_at or datetime.utcnow()).isoformat()
        username = record.user.username if record.user else record.user_id
        header = (
            f"[#{record.id}] user={username} "
            f"time={created} visibility={record.visibility} tags={tags}"
        )

        full_text = _record_full_text_for_prompt(record, max_file_bytes=max_file_bytes)
        if not full_text:
            full_text = _normalize_prompt_text(record.preview or "")
        body = _trim_text_balanced(full_text, limit=per_record_limit)
        if not body:
            continue

        separator = 2 if chunks else 0
        allowed_body = total_limit - used_chars - separator - len(header) - 1
        if allowed_body <= 0:
            break
        if len(body) > allowed_body:
            if allowed_body < 200:
                break
            body = _trim_text_balanced(body, limit=allowed_body)

        block = f"{header}\n{body}"
        chunks.append(block)
        used_chars += len(block) + separator
        if used_chars >= total_limit:
            break

    return "\n\n".join(chunks)


def _archive_rows_for_day(day_value: date) -> list[dict]:
    payload = load_archive(archive_file_path(day_value))
    rows = payload.get("records") or []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _records_for_ai_prompt_from_archive(rows: list[dict], *, max_chars: int | None = None) -> str:
    total_limit = int(max_chars or get_setting_int("AI_NOTICE_CONTEXT_MAX_CHARS", default=60000))
    total_limit = max(8000, min(total_limit, 260000))
    per_record_limit = get_setting_int("AI_NOTICE_RECORD_MAX_CHARS", default=3200)
    per_record_limit = max(600, min(per_record_limit, 24000))

    chunks: list[str] = []
    used_chars = 0
    for row in rows:
        user = row.get("user") or {}
        tags = [str(item).strip() for item in (row.get("tags") or []) if str(item).strip()]
        tags_text = ",".join(tags) or "-"
        created = str(row.get("created_at") or "").strip() or datetime.utcnow().isoformat()
        visibility = str(row.get("visibility") or "public").strip() or "public"
        username = str(user.get("username") or row.get("user_id") or "-")
        record_id = int(row.get("id") or row.get("record_no") or 0)
        header = f"[#{record_id}] user={username} time={created} visibility={visibility} tags={tags_text}"

        full_text = _normalize_prompt_text(str(row.get("text") or row.get("preview") or ""))
        body = _trim_text_balanced(full_text, limit=per_record_limit)
        if not body:
            continue

        separator = 2 if chunks else 0
        allowed_body = total_limit - used_chars - separator - len(header) - 1
        if allowed_body <= 0:
            break
        if len(body) > allowed_body:
            if allowed_body < 200:
                break
            body = _trim_text_balanced(body, limit=allowed_body)

        block = f"{header}\n{body}"
        chunks.append(block)
        used_chars += len(block) + separator
        if used_chars >= total_limit:
            break

    return "\n\n".join(chunks)


def _notice_image_urls_from_archive_rows(rows: list[dict]) -> list[str]:
    if not get_setting_bool("AI_NOTICE_ATTACH_IMAGES", default=True):
        return []

    max_images = max(0, min(get_setting_int("AI_NOTICE_MAX_IMAGE_ATTACHMENTS", default=6), 20))
    if max_images <= 0:
        return []
    expires = max(300, min(get_setting_int("AI_NOTICE_IMAGE_URL_EXPIRES_SECONDS", default=1800), 86400))

    urls: list[str] = []
    seen_keys: set[str] = set()
    for row in rows:
        content = row.get("content") or {}
        if not isinstance(content, dict):
            continue
        if str(content.get("kind") or "") != "file":
            continue
        if str(content.get("media_type") or "").lower() != "image":
            continue
        oss_key = str(content.get("oss_key") or "").strip()
        if not oss_key or oss_key in seen_keys:
            continue
        signed = sign_get_url(oss_key, expires=expires)
        if not signed:
            continue
        seen_keys.add(oss_key)
        urls.append(signed)
        if len(urls) >= max_images:
            break
    return urls


def _source_images_from_records(records: list[Record], *, max_items: int) -> list[dict]:
    limit = max(0, min(int(max_items or 0), 60))
    if limit <= 0:
        return []

    items: list[dict] = []
    seen_content_ids: set[int] = set()
    for record in records:
        if len(items) >= limit:
            break
        content = record.content
        if not content or str(content.kind or "").strip().lower() != "file":
            continue
        if _rechecked_content_file_type(content) != "image":
            continue

        content_id = int(content.id or 0)
        if content_id <= 0 or content_id in seen_content_ids:
            continue
        seen_content_ids.add(content_id)

        tags = [item for item in record.get_tags() if item]
        tags_text = ", ".join(tags[:5])
        preview = _trim_text_balanced(_normalize_prompt_text(record.preview or ""), limit=180)
        filename = str(content.filename or "").strip() or f"record-{int(record.id)}.image"

        note_parts = [f"record#{int(record.id)}"]
        if tags_text:
            note_parts.append(f"tags={tags_text}")
        if preview:
            note_parts.append(f"preview={preview}")
        note = " | ".join(note_parts)

        index = len(items) + 1
        fallback_src = f"/api/contents/{content_id}/blob"
        src, fallback_media_src = _prefer_oss_media_src(oss_key=content.oss_key, fallback_src=fallback_src)
        items.append(
            {
                "id": f"img-{index}",
                "record_id": int(record.id),
                "content_id": content_id,
                "slot_hint": _normalize_slot_name(tags[0] if tags else "", fallback_index=index),
                "src": src,
                "fallback_src": fallback_media_src,
                "title": filename[:120],
                "caption": preview[:200] if preview else filename[:120],
                "note": note[:480],
            }
        )
    return items


def _source_audios_from_records(records: list[Record], *, max_items: int) -> list[dict]:
    limit = max(0, min(int(max_items or 0), 60))
    if limit <= 0:
        return []

    items: list[dict] = []
    seen_content_ids: set[int] = set()
    for record in records:
        if len(items) >= limit:
            break
        content = record.content
        if not content or str(content.kind or "").strip().lower() != "file":
            continue
        if _rechecked_content_file_type(content) != "audio":
            continue

        content_id = int(content.id or 0)
        if content_id <= 0 or content_id in seen_content_ids:
            continue
        seen_content_ids.add(content_id)

        tags = [item for item in record.get_tags() if item]
        tags_text = ", ".join(tags[:5])
        preview = _trim_text_balanced(_normalize_prompt_text(record.preview or ""), limit=180)
        filename = str(content.filename or "").strip() or f"record-{int(record.id)}.audio"

        note_parts = [f"record#{int(record.id)}"]
        if tags_text:
            note_parts.append(f"tags={tags_text}")
        if preview:
            note_parts.append(f"preview={preview}")
        note = " | ".join(note_parts)

        index = len(items) + 1
        fallback_src = f"/api/contents/{content_id}/blob"
        src, fallback_media_src = _prefer_oss_media_src(oss_key=content.oss_key, fallback_src=fallback_src)
        items.append(
            {
                "id": f"aud-{index}",
                "record_id": int(record.id),
                "content_id": content_id,
                "src": src,
                "fallback_src": fallback_media_src,
                "title": filename[:120],
                "caption": preview[:200] if preview else filename[:120],
                "note": note[:480],
            }
        )
    return items


def _source_videos_from_records(records: list[Record], *, max_items: int) -> list[dict]:
    limit = max(0, min(int(max_items or 0), 60))
    if limit <= 0:
        return []

    items: list[dict] = []
    seen_content_ids: set[int] = set()
    for record in records:
        if len(items) >= limit:
            break
        content = record.content
        if not content or str(content.kind or "").strip().lower() != "file":
            continue
        if _rechecked_content_file_type(content) != "video":
            continue

        content_id = int(content.id or 0)
        if content_id <= 0 or content_id in seen_content_ids:
            continue
        seen_content_ids.add(content_id)

        tags = [item for item in record.get_tags() if item]
        tags_text = ", ".join(tags[:5])
        preview = _trim_text_balanced(_normalize_prompt_text(record.preview or ""), limit=180)
        filename = str(content.filename or "").strip() or f"record-{int(record.id)}.video"

        note_parts = [f"record#{int(record.id)}"]
        if tags_text:
            note_parts.append(f"tags={tags_text}")
        if preview:
            note_parts.append(f"preview={preview}")
        note = " | ".join(note_parts)

        index = len(items) + 1
        fallback_src = f"/api/contents/{content_id}/blob"
        src, fallback_media_src = _prefer_oss_media_src(oss_key=content.oss_key, fallback_src=fallback_src)
        items.append(
            {
                "id": f"vid-{index}",
                "record_id": int(record.id),
                "content_id": content_id,
                "src": src,
                "fallback_src": fallback_media_src,
                "title": filename[:120],
                "caption": preview[:200] if preview else filename[:120],
                "note": note[:480],
            }
        )
    return items


def _source_images_compact_text(source_images: list[dict]) -> str:
    lines: list[str] = []
    for item in source_images:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("id") or "").strip()
        if not image_id:
            continue
        title = str(item.get("title") or "").strip()
        note = str(item.get("note") or "").strip()
        slot_hint = str(item.get("slot_hint") or "").strip()
        pieces = [image_id]
        if slot_hint:
            pieces.append(f"hint={slot_hint}")
        if title:
            pieces.append(f"title={title}")
        if note:
            pieces.append(f"note={note}")
        lines.append(" | ".join(pieces))
    return "\n".join(lines).strip()


def _assign_source_images_to_slots(*, plan: dict, source_images: list[dict]) -> dict[str, list[str]]:
    images = [item for item in source_images if isinstance(item, dict) and str(item.get("id") or "").strip()]
    if not images:
        return {}

    sections_raw = plan.get("sections") or []
    sections = sections_raw if isinstance(sections_raw, list) else []
    slot_order: list[str] = []
    slot_limits: dict[str, int] = {}
    for idx, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            continue
        slot = _normalize_slot_name(section.get("slot"), fallback_index=idx)
        if slot in slot_limits:
            continue
        count = max(0, min(_safe_int(section.get("image_count"), default=0), 4))
        slot_order.append(slot)
        slot_limits[slot] = count

    if not slot_order:
        slot_order = ["slot-1"]
        slot_limits = {"slot-1": max(1, min(len(images), 3))}
    elif max(slot_limits.values() or [0]) <= 0:
        slot_limits[slot_order[0]] = max(1, min(len(images), 3))

    def _fallback_assign(image_ids: list[str], existing: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
        result = {slot: list((existing or {}).get(slot) or []) for slot in slot_order}
        candidates = [slot for slot in slot_order if int(slot_limits.get(slot) or 0) > 0]
        if not candidates:
            return {}

        cursor = 0
        for image_id in image_ids:
            placed = False
            for _ in range(len(candidates)):
                slot = candidates[cursor % len(candidates)]
                cursor += 1
                limit = int(slot_limits.get(slot) or 0)
                if limit <= 0:
                    continue
                if len(result[slot]) >= limit:
                    continue
                if image_id in result[slot]:
                    placed = True
                    break
                result[slot].append(image_id)
                placed = True
                break
            if not placed:
                break
        return {slot: ids for slot, ids in result.items() if ids}

    ordered_ids = [str(item.get("id")) for item in images]
    fallback = _fallback_assign(ordered_ids)

    assignments_raw = None
    try:
        output, _ = _ai_chat(
            messages=[
                {
                    "role": "system",
                    "content": "你是媒体编排助手。你只输出 JSON，不要解释。",
                },
                {
                    "role": "user",
                    "content": (
                        "请把原图分配到各段落 slot。\n"
                        "规则：\n"
                        "1) 只能使用给定 slot 与图片 id；\n"
                        "2) 每个图片最多分配到一个 slot；\n"
                        "3) 每个 slot 的图片数量不超过该 slot 的 limit；\n"
                        "4) 若不确定，优先按内容相关性分配。\n\n"
                        f"slot limits:\n{json.dumps(slot_limits, ensure_ascii=False)}\n\n"
                        f"source images:\n{_source_images_compact_text(images)}\n\n"
                        "输出 JSON：\n"
                        '{\n'
                        '  "assignments": [\n'
                        '    {"slot": "slot-1", "source_image_ids": ["img-1"]}\n'
                        "  ]\n"
                        "}\n"
                    ),
                },
            ],
            temperature=0.12,
            max_tokens=1000,
        )
        parsed = _parse_ai_json_object(output)
        assignments_raw = parsed.get("assignments")
    except Exception as exc:
        current_app.logger.warning("source image slot assignment fallback: %s", exc)
        assignments_raw = None

    if not isinstance(assignments_raw, list):
        return fallback

    valid_slots = set(slot_order)
    valid_ids = set(ordered_ids)
    used_ids: set[str] = set()
    result: dict[str, list[str]] = {slot: [] for slot in slot_order}

    for entry in assignments_raw:
        if not isinstance(entry, dict):
            continue
        slot = _normalize_slot_name(entry.get("slot"), fallback_index=1)
        if slot not in valid_slots:
            continue
        limit = int(slot_limits.get(slot) or 0)
        if limit <= 0:
            continue
        ids_raw = entry.get("source_image_ids")
        if not isinstance(ids_raw, list):
            ids_raw = entry.get("image_ids")
        if not isinstance(ids_raw, list):
            continue
        for raw_id in ids_raw:
            image_id = str(raw_id or "").strip()
            if not image_id or image_id not in valid_ids:
                continue
            if image_id in used_ids:
                continue
            if len(result[slot]) >= limit:
                break
            result[slot].append(image_id)
            used_ids.add(image_id)

    remaining_ids = [image_id for image_id in ordered_ids if image_id not in used_ids]
    if remaining_ids:
        result = _fallback_assign(remaining_ids, existing=result)

    cleaned = {slot: ids for slot, ids in result.items() if ids}
    return cleaned or fallback


def _normalize_podcast_style(raw) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "talk": "dialogue",
        "chat": "dialogue",
        "conversation": "dialogue",
        "speech": "speech",
        "lecture": "speech",
        "interview": "interview",
        "qa": "interview",
        "news": "news",
        "newscast": "news",
    }
    value = aliases.get(value, value)
    if value in _PODCAST_STYLE_GUIDE:
        return value

    fallback = get_setting_str("PODCAST_DEFAULT_STYLE", default="dialogue").strip().lower()
    fallback = aliases.get(fallback, fallback)
    if fallback in _PODCAST_STYLE_GUIDE:
        return fallback
    return "dialogue"


def _build_notice_ai_prompt(
    action: str,
    records_text: str,
    *,
    podcast_style: str = "dialogue",
    image_urls: list[str] | None = None,
) -> list[dict]:
    if action == "blog":
        task = get_setting_str("PROMPT_NOTICE_BLOG_TASK", default=DEFAULT_NOTICE_BLOG_TASK)
    elif action == "podcast":
        style = _normalize_podcast_style(podcast_style)
        style_line = _PODCAST_STYLE_GUIDE.get(style, _PODCAST_STYLE_GUIDE["dialogue"])
        task = get_setting_str("PROMPT_NOTICE_PODCAST_TASK", default=DEFAULT_NOTICE_PODCAST_TASK)
        task = (
            f"{task}\n"
            f"风格要求：{style_line}\n"
            "请输出适合直接 TTS 的纯文本脚本，不要 markdown，不要代码块。"
        )
    elif action == "poster":
        task = get_setting_str("PROMPT_NOTICE_POSTER_TASK", default=DEFAULT_NOTICE_POSTER_TASK)
    else:
        task = "把输入记录整理成结构清晰的中文总结，准确且可读。"

    system_prompt = get_setting_str("PROMPT_NOTICE_SYSTEM", default=DEFAULT_NOTICE_SYSTEM_PROMPT)
    user_text = f"{task}\n\n输入记录如下：\n{records_text}"
    clean_urls = [str(url or "").strip() for url in (image_urls or []) if str(url or "").strip()]
    user_content: str | list[dict] = user_text
    if clean_urls:
        content_parts: list[dict] = [
            {
                "type": "text",
                "text": (
                    f"{user_text}\n\n"
                    "附加图片已一并提供。请仅基于可见证据写作，不要臆造图片中不存在的信息。"
                ),
            }
        ]
        for image_url in clean_urls:
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                }
            )
        user_content = content_parts

    return [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": user_content,
        },
    ]


def _safe_int(value, *, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _normalize_slot_name(raw: str, *, fallback_index: int) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    if not text:
        text = f"slot-{max(1, int(fallback_index or 1))}"
    return text[:64]


def _parse_ai_json_object(raw: str) -> dict:
    text = _strip_markdown_code_fence(raw)
    candidates: list[str] = []
    if text:
        candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except Exception:
            continue
        if isinstance(loaded, dict):
            return loaded
    raise ValueError("invalid ai json output")


def _digest_collab_enabled() -> bool:
    return get_setting_bool("DIGEST_COLLAB_ENABLED", default=True)


def _digest_collab_max_sections() -> int:
    return max(1, min(get_setting_int("DIGEST_COLLAB_MAX_SECTIONS", default=4), 8))


def _digest_collab_max_image_assets() -> int:
    return max(0, min(get_setting_int("DIGEST_COLLAB_MAX_IMAGE_ASSETS", default=4), 12))


def _digest_collab_max_source_images() -> int:
    return max(0, min(get_setting_int("DIGEST_COLLAB_MAX_SOURCE_IMAGES", default=12), 30))


def _digest_collab_max_audio_assets() -> int:
    return max(0, min(get_setting_int("DIGEST_COLLAB_MAX_AUDIO_ASSETS", default=3), 10))


def _digest_collab_review_rounds() -> int:
    return max(0, min(get_setting_int("DIGEST_COLLAB_REVIEW_ROUNDS", default=2), 3))


def _build_digest_collab_plan(records_text: str) -> tuple[dict, dict | None]:
    max_sections = _digest_collab_max_sections()
    max_images = _digest_collab_max_image_assets()
    max_audios = _digest_collab_max_audio_assets()

    fallback_section = {
        "slot": "slot-1",
        "title": "核心摘要",
        "focus": _trim_text_balanced(records_text, limit=1400),
        "image_count": 1 if max_images > 0 else 0,
        "audio_count": 1 if max_audios > 0 else 0,
    }
    fallback_plan = {"lead": "", "sections": [fallback_section]}

    if not _digest_collab_enabled():
        return fallback_plan, None

    system_prompt = (
        "你是 Benoss 的 Planner Agent。"
        "你负责把输入记录拆分成可并行生成媒体的章节计划。"
        "只输出 JSON，不要解释。"
    )
    user_prompt = (
        "请把输入记录规划为日报博客章节，并给出每章建议媒体数量。\n"
        f"限制：sections 最多 {max_sections} 个；全局 image_count 总和不超过 {max_images}；"
        f"audio_count 总和不超过 {max_audios}。\n"
        "输出 JSON 结构：\n"
        '{\n'
        '  "lead": "一句话定位（可选）",\n'
        '  "sections": [\n'
        '    {\n'
        '      "slot": "slot-1",\n'
        '      "title": "章节标题",\n'
        '      "focus": "章节摘要（1-3 句）",\n'
        '      "image_count": 0,\n'
        '      "audio_count": 0\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"输入记录：\n{records_text}"
    )

    try:
        output, settings = _ai_chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.18,
            max_tokens=1200,
        )
        parsed = _parse_ai_json_object(output)
    except Exception as exc:
        current_app.logger.warning("digest planner fallback: %s", exc)
        return fallback_plan, None

    sections_raw = parsed.get("sections") or []
    if not isinstance(sections_raw, list):
        sections_raw = []

    sections: list[dict] = []
    seen_slots: set[str] = set()
    for item in sections_raw:
        if len(sections) >= max_sections:
            break
        if not isinstance(item, dict):
            continue
        idx = len(sections) + 1
        title = _normalize_prompt_text(str(item.get("title") or item.get("heading") or "").strip())
        title = title[:80] or f"章节 {idx}"
        focus_raw = str(item.get("focus") or item.get("summary") or item.get("description") or "").strip()
        focus = _trim_text_balanced(focus_raw or title, limit=1200)
        slot = _normalize_slot_name(
            str(item.get("slot") or item.get("id") or title),
            fallback_index=idx,
        )
        if slot in seen_slots:
            slot = _normalize_slot_name(f"{slot}-{idx}", fallback_index=idx)
        seen_slots.add(slot)
        image_count = max(0, min(_safe_int(item.get("image_count"), default=0), 3))
        audio_count = max(0, min(_safe_int(item.get("audio_count"), default=0), 3))
        sections.append(
            {
                "slot": slot,
                "title": title,
                "focus": focus,
                "image_count": image_count,
                "audio_count": audio_count,
            }
        )

    if not sections:
        sections = [fallback_section]

    remaining_images = max_images
    for section in sections:
        allowed = min(int(section.get("image_count") or 0), remaining_images)
        section["image_count"] = allowed
        remaining_images -= allowed
    if max_images > 0 and sum(int(item.get("image_count") or 0) for item in sections) <= 0:
        sections[0]["image_count"] = 1

    remaining_audios = max_audios
    for section in sections:
        allowed = min(int(section.get("audio_count") or 0), remaining_audios)
        section["audio_count"] = allowed
        remaining_audios -= allowed
    if max_audios > 0 and sum(int(item.get("audio_count") or 0) for item in sections) <= 0:
        sections[0]["audio_count"] = 1

    plan = {
        "lead": _trim_text_balanced(str(parsed.get("lead") or "").strip(), limit=220),
        "sections": sections,
    }
    return plan, settings


def _collab_plan_compact_text(plan: dict) -> str:
    sections = plan.get("sections") or []
    lines: list[str] = []
    lead = str(plan.get("lead") or "").strip()
    if lead:
        lines.append(f"定位：{lead}")
    for idx, section in enumerate(sections, start=1):
        slot = str(section.get("slot") or f"slot-{idx}")
        title = str(section.get("title") or f"章节 {idx}")
        focus = str(section.get("focus") or "")
        images = int(section.get("image_count") or 0)
        audios = int(section.get("audio_count") or 0)
        lines.append(f"{idx}. slot={slot} title={title} images={images} audios={audios}")
        if focus:
            lines.append(f"   focus={focus}")
    return "\n".join(lines).strip()


def _build_collab_blog_html(
    *,
    title: str,
    records_text: str,
    image_urls: list[str] | None,
    plan: dict,
) -> tuple[str, dict]:
    task = get_setting_str("PROMPT_NOTICE_BLOG_TASK", default=DEFAULT_NOTICE_BLOG_TASK).strip()
    system_prompt = (
        get_setting_str("PROMPT_NOTICE_SYSTEM", default=DEFAULT_NOTICE_SYSTEM_PROMPT).strip()
        + "\n你是 Writer Agent，负责写可直接发布的中文博客 HTML。"
    )
    sections_text = _collab_plan_compact_text(plan)
    user_text = (
        f"{task}\n\n"
        "请基于下面的章节计划输出完整 HTML。要求：\n"
        "1) 仅输出 HTML，不要 markdown 代码块。\n"
        "2) 每个章节必须包含对应 slot 标记，格式严格为 <!-- BENOSS_SLOT:slot-id -->。\n"
        "3) slot 标记位置放在该章节正文末尾，用于后续自动注入图片/音频。\n"
        "4) 保持信息准确，避免臆造。\n\n"
        f"标题：{title}\n\n"
        f"章节计划：\n{sections_text}\n\n"
        f"输入记录：\n{records_text}"
    )

    clean_urls = [str(url or "").strip() for url in (image_urls or []) if str(url or "").strip()]
    user_content: str | list[dict] = user_text
    if clean_urls:
        parts: list[dict] = [
            {
                "type": "text",
                "text": (
                    f"{user_text}\n\n"
                    "附加图片作为事实线索。只能基于可见信息写作，不要臆造。"
                ),
            }
        ]
        for image_url in clean_urls:
            parts.append({"type": "image_url", "image_url": {"url": image_url}})
        user_content = parts

    output, settings = _ai_chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.26,
        max_tokens=2400,
    )
    return output, settings


def _collab_blog_review_feedback(*, plan: dict, blog_html: str) -> tuple[dict, dict]:
    system_prompt = "你是 Critic Agent。你只做质量审查，输出严格 JSON。"
    user_prompt = (
        "请审查以下博客 HTML 是否满足计划与事实约束。\n"
        "只输出 JSON：\n"
        '{\n'
        '  "approved": true,\n'
        '  "issues": ["最多 5 条问题"],\n'
        '  "rewrite_instruction": "若不通过，给出明确改写指令"\n'
        "}\n\n"
        f"章节计划：\n{_collab_plan_compact_text(plan)}\n\n"
        f"博客 HTML：\n{blog_html}"
    )
    output, settings = _ai_chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=900,
    )
    parsed = _parse_ai_json_object(output)
    approved = bool(parsed.get("approved"))
    issues = parsed.get("issues") if isinstance(parsed.get("issues"), list) else []
    rewrite_instruction = _trim_text_balanced(str(parsed.get("rewrite_instruction") or "").strip(), limit=1000)
    return {
        "approved": approved,
        "issues": [str(item or "").strip() for item in issues if str(item or "").strip()][:5],
        "rewrite_instruction": rewrite_instruction,
    }, settings


def _collab_blog_rewrite(
    *,
    title: str,
    plan: dict,
    records_text: str,
    current_html: str,
    review_feedback: dict,
) -> tuple[str, dict]:
    task = get_setting_str("PROMPT_NOTICE_BLOG_TASK", default=DEFAULT_NOTICE_BLOG_TASK).strip()
    issues = review_feedback.get("issues") or []
    instruction = str(review_feedback.get("rewrite_instruction") or "").strip()
    issue_text = "\n".join(f"- {str(item)}" for item in issues) if issues else "- 无显式问题列表"
    user_prompt = (
        f"{task}\n\n"
        "请基于审稿意见重写完整 HTML（只输出 HTML，不要代码块），并保留所有 slot 标记。\n"
        "slot 标记格式：<!-- BENOSS_SLOT:slot-id -->。\n\n"
        f"标题：{title}\n\n"
        f"章节计划：\n{_collab_plan_compact_text(plan)}\n\n"
        f"审稿问题：\n{issue_text}\n\n"
        f"改写指令：\n{instruction or '请在不改变事实的前提下优化结构与可读性。'}\n\n"
        f"输入记录：\n{records_text}\n\n"
        f"当前 HTML：\n{current_html}"
    )
    output, settings = _ai_chat(
        messages=[
            {"role": "system", "content": "你是 Writer Agent。你根据审稿意见迭代博客 HTML。"},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.24,
        max_tokens=2400,
    )
    return output, settings


def _ai_request_json(
    path: str,
    payload: dict,
    *,
    timeout: int | None = None,
    settings: dict | None = None,
) -> tuple[dict, dict]:
    settings = settings or _ai_provider_settings()
    if not settings:
        raise RuntimeError("AI provider not configured")

    endpoint = settings["base_url"] + path
    timeout_seconds = int(timeout or get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45))
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise RuntimeError(f"provider request error: {exc}") from exc

    if not response.ok:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 320:
            detail = detail[:320] + "..."
        raise RuntimeError(f"provider request failed ({response.status_code}): {detail}")

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("provider returned non-json payload") from exc
    return data, settings


def _ai_request_binary(
    path: str,
    payload: dict,
    *,
    timeout: int | None = None,
    settings: dict | None = None,
) -> tuple[bytes, str, dict]:
    settings = settings or _ai_provider_settings()
    if not settings:
        raise RuntimeError("AI provider not configured")

    endpoint = settings["base_url"] + path
    timeout_seconds = int(timeout or get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45))
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise RuntimeError(f"provider request error: {exc}") from exc

    if not response.ok:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 320:
            detail = detail[:320] + "..."
        raise RuntimeError(f"provider request failed ({response.status_code}): {detail}")

    content_type = str(response.headers.get("content-type") or "").strip().lower()
    if "application/json" in content_type:
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError("provider returned invalid json payload") from exc
        b64_value = ""
        if isinstance(data, dict):
            b64_value = str(data.get("audio") or data.get("b64_json") or data.get("data") or "").strip()
        if not b64_value:
            raise RuntimeError("provider returned json without audio bytes")
        try:
            audio_bytes = base64.b64decode(b64_value)
        except Exception as exc:
            raise RuntimeError("provider json audio decode failed") from exc
        return audio_bytes, content_type, settings

    return response.content, content_type, settings


def _download_binary_file(url: str, *, timeout: int) -> tuple[bytes, str]:
    remote_url = str(url or "").strip()
    if not remote_url:
        raise RuntimeError("download url is empty")
    try:
        response = requests.get(remote_url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"download failed: {exc}") from exc
    payload = response.content
    if not payload:
        raise RuntimeError("downloaded payload is empty")
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return payload, content_type


def _guess_content_type_and_ext(
    *,
    content_type: str,
    source_url: str = "",
    default_content_type: str,
    default_ext: str,
) -> tuple[str, str]:
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    clean_url = str(source_url or "").strip()
    ext = ""
    if clean_url:
        ext = Path(clean_url.split("?", 1)[0]).suffix.lower()
    if not ext and normalized_type:
        guessed = mimetypes.guess_extension(normalized_type) or ""
        ext = guessed.lower()
    if ext == ".jpe":
        ext = ".jpg"
    if not ext:
        ext = default_ext

    final_type = normalized_type
    if not final_type:
        guessed_type = mimetypes.guess_type(f"file{ext}")[0] if ext else ""
        final_type = str(guessed_type or default_content_type).split(";", 1)[0].strip().lower()
    if not final_type:
        final_type = default_content_type
    return final_type, ext


def _aliyun_native_generation_endpoint(settings: dict) -> str:
    base_url = str(settings.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    marker = "/compatible-mode/v1"
    lower = base_url.lower()
    index = lower.find(marker)
    if index >= 0:
        origin = base_url[:index].rstrip("/")
    else:
        origin = base_url.rstrip("/")
    if not origin:
        origin = "https://dashscope.aliyuncs.com"
    return origin + "/api/v1/services/aigc/multimodal-generation/generation"


def _aliyun_native_generation_request(
    *,
    payload: dict,
    timeout: int,
    settings: dict,
) -> tuple[dict, dict]:
    endpoint = _aliyun_native_generation_endpoint(settings)
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }
    response = None
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
            if response.status_code >= 500:
                continue
            break
        except requests.RequestException as exc:
            last_exc = exc
            continue
    if response is None:
        raise RuntimeError(f"provider request error: {last_exc}") from last_exc

    if not response.ok:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 320:
            detail = detail[:320] + "..."
        raise RuntimeError(f"provider request failed ({response.status_code}): {detail}")

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("provider returned non-json payload") from exc
    return data, settings


def _aliyun_tts_voice(voice: str) -> str:
    selected = str(voice or "").strip()
    if not selected:
        return "Cherry"
    if selected.lower() in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}:
        return "Cherry"
    return selected


def _ai_tts_audio_aliyun_native(
    *,
    settings: dict,
    text: str,
    voice: str,
    timeout: int,
) -> tuple[bytes, str, str, dict]:
    mapped_voice = _aliyun_tts_voice(voice)
    payload = {
        "model": settings["model"],
        "input": {
            "text": text,
            "voice": mapped_voice,
            "language_type": "Chinese",
        },
    }
    data, used = _aliyun_native_generation_request(payload=payload, timeout=timeout, settings=settings)
    output = data.get("output") if isinstance(data, dict) else {}
    audio_block = output.get("audio") if isinstance(output, dict) else {}
    if not isinstance(audio_block, dict):
        audio_block = {}

    audio_bytes = b""
    media_type = ""
    remote_url = str(audio_block.get("url") or "").strip()
    b64_audio = str(audio_block.get("data") or "").strip()

    if b64_audio:
        try:
            audio_bytes = base64.b64decode(b64_audio)
        except Exception as exc:
            raise RuntimeError(f"provider json audio decode failed: {exc}") from exc
    elif remote_url:
        audio_bytes, media_type = _download_binary_file(remote_url, timeout=timeout)
    else:
        raise RuntimeError("provider returned json without audio bytes/url")

    if not audio_bytes:
        raise RuntimeError("provider returned empty audio")

    content_type, ext = _guess_content_type_and_ext(
        content_type=media_type,
        source_url=remote_url,
        default_content_type="audio/wav",
        default_ext=".wav",
    )
    return (
        audio_bytes,
        content_type,
        ext,
        {
            "provider": used["provider"],
            "model": used["model"],
            "voice": mapped_voice,
        },
    )


def _ai_generate_poster_image_aliyun_native(
    *,
    settings: dict,
    prompt: str,
    timeout: int,
) -> tuple[bytes, str, str, dict]:
    payload = {
        "model": settings["model"],
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ]
        },
        "parameters": {
            "size": "1328*1328",
            "watermark": False,
            "prompt_extend": True,
        },
    }
    data, used = _aliyun_native_generation_request(payload=payload, timeout=timeout, settings=settings)
    output = data.get("output") if isinstance(data, dict) else {}
    image_url = ""

    if isinstance(output, dict):
        choices = output.get("choices") or []
        if isinstance(choices, list) and choices:
            first_choice = choices[0] if isinstance(choices[0], dict) else {}
            message = first_choice.get("message") if isinstance(first_choice, dict) else {}
            content_items = message.get("content") if isinstance(message, dict) else []
            if isinstance(content_items, list):
                for item in content_items:
                    if not isinstance(item, dict):
                        continue
                    candidate = str(item.get("image") or item.get("url") or item.get("image_url") or "").strip()
                    if candidate:
                        image_url = candidate
                        break
        if not image_url:
            image_url = str(output.get("url") or "").strip()

    if not image_url:
        raise RuntimeError("provider response missing image url")

    image_bytes, media_type = _download_binary_file(image_url, timeout=timeout)
    content_type, ext = _guess_content_type_and_ext(
        content_type=media_type,
        source_url=image_url,
        default_content_type="image/png",
        default_ext=".png",
    )
    return image_bytes, content_type, ext, {"provider": used["provider"], "model": used["model"]}


def _audio_format_meta(audio_format: str) -> tuple[str, str]:
    value = str(audio_format or "").strip().lower()
    mapping = {
        "mp3": ("audio/mpeg", ".mp3"),
        "wav": ("audio/wav", ".wav"),
        "aac": ("audio/aac", ".aac"),
        "flac": ("audio/flac", ".flac"),
        "opus": ("audio/opus", ".opus"),
        "pcm": ("audio/wav", ".wav"),
    }
    return mapping.get(value, ("audio/mpeg", ".mp3"))


def _local_tts_audio(text: str, *, voice: str) -> tuple[bytes, str, str, dict]:
    say_bin = shutil.which("say")
    if not say_bin:
        raise RuntimeError("local tts command `say` not found")

    content = str(text or "").strip()
    if not content:
        raise RuntimeError("local tts input is empty")

    tmp_path = ""
    with tempfile.NamedTemporaryFile(prefix="benoss-tts-", suffix=".aiff", delete=False) as tmp:
        tmp_path = tmp.name

    voice_value = str(voice or "").strip()
    attempts: list[list[str]] = []
    if voice_value:
        attempts.append([say_bin, "-v", voice_value, "-o", tmp_path, content])
    attempts.append([say_bin, "-o", tmp_path, content])

    last_error = ""
    try:
        for command in attempts:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                audio_bytes = Path(tmp_path).read_bytes()
                if not audio_bytes:
                    raise RuntimeError("local tts produced empty audio")
                return (
                    audio_bytes,
                    "audio/aiff",
                    ".aiff",
                    {
                        "provider": "local",
                        "model": "macos-say",
                        "voice": voice_value or "system-default",
                    },
                )
            stderr_text = str(result.stderr or "").strip()
            stdout_text = str(result.stdout or "").strip()
            last_error = stderr_text or stdout_text or f"exit code {result.returncode}"
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    raise RuntimeError(f"local tts failed: {last_error}")


def _ai_tts_audio(script: str, *, podcast_style: str) -> tuple[bytes, str, str, dict]:
    tts_voice = get_setting_str("AI_TTS_VOICE", default="alloy").strip() or "alloy"
    response_format = get_setting_str("AI_TTS_RESPONSE_FORMAT", default="mp3").strip().lower() or "mp3"
    max_chars = max(600, min(get_setting_int("AI_TTS_MAX_INPUT_CHARS", default=3600), 20000))
    safe_text = str(script or "").strip()
    if not safe_text:
        raise RuntimeError("podcast script is empty")
    if len(safe_text) > max_chars:
        safe_text = safe_text[:max_chars].rstrip()

    style = _normalize_podcast_style(podcast_style)
    style_hint = _PODCAST_STYLE_GUIDE.get(style, _PODCAST_STYLE_GUIDE["dialogue"])
    timeout = max(45, get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45))
    errors: list[str] = []

    for settings in _capability_settings_candidates("tts"):
        try:
            if settings.get("provider") == "aliyun":
                audio_bytes, content_type, ext, meta = _ai_tts_audio_aliyun_native(
                    settings=settings,
                    text=f"{style_hint}\n\n{safe_text}",
                    voice=tts_voice,
                    timeout=timeout,
                )
                return audio_bytes, content_type, ext, meta

            payload = {
                "model": settings["model"],
                "voice": tts_voice,
                "input": f"{style_hint}\n\n{safe_text}",
                "response_format": response_format,
            }
            audio_bytes, returned_content_type, used = _ai_request_binary(
                "/audio/speech",
                payload,
                timeout=timeout,
                settings=settings,
            )
        except RuntimeError as exc:
            errors.append(f"{settings['provider']}:{settings['model']} => {exc}")
            continue

        if not audio_bytes:
            errors.append(f"{settings['provider']}:{settings['model']} => empty audio")
            continue

        content_type, ext = _audio_format_meta(response_format)
        if returned_content_type and "application/json" not in returned_content_type:
            content_type = returned_content_type.split(";")[0].strip() or content_type
        return (
            audio_bytes,
            content_type,
            ext,
            {
                "provider": used["provider"],
                "model": used["model"],
                "voice": tts_voice,
            },
        )

    if get_setting_bool("AI_TTS_FALLBACK_LOCAL", default=True):
        try:
            return _local_tts_audio(f"{style_hint}\n\n{safe_text}", voice=tts_voice)
        except RuntimeError as exc:
            errors.append(f"local:macos-say => {exc}")

    if not errors:
        raise RuntimeError("TTS not configured for available providers")
    raise RuntimeError("TTS unavailable across providers: " + " | ".join(errors)[:2000])


def _ai_generate_poster_image(prompt: str) -> tuple[bytes, str, str, dict]:
    timeout = max(45, get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45))
    errors: list[str] = []

    for settings in _capability_settings_candidates("image"):
        try:
            if settings.get("provider") == "aliyun":
                return _ai_generate_poster_image_aliyun_native(
                    settings=settings,
                    prompt=prompt,
                    timeout=timeout,
                )

            payload = {
                "model": settings["model"],
                "prompt": prompt,
                "size": "1024x1024",
                "response_format": "b64_json",
            }
            data, used = _ai_request_json("/images/generations", payload, timeout=timeout, settings=settings)
        except RuntimeError as exc:
            errors.append(f"{settings['provider']}:{settings['model']} => {exc}")
            continue

        items = data.get("data") or []
        first = items[0] if items else {}
        b64_json = str(first.get("b64_json") or "").strip()
        url = str(first.get("url") or "").strip()

        if b64_json:
            try:
                image_bytes = base64.b64decode(b64_json)
            except Exception as exc:
                errors.append(f"{settings['provider']}:{settings['model']} => base64 decode failed: {exc}")
                continue
        elif url:
            try:
                response = requests.get(url, timeout=timeout)
                response.raise_for_status()
                image_bytes = response.content
            except requests.RequestException as exc:
                errors.append(f"{settings['provider']}:{settings['model']} => image download failed: {exc}")
                continue
        else:
            errors.append(f"{settings['provider']}:{settings['model']} => response missing b64_json/url")
            continue

        if not image_bytes:
            errors.append(f"{settings['provider']}:{settings['model']} => image bytes empty")
            continue

        return image_bytes, "image/png", ".png", {"provider": used["provider"], "model": used["model"]}

    if get_setting_bool("AI_IMAGE_FALLBACK_LOCAL", default=True):
        if errors:
            current_app.logger.warning("image generation failed, fallback to local svg: %s", " | ".join(errors)[:2000])
        else:
            current_app.logger.warning("image generation uses local svg fallback because no external image provider is available")
        image_bytes = _render_local_poster_svg(prompt)
        return image_bytes, "image/svg+xml", ".svg", {"provider": "local", "model": "local-svg-poster-v2"}

    if not errors:
        raise RuntimeError("image generation not configured for available providers")
    raise RuntimeError("image generation unavailable across providers: " + " | ".join(errors)[:2000])


def _wrap_lines(value: str, *, width: int, max_lines: int) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            if current.strip():
                chunks.append(current.strip())
            current = ""
            if len(chunks) >= max_lines:
                return chunks
            continue
        current += ch
        if len(current) >= width:
            chunks.append(current.strip())
            current = ""
            if len(chunks) >= max_lines:
                return chunks
    if current.strip() and len(chunks) < max_lines:
        chunks.append(current.strip())
    return chunks


def _poster_prompt_segments(prompt: str, *, limit: int = 10) -> list[str]:
    text = _normalize_prompt_text(prompt)
    if not text:
        return []
    text = re.sub(r"[`*_#>]", " ", text)
    parts = re.split(r"[\n，。；;、|/]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        item = str(raw or "").strip()
        if not item:
            continue
        item = re.sub(
            r"^(主题|排版|颜色|风格|元素|构图|文案|标题|副标题|画面|视觉焦点|配色|版式)\s*[：:]\s*",
            "",
            item,
        )
        item = re.sub(r"\s+", " ", item).strip(" -•·")
        if not item:
            continue
        token = item.lower()
        if token in seen:
            continue
        seen.add(token)
        out.append(item[:30])
        if len(out) >= limit:
            break
    return out


def _poster_title_and_subtitle(prompt: str) -> tuple[str, str]:
    parts = _poster_prompt_segments(prompt, limit=12)
    title = ""
    subtitle = ""

    for part in parts:
        if 4 <= len(part) <= 26:
            title = part
            break
    if not title and parts:
        title = parts[0][:24]
    if not title:
        title = "学习小组日报"

    for part in parts:
        if part == title:
            continue
        if 4 <= len(part) <= 34:
            subtitle = part
            break
    if not subtitle:
        subtitle = "用持续学习，走向更好的自己"

    return title[:26], subtitle[:34]


def _poster_badges(prompt: str, *, max_items: int = 3) -> list[str]:
    parts = _poster_prompt_segments(prompt, limit=24)
    blocked = {
        "主题",
        "排版",
        "颜色",
        "风格",
        "元素",
        "构图",
        "学习小组日报",
        "Benoss Local Poster Fallback".lower(),
    }
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = part.strip()
        if not item:
            continue
        key = item.lower()
        compact = re.sub(r"\s+", "", key)
        if compact in blocked or key in blocked or key in seen:
            continue
        seen.add(key)
        out.append(item[:10])
        if len(out) >= max_items:
            break
    if not out:
        out = ["学习计划", "行动复盘", "持续成长"][:max_items]
    return out


def _poster_image_prompt(prompt: str) -> str:
    text = _normalize_prompt_text(prompt)
    if not text:
        return (
            "学习小组主题海报，主视觉为正在协作学习的年轻人，背景有书本、便签与自然光，"
            "构图层次分明，色调明快，细节干净，画面中的文字极少，仅保留1-2行短标题。"
        )

    parts = _poster_prompt_segments(text, limit=12)
    body = "，".join(parts[:8]) if parts else _trim_text_balanced(text, limit=220)
    body = _normalize_prompt_text(body).replace("\n", "，").strip("， ")
    body = body[:320]
    if not body:
        body = "学习小组主题海报，人物主体清晰，场景真实，色彩层次丰富"

    return (
        f"{body}。"
        "海报需图像主导，主体与场景具象，构图有纵深，光影自然，质感清晰；"
        "画面文字极少，仅允许1-2行短标题，不要大段文字上墙。"
    )


def _render_local_poster_svg(prompt: str) -> bytes:
    prompt_text = _normalize_prompt_text(prompt)
    if not prompt_text:
        prompt_text = "Daily Digest Poster"

    palettes = [
        ("#102a43", "#1d4ed8", "#22d3ee", "#f59e0b", "#0f172a"),
        ("#0f3d2e", "#2a9d8f", "#4cc9f0", "#ffd166", "#102a43"),
        ("#1f2937", "#334155", "#38bdf8", "#f97316", "#0f172a"),
        ("#2f3e46", "#52796f", "#84a98c", "#f4a261", "#1b263b"),
    ]
    digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    palette = palettes[int(digest[:2], 16) % len(palettes)]
    bg_start, bg_end, accent_primary, accent_secondary, text_dark = palette
    seed = [int(digest[i : i + 2], 16) for i in range(2, 26, 2)]

    orb1_x = 120 + (seed[0] % 260)
    orb1_y = 150 + (seed[1] % 220)
    orb2_x = 660 + (seed[2] % 260)
    orb2_y = 180 + (seed[3] % 260)
    orb3_x = 780 + (seed[4] % 200)
    orb3_y = 760 + (seed[5] % 180)

    title, subtitle = _poster_title_and_subtitle(prompt_text)
    badges = _poster_badges(prompt_text, max_items=3)
    title_lines = _wrap_lines(title, width=12, max_lines=2) or ["学习小组日报"]
    subtitle_lines = _wrap_lines(subtitle, width=18, max_lines=2)

    title_items: list[str] = []
    title_start_y = 302
    title_gap = 76
    for idx, line in enumerate(title_lines):
        safe_line = html.escape(line)
        y_pos = title_start_y + idx * title_gap
        title_items.append(
            f'<text x="132" y="{y_pos}" font-size="66" font-weight="700" letter-spacing="1.2" fill="{text_dark}">{safe_line}</text>'
        )

    subtitle_items: list[str] = []
    subtitle_start_y = title_start_y + len(title_lines) * title_gap + 32
    for idx, line in enumerate(subtitle_lines):
        safe_line = html.escape(line)
        y_pos = subtitle_start_y + idx * 44
        subtitle_items.append(
            f'<text x="136" y="{y_pos}" font-size="30" fill="#334155" fill-opacity="0.95">{safe_line}</text>'
        )

    badge_items: list[str] = []
    badge_x = 132
    badge_y = subtitle_start_y + max(1, len(subtitle_lines)) * 44 + 34
    for badge in badges:
        safe_badge = html.escape(badge)
        badge_width = max(150, min(360, 72 + len(badge) * 26))
        if badge_x + badge_width > 900:
            badge_x = 132
            badge_y += 72
        badge_items.append(
            f'<rect x="{badge_x}" y="{badge_y}" width="{badge_width}" height="50" rx="25" fill="{accent_primary}" fill-opacity="0.14"/>'
        )
        badge_items.append(
            f'<text x="{badge_x + 24}" y="{badge_y + 34}" font-size="24" fill="{text_dark}" fill-opacity="0.92">{safe_badge}</text>'
        )
        badge_x += badge_width + 18

    visual_items = "\n    ".join(title_items + subtitle_items + badge_items)
    today_text = date.today().isoformat()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">\n'
        "  <defs>\n"
        '    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">\n'
        f'      <stop offset="0%" stop-color="{bg_start}"/>\n'
        f'      <stop offset="100%" stop-color="{bg_end}"/>\n'
        "    </linearGradient>\n"
        '    <radialGradient id="orbA" cx="50%" cy="50%" r="50%">\n'
        f'      <stop offset="0%" stop-color="{accent_primary}" stop-opacity="0.72"/>\n'
        f'      <stop offset="100%" stop-color="{accent_primary}" stop-opacity="0"/>\n'
        "    </radialGradient>\n"
        '    <radialGradient id="orbB" cx="50%" cy="50%" r="50%">\n'
        f'      <stop offset="0%" stop-color="{accent_secondary}" stop-opacity="0.52"/>\n'
        f'      <stop offset="100%" stop-color="{accent_secondary}" stop-opacity="0"/>\n'
        "    </radialGradient>\n"
        "  </defs>\n"
        '  <rect width="1024" height="1024" fill="url(#bg)"/>\n'
        f'  <circle cx="{orb1_x}" cy="{orb1_y}" r="220" fill="url(#orbA)"/>\n'
        f'  <circle cx="{orb2_x}" cy="{orb2_y}" r="280" fill="url(#orbB)"/>\n'
        f'  <circle cx="{orb3_x}" cy="{orb3_y}" r="260" fill="url(#orbA)"/>\n'
        f'  <path d="M70,760 C230,630 430,630 610,760 L610,1024 L70,1024 Z" fill="{accent_primary}" fill-opacity="0.12"/>\n'
        f'  <path d="M420,730 C610,590 860,620 980,760 L980,1024 L420,1024 Z" fill="{accent_secondary}" fill-opacity="0.12"/>\n'
        '  <rect x="80" y="92" width="864" height="840" rx="38" fill="#ffffff" fill-opacity="0.9"/>\n'
        '  <rect x="104" y="116" width="816" height="792" rx="30" fill="#ffffff" fill-opacity="0.72"/>\n'
        f'  <text x="132" y="186" font-size="28" letter-spacing="2" fill="{accent_primary}" fill-opacity="0.95">BENOSS VISUAL DIGEST</text>\n'
        f"  {visual_items}\n"
        f'  <circle cx="236" cy="796" r="84" fill="{accent_primary}" fill-opacity="0.16"/>\n'
        f'  <circle cx="352" cy="828" r="54" fill="{accent_secondary}" fill-opacity="0.2"/>\n'
        f'  <path d="M470 780 L530 846 L588 780" stroke="{text_dark}" stroke-width="14" stroke-linecap="round" stroke-linejoin="round" fill="none"/>\n'
        f'  <path d="M166 876 C286 816 406 816 526 876 L526 904 C406 844 286 844 166 904 Z" fill="{text_dark}" fill-opacity="0.1"/>\n'
        f'  <path d="M528 876 C648 816 768 816 888 876 L888 904 C768 844 648 844 528 904 Z" fill="{text_dark}" fill-opacity="0.1"/>\n'
        f'  <text x="132" y="958" font-size="24" fill="{text_dark}" fill-opacity="0.68">{today_text}</text>\n'
        f'  <text x="756" y="958" font-size="24" fill="{text_dark}" fill-opacity="0.68">fallback svg</text>\n'
        "</svg>\n"
    )
    return svg.encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _generated_asset_payload(asset: GeneratedAsset, *, viewer: User | None = None) -> dict:
    can_manage = _can_manage_generated_asset(asset, viewer)
    file_type = _rechecked_asset_file_type(asset)
    return {
        "id": asset.id,
        "kind": asset.kind,
        "title": asset.title,
        "visibility": asset.visibility or "private",
        "status": asset.status or "ready",
        "is_daily_digest": bool(asset.is_daily_digest),
        "source_day": asset.source_day.isoformat() if asset.source_day else None,
        "provider": asset.provider,
        "model": asset.model,
        "file_type": file_type,
        "content_type": asset.content_type,
        "ext": asset.ext,
        "sha256": asset.sha256,
        "user": {
            "id": asset.user.id if asset.user else asset.user_id,
            "username": asset.user.username if asset.user else "",
        },
        "size_bytes": int(asset.size_bytes or 0),
        "created_at": _iso(asset.created_at),
        "updated_at": _iso(asset.updated_at),
        "can_edit": can_manage,
        "can_delete": can_manage,
        "blob_url": url_for("api.generated_asset_blob", asset_id=asset.id),
    }


def _generated_asset_default_file_meta(kind: str) -> tuple[str, str]:
    normalized = str(kind or "").strip().lower()
    if normalized == "blog_html":
        return ".html", "text/html; charset=utf-8"
    if normalized == "podcast_audio":
        return ".mp3", "audio/mpeg"
    if normalized == "poster_image":
        return ".png", "image/png"
    return ".bin", "application/octet-stream"


def _generated_asset_file_matches_kind(kind: str, *, content_type: str | None, filename_or_ext: str | None) -> bool:
    inferred = _detect_asset_file_type_from_values(kind, content_type, filename_or_ext)
    normalized = str(kind or "").strip().lower()
    if normalized == "podcast_audio":
        return inferred == "audio"
    if normalized == "poster_image":
        return inferred == "image"
    if normalized == "blog_html":
        return inferred == "web"
    return True


def _generated_asset_file_error(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized == "blog_html":
        return "blog_html expects an HTML file"
    if normalized == "podcast_audio":
        return "podcast_audio expects an audio file"
    if normalized == "poster_image":
        return "poster_image expects an image file"
    return "invalid replacement file for generated asset"


def _save_generated_asset(
    *,
    user: User,
    kind: str,
    title: str,
    provider: str,
    model: str,
    content_type: str,
    ext: str,
    data: bytes,
    filters: dict,
    visibility: str = "private",
    source_day: date | None = None,
    is_daily_digest: bool = False,
    status: str = "ready",
) -> GeneratedAsset:
    safe_ext = ext if ext.startswith(".") else f".{ext}"
    filename = f"asset{safe_ext}"
    asset_uuid = new_uuid()
    key = generated_asset_key(user.id, kind, asset_uuid, filename)
    put_object_bytes(key, data, content_type=content_type)

    asset = GeneratedAsset(
        user_id=user.id,
        kind=kind,
        title=(title or "").strip()[:255],
        provider=(provider or "").strip()[:64],
        model=(model or "").strip()[:128],
        visibility=_normalize_visibility(visibility, default="private"),
        status=(status or "ready").strip()[:16] or "ready",
        is_daily_digest=bool(is_daily_digest),
        source_day=source_day,
        file_type=_detect_asset_file_type_from_values(kind, (content_type or "").strip()[:255], safe_ext[:16]),
        content_type=(content_type or "").strip()[:255],
        ext=safe_ext[:16],
        size_bytes=len(data),
        oss_key=key,
        sha256=_sha256_bytes(data),
        source_filters_json=json.dumps(filters or {}, ensure_ascii=False),
    )
    db.session.add(asset)
    db.session.commit()
    return asset


def _strip_markdown_code_fence(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    match = _MARKDOWN_CODE_FENCE_RE.match(raw)
    if match:
        return str(match.group(1) or "").strip()

    if "```" in raw:
        blocks = [str(item or "").strip() for item in _MARKDOWN_CODE_BLOCK_RE.findall(raw)]
        html_blocks = [item for item in blocks if "<" in item and ">" in item]
        if html_blocks:
            return max(html_blocks, key=len)
        lines = [line for line in raw.splitlines() if not line.strip().startswith("```")]
        compact = "\n".join(lines).strip()
        if compact:
            return compact
    return raw


def _inject_html_before_closing_tag(doc_html: str, snippet_html: str, tag_name: str) -> str:
    raw = str(doc_html or "")
    snippet = str(snippet_html or "").strip()
    closing = str(tag_name or "").strip().lower()
    if not raw or not snippet or not closing:
        return raw
    marker = f"</{closing}>"
    lower_raw = raw.lower()
    index = lower_raw.rfind(marker)
    if index < 0:
        return raw
    return f"{raw[:index]}\n{snippet}\n{raw[index:]}"


def _inject_html_after_opening_tag(doc_html: str, snippet_html: str, tag_name: str) -> str:
    raw = str(doc_html or "")
    snippet = str(snippet_html or "").strip()
    opening = str(tag_name or "").strip().lower()
    if not raw or not snippet or not opening:
        return raw

    pattern = re.compile(rf"<{re.escape(opening)}\b[^>]*>", re.IGNORECASE)
    match = pattern.search(raw)
    if not match:
        return raw
    index = match.end()
    return f"{raw[:index]}\n{snippet}\n{raw[index:]}"


def _prefer_oss_media_src(
    *,
    oss_key: str | None,
    fallback_src: str,
    expires: int = _BLOG_MEDIA_SIGNED_URL_EXPIRES_SECONDS,
) -> tuple[str, str]:
    fallback = str(fallback_src or "").strip()
    if not fallback:
        return "", ""
    signed = _signed_url_if_enabled(
        oss_key,
        enabled=True,
        expires=_normalize_signed_url_expires(expires, default=_BLOG_MEDIA_SIGNED_URL_EXPIRES_SECONDS),
    )
    primary = signed or fallback
    backup = fallback if signed and signed != fallback else ""
    return primary, backup


def _generated_asset_media_src(asset: GeneratedAsset | None) -> tuple[str, str]:
    if not asset or not asset.id:
        return "", ""
    fallback = f"/api/generated-assets/{int(asset.id)}/blob"
    return _prefer_oss_media_src(oss_key=asset.oss_key, fallback_src=fallback)


def _generated_asset_media_src_by_id(asset_id: int | None) -> tuple[str, str]:
    if not asset_id:
        return "", ""
    fallback = f"/api/generated-assets/{int(asset_id)}/blob"
    asset = GeneratedAsset.query.filter(GeneratedAsset.id == int(asset_id)).first()
    oss_key = str(asset.oss_key or "").strip() if asset else ""
    return _prefer_oss_media_src(oss_key=oss_key, fallback_src=fallback)


def _digest_image_tag(*, src: str, alt: str, fallback_src: str = "") -> str:
    primary = str(src or "").strip()
    if not primary:
        return ""

    attrs = [
        f'src="{html.escape(primary, quote=True)}"',
        f'alt="{html.escape(alt or "", quote=True)}"',
    ]
    fallback = str(fallback_src or "").strip()
    if fallback and fallback != primary:
        attrs.append(f'data-fallback-src="{html.escape(fallback, quote=True)}"')
        attrs.append(
            'onerror="if(this.dataset.fallbackTried!==\'1\'&&this.dataset.fallbackSrc){'
            "this.dataset.fallbackTried='1';this.src=this.dataset.fallbackSrc;}"
            '"'
        )
    return f"<img {' '.join(attrs)}>"


def _digest_audio_tag(*, src: str, fallback_src: str = "") -> str:
    primary = str(src or "").strip()
    if not primary:
        return ""

    fallback = str(fallback_src or "").strip()
    primary_escaped = html.escape(primary, quote=True)
    if fallback and fallback != primary:
        fallback_escaped = html.escape(fallback, quote=True)
        return (
            f"<audio controls preload=\"none\" data-fallback-src=\"{fallback_escaped}\" "
            "onerror=\"if(this.dataset.fallbackTried!=='1'&&this.dataset.fallbackSrc){"
            "this.dataset.fallbackTried='1';this.src=this.dataset.fallbackSrc;this.load();}\">"
            f"<source src=\"{primary_escaped}\">"
            f"<source src=\"{fallback_escaped}\">"
            "</audio>"
        )
    return f'<audio controls preload="none" src="{primary_escaped}"></audio>'


def _digest_video_tag(*, src: str, fallback_src: str = "") -> str:
    primary = str(src or "").strip()
    if not primary:
        return ""

    fallback = str(fallback_src or "").strip()
    primary_escaped = html.escape(primary, quote=True)
    if fallback and fallback != primary:
        fallback_escaped = html.escape(fallback, quote=True)
        return (
            f"<video controls preload=\"metadata\" data-fallback-src=\"{fallback_escaped}\" "
            "onerror=\"if(this.dataset.fallbackTried!=='1'&&this.dataset.fallbackSrc){"
            "this.dataset.fallbackTried='1';this.src=this.dataset.fallbackSrc;this.load();}\">"
            f"<source src=\"{primary_escaped}\">"
            f"<source src=\"{fallback_escaped}\">"
            "</video>"
        )
    return f'<video controls preload="metadata" src="{primary_escaped}"></video>'


def _digest_media_section_html(*, poster_asset_id: int | None, podcast_asset_id: int | None) -> str:
    poster_src, poster_fallback = _generated_asset_media_src_by_id(poster_asset_id)
    podcast_src, podcast_fallback = _generated_asset_media_src_by_id(podcast_asset_id)
    if not poster_src and not podcast_src:
        return ""

    items: list[str] = ['<section class="benoss-media-appendix">']
    if poster_src:
        poster_tag = _digest_image_tag(src=poster_src, alt="日报海报", fallback_src=poster_fallback)
        items.extend(
            [
                "<figure>",
                f"  {poster_tag}",
                "  <figcaption>日报配图</figcaption>",
                "</figure>",
            ]
        )
    if podcast_src:
        podcast_tag = _digest_audio_tag(src=podcast_src, fallback_src=podcast_fallback)
        items.extend(
            [
                "<div>",
                "  <h3>日报播客</h3>",
                f"  {podcast_tag}",
                "</div>",
            ]
        )
    items.append("</section>")
    return "\n".join(items)


def _digest_media_section_assets_html(
    *,
    poster_assets: list[GeneratedAsset],
    podcast_assets: list[GeneratedAsset],
    source_images: list[dict] | None = None,
    source_audios: list[dict] | None = None,
    source_videos: list[dict] | None = None,
    heading: str = "日报多媒体",
    class_name: str = "benoss-media-appendix",
) -> str:
    posters = [item for item in poster_assets if item]
    podcasts = [item for item in podcast_assets if item]
    image_sources = [item for item in (source_images or []) if isinstance(item, dict)]
    audio_sources = [item for item in (source_audios or []) if isinstance(item, dict)]
    video_sources = [item for item in (source_videos or []) if isinstance(item, dict)]
    if not posters and not podcasts and not image_sources and not audio_sources and not video_sources:
        return ""

    class_token = html.escape((class_name or "benoss-media-appendix").strip(), quote=True)
    heading_text = html.escape((heading or "").strip(), quote=False)
    lines: list[str] = [f'<section class="{class_token}">']
    if heading_text:
        lines.append(f"  <h3>{heading_text}</h3>")

    for source in image_sources:
        src = str(source.get("src") or source.get("fallback_src") or "").strip()
        if not src:
            continue
        fallback_src = str(source.get("fallback_src") or "").strip()
        title_raw = str(source.get("title") or "").strip() or "原图"
        caption_raw = str(source.get("caption") or "").strip() or title_raw
        title = html.escape(title_raw, quote=False)
        caption = html.escape(caption_raw, quote=False)
        image_tag = _digest_image_tag(src=src, alt=title_raw, fallback_src=fallback_src)
        lines.extend(
            [
                "  <figure>",
                f"    {image_tag}",
                f"    <figcaption>{caption}</figcaption>",
                "  </figure>",
            ]
        )

    for source in audio_sources:
        src = str(source.get("src") or source.get("fallback_src") or "").strip()
        if not src:
            continue
        fallback_src = str(source.get("fallback_src") or "").strip()
        title_raw = str(source.get("title") or "").strip() or "原始音频"
        caption_raw = str(source.get("caption") or "").strip() or title_raw
        title = html.escape(title_raw, quote=False)
        caption = html.escape(caption_raw, quote=False)
        audio_tag = _digest_audio_tag(src=src, fallback_src=fallback_src)
        lines.extend(
            [
                '  <div class="benoss-media-audio-item">',
                f"    <h4>{title}</h4>",
                f"    {audio_tag}",
                f"    <p>{caption}</p>",
                "  </div>",
            ]
        )

    for source in video_sources:
        src = str(source.get("src") or source.get("fallback_src") or "").strip()
        if not src:
            continue
        fallback_src = str(source.get("fallback_src") or "").strip()
        title_raw = str(source.get("title") or "").strip() or "原始视频"
        caption_raw = str(source.get("caption") or "").strip() or title_raw
        title = html.escape(title_raw, quote=False)
        caption = html.escape(caption_raw, quote=False)
        video_tag = _digest_video_tag(src=src, fallback_src=fallback_src)
        lines.extend(
            [
                '  <div class="benoss-media-video-item">',
                f"    <h4>{title}</h4>",
                f"    {video_tag}",
                f"    <p>{caption}</p>",
                "  </div>",
            ]
        )

    for asset in posters:
        src, fallback_src = _generated_asset_media_src(asset)
        if not src:
            continue
        caption_raw = str(asset.title or "").strip() or "日报配图"
        caption = html.escape(caption_raw, quote=False)
        image_tag = _digest_image_tag(src=src, alt=caption_raw, fallback_src=fallback_src)
        lines.extend(
            [
                "  <figure>",
                f"    {image_tag}",
                f"    <figcaption>{caption}</figcaption>",
                "  </figure>",
            ]
        )

    for asset in podcasts:
        src, fallback_src = _generated_asset_media_src(asset)
        if not src:
            continue
        title_raw = str(asset.title or "").strip() or "日报播客"
        title = html.escape(title_raw, quote=False)
        audio_tag = _digest_audio_tag(src=src, fallback_src=fallback_src)
        lines.extend(
            [
                '  <div class="benoss-media-audio-item">',
                f"    <h4>{title}</h4>",
                f"    {audio_tag}",
                "  </div>",
            ]
        )

    lines.append("</section>")
    return "\n".join(lines)


def _asset_filters(asset: GeneratedAsset) -> dict:
    raw = str(asset.source_filters_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _asset_slot_name(asset: GeneratedAsset) -> str:
    filters = _asset_filters(asset)
    slot_raw = str(filters.get("slot") or "").strip()
    if not slot_raw:
        return ""
    return _normalize_slot_name(slot_raw, fallback_index=1)


def _inject_slot_media_markers(
    blog_html: str,
    *,
    slot_media_html: dict[str, str],
) -> tuple[str, set[str]]:
    used_slots: set[str] = set()

    def _replace(match: re.Match) -> str:
        slot_raw = str(match.group(1) or "").strip()
        slot = _normalize_slot_name(slot_raw, fallback_index=1)
        block = str(slot_media_html.get(slot) or "").strip()
        if block:
            used_slots.add(slot)
            return block
        return ""

    output = _BLOG_SLOT_MARKER_RE.sub(_replace, str(blog_html or ""))
    return output, used_slots


def _strip_digest_media_section(doc_html: str) -> str:
    raw = str(doc_html or "")
    if not raw:
        return raw
    return _DIGEST_MEDIA_APPENDIX_RE.sub("", raw).strip()


def _refresh_existing_blog_asset_media(
    asset: GeneratedAsset,
    *,
    poster_asset_id: int | None,
    podcast_asset_id: int | None,
    extra_media_html: str = "",
) -> bool:
    if str(asset.kind or "").strip().lower() != "blog_html":
        return False
    key = str(asset.oss_key or "").strip()
    if not key:
        return False

    original_bytes = get_object_bytes(key)
    original_html = original_bytes.decode("utf-8", errors="replace")

    cleaned_html = _strip_digest_media_section(original_html)
    refreshed_html = _wrap_blog_html_document(
        cleaned_html,
        title=asset.title or "Daily Digest",
        poster_asset_id=poster_asset_id,
        podcast_asset_id=podcast_asset_id,
        extra_media_html=extra_media_html,
    )
    refreshed_bytes = refreshed_html.encode("utf-8")
    if refreshed_bytes == original_bytes:
        return True

    put_object_bytes(key, refreshed_bytes, content_type="text/html; charset=utf-8")
    asset.content_type = "text/html; charset=utf-8"
    asset.ext = ".html"
    asset.file_type = _detect_asset_file_type_from_values("blog_html", asset.content_type, asset.ext)
    asset.size_bytes = len(refreshed_bytes)
    asset.sha256 = _sha256_bytes(refreshed_bytes)
    db.session.add(asset)
    db.session.commit()
    return True


def _wrap_blog_html_document(
    body_html: str,
    *,
    title: str,
    poster_asset_id: int | None = None,
    podcast_asset_id: int | None = None,
    extra_media_html: str = "",
) -> str:
    raw = _strip_markdown_code_fence(body_html)
    primary_media_html = _digest_media_section_html(
        poster_asset_id=poster_asset_id,
        podcast_asset_id=podcast_asset_id,
    )
    extra_media = str(extra_media_html or "").strip()
    media_parts = [part for part in [extra_media, primary_media_html] if part]
    media_html = "\n".join(media_parts).strip()
    if "<html" in raw.lower():
        doc = raw
        if media_html:
            for tag in ("main", "article", "body"):
                updated = _inject_html_after_opening_tag(doc, media_html, tag)
                if updated != doc:
                    doc = updated
                    break
            else:
                for tag in ("body", "html"):
                    updated = _inject_html_before_closing_tag(doc, media_html, tag)
                    if updated != doc:
                        doc = updated
                        break
        return doc
    safe_title = html.escape(title or "Daily Digest")
    media_part = f"{media_html}\n" if media_html else ""
    return (
        "<!doctype html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>{safe_title}</title>\n"
        "  <style>\n"
        "    body { margin: 0; font-family: \"Noto Sans SC\", \"PingFang SC\", sans-serif; background: #f4f8fb; color: #1b2430; }\n"
        "    main { width: min(980px, 94vw); margin: 28px auto; background: #fff; border: 1px solid #d2dee8; border-radius: 14px; padding: 18px 20px; }\n"
        "    h1, h2, h3 { line-height: 1.35; }\n"
        "    pre { white-space: pre-wrap; }\n"
        "    img, video, audio { max-width: 100%; border-radius: 10px; }\n"
        "    .benoss-media-appendix, .benoss-slot-media { margin-bottom: 24px; padding-bottom: 14px; border-bottom: 1px solid #dbe5ef; }\n"
        "    .benoss-media-appendix h3, .benoss-slot-media h3 { margin: 12px 0 8px; font-size: 1rem; }\n"
        "    .benoss-media-appendix h4, .benoss-slot-media h4 { margin: 10px 0 6px; font-size: 0.94rem; color: #334155; }\n"
        "    .benoss-media-appendix figure, .benoss-slot-media figure { margin: 0 auto 12px; width: min(560px, 100%); }\n"
        "    .benoss-media-appendix img, .benoss-slot-media img { display: block; width: 100%; height: auto; border-radius: 10px; }\n"
        "    .benoss-media-appendix audio, .benoss-slot-media audio { width: min(560px, 100%); }\n"
        "    .benoss-media-appendix video, .benoss-slot-media video { width: min(560px, 100%); }\n"
        "    .benoss-media-appendix figcaption, .benoss-slot-media figcaption { margin-top: 6px; color: #64748b; font-size: 0.92rem; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <main>\n"
        f"{media_part}"
        f"{raw}\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )


def _generate_blog_asset(
    *,
    user: User,
    records_text: str,
    image_urls: list[str] | None,
    filters: dict,
    title: str,
    visibility: str,
    poster_asset_id: int | None = None,
    podcast_asset_id: int | None = None,
    extra_media_html: str = "",
    source_day: date | None = None,
    is_daily_digest: bool = False,
) -> tuple[GeneratedAsset, str]:
    output, settings = _ai_chat(
        messages=_build_notice_ai_prompt("blog", records_text, image_urls=image_urls),
        temperature=0.25,
        max_tokens=2000,
    )
    html_doc = _wrap_blog_html_document(
        output,
        title=title,
        poster_asset_id=poster_asset_id,
        podcast_asset_id=podcast_asset_id,
        extra_media_html=extra_media_html,
    )
    asset = _save_generated_asset(
        user=user,
        kind="blog_html",
        title=title,
        provider=settings["provider"],
        model=settings["model"],
        content_type="text/html; charset=utf-8",
        ext=".html",
        data=html_doc.encode("utf-8"),
        filters=filters,
        visibility=visibility,
        source_day=source_day,
        is_daily_digest=is_daily_digest,
    )
    return asset, output


def _generate_podcast_asset(
    *,
    user: User,
    records_text: str,
    image_urls: list[str] | None,
    filters: dict,
    title: str,
    visibility: str,
    podcast_style: str = "dialogue",
    source_day: date | None = None,
    is_daily_digest: bool = False,
) -> tuple[GeneratedAsset, str]:
    style = _normalize_podcast_style(podcast_style)
    script, script_info = _ai_chat(
        messages=_build_notice_ai_prompt("podcast", records_text, podcast_style=style, image_urls=image_urls),
        temperature=0.42,
        max_tokens=1900,
    )
    audio_bytes, audio_type, ext, audio_info = _ai_tts_audio(script, podcast_style=style)
    asset = _save_generated_asset(
        user=user,
        kind="podcast_audio",
        title=title,
        provider=audio_info["provider"],
        model=f"{script_info['model']} + {audio_info['model']}",
        content_type=audio_type,
        ext=ext,
        data=audio_bytes,
        filters=filters,
        visibility=visibility,
        source_day=source_day,
        is_daily_digest=is_daily_digest,
    )
    return asset, script


def _generate_poster_asset(
    *,
    user: User,
    records_text: str,
    image_urls: list[str] | None,
    filters: dict,
    title: str,
    visibility: str,
    source_day: date | None = None,
    is_daily_digest: bool = False,
) -> tuple[GeneratedAsset, str]:
    poster_system_prompt = get_setting_str("PROMPT_POSTER_SYSTEM", default=DEFAULT_POSTER_SYSTEM_PROMPT)
    poster_user_template = get_setting_str(
        "PROMPT_POSTER_USER_TEMPLATE",
        default=DEFAULT_POSTER_USER_TEMPLATE,
    )
    poster_user_prompt = format_prompt_template(poster_user_template, records_text=records_text)
    poster_user_content: str | list[dict] = poster_user_prompt
    clean_urls = [str(url or "").strip() for url in (image_urls or []) if str(url or "").strip()]
    if clean_urls:
        parts: list[dict] = [
            {
                "type": "text",
                "text": (
                    f"{poster_user_prompt}\n\n"
                    "附加图片已提供，可辅助理解场景。请严格基于图片可见信息与记录内容，不要臆造。"
                ),
            }
        ]
        for image_url in clean_urls:
            parts.append({"type": "image_url", "image_url": {"url": image_url}})
        poster_user_content = parts
    poster_prompt_messages = [
        {
            "role": "system",
            "content": poster_system_prompt,
        },
        {
            "role": "user",
            "content": poster_user_content,
        },
    ]
    poster_prompt_raw, prompt_settings = _ai_chat(
        messages=poster_prompt_messages,
        temperature=0.45,
        max_tokens=700,
    )
    poster_prompt = _poster_image_prompt(poster_prompt_raw)
    image_bytes, image_type, ext, image_info = _ai_generate_poster_image(poster_prompt)
    asset = _save_generated_asset(
        user=user,
        kind="poster_image",
        title=title,
        provider=image_info["provider"],
        model=f"{prompt_settings['model']} + {image_info['model']}",
        content_type=image_type,
        ext=ext,
        data=image_bytes,
        filters=filters,
        visibility=visibility,
        source_day=source_day,
        is_daily_digest=is_daily_digest,
    )
    return asset, poster_prompt


def build_daily_public_digest(*, day_value: date, force: bool = False, timezone_name: str | None = None) -> dict:
    tz_name = str(timezone_name or _digest_timezone()).strip() or "Asia/Shanghai"
    start, end = _utc_bounds_for_local_day(day_value, tz_name)

    job = DailyDigestJob.query.filter_by(day=day_value, timezone=tz_name).first()
    if not job:
        job = DailyDigestJob(day=day_value, timezone=tz_name)
    job.status = "running"
    job.started_at = datetime.utcnow()
    job.finished_at = None
    job.error = ""
    job.blog_asset_id = None
    job.podcast_asset_id = None
    job.poster_asset_id = None
    db.session.add(job)
    db.session.commit()

    records = (
        Record.query.options(joinedload(Record.user), joinedload(Record.content))
        .filter(
            Record.visibility == "public",
            Record.created_at >= start,
            Record.created_at < end,
        )
        .order_by(Record.created_at.asc(), Record.id.asc())
        .all()
    )
    if not records:
        retention_info = apply_archive_retention_policy()
        vector_info = index_meta()
        if (
            int(retention_info.get("deleted_count") or 0) > 0
            and get_setting_bool("VECTOR_AUTO_REBUILD", default=True)
        ):
            try:
                vector_info = build_index(max_docs=get_setting_int("VECTOR_MAX_DOCS", default=4000))
            except Exception as exc:
                current_app.logger.warning("vector rebuild failed after retention cleanup: %s", exc)
        job.status = "ready"
        job.finished_at = datetime.utcnow()
        job.error = "no public records"
        db.session.add(job)
        db.session.commit()
        return {
            "day": day_value.isoformat(),
            "timezone": tz_name,
            "record_count": 0,
            "status": job.status,
            "error": job.error,
            "assets": [],
            "retention": retention_info,
            "vector": vector_info,
        }

    _archive_and_index_records(
        day_value=day_value,
        records=records,
        scope="public",
        source="daily_digest",
        timezone_name=tz_name,
    )
    archive_rows = _archive_rows_for_day(day_value)
    generation_limit = max(20, min(get_setting_int("AI_MAX_NOTICE_RECORDS", default=180), 500))
    rows_for_generation = archive_rows[:generation_limit] if archive_rows else []
    records_text = _records_for_ai_prompt_from_archive(rows_for_generation)
    image_urls = _notice_image_urls_from_archive_rows(rows_for_generation)
    source_images = _source_images_from_records(records, max_items=_digest_collab_max_source_images())
    source_audios = _source_audios_from_records(records, max_items=_digest_collab_max_source_images())
    source_videos = _source_videos_from_records(records, max_items=_digest_collab_max_source_images())
    source_media_html = _digest_media_section_assets_html(
        poster_assets=[],
        podcast_assets=[],
        source_images=[],
        source_audios=source_audios,
        source_videos=source_videos,
        heading="记录原始媒体",
        class_name="benoss-media-appendix",
    )
    record_count_total = len(archive_rows) if archive_rows else len(records)
    if not records_text:
        records_text = _records_for_ai_prompt(records[:generation_limit])
    if not records_text:
        job.status = "failed"
        job.finished_at = datetime.utcnow()
        job.error = "public records are empty after prompt reduction"
        db.session.add(job)
        db.session.commit()
        return {
            "day": day_value.isoformat(),
            "timezone": tz_name,
            "record_count": record_count_total,
            "status": job.status,
            "error": job.error,
            "assets": [],
        }

    owner = _digest_owner_user()
    base_filters = {
        "day": day_value.isoformat(),
        "scope": "public",
        "timezone": tz_name,
        "job": "daily_digest",
    }
    title_map = {
        "blog_html": f"Daily Digest Blog {day_value.isoformat()}",
        "podcast_audio": f"Daily Digest Podcast {day_value.isoformat()}",
        "poster_image": f"Daily Digest Poster {day_value.isoformat()}",
    }
    errors: list[str] = []

    def _list_existing_assets(kind: str) -> list[GeneratedAsset]:
        if force:
            return []
        return (
            GeneratedAsset.query.options(joinedload(GeneratedAsset.user))
            .filter(
                GeneratedAsset.kind == kind,
                GeneratedAsset.visibility == "public",
                GeneratedAsset.status == "ready",
                GeneratedAsset.is_daily_digest.is_(True),
                GeneratedAsset.source_day == day_value,
            )
            .order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id.desc())
            .all()
        )

    existing_by_kind = {
        "blog_html": _list_existing_assets("blog_html"),
        "podcast_audio": _list_existing_assets("podcast_audio"),
        "poster_image": _list_existing_assets("poster_image"),
    }

    blog_asset: GeneratedAsset | None = None
    podcast_assets: list[GeneratedAsset] = []
    poster_assets: list[GeneratedAsset] = []

    existing_blog = existing_by_kind.get("blog_html") or []
    if existing_blog and not force:
        blog_asset = existing_blog[0]
        podcast_assets = list(existing_by_kind.get("podcast_audio") or [])
        poster_assets = list(existing_by_kind.get("poster_image") or [])
        try:
            _refresh_existing_blog_asset_media(
                blog_asset,
                poster_asset_id=poster_assets[0].id if poster_assets else None,
                podcast_asset_id=podcast_assets[0].id if podcast_assets else None,
                extra_media_html=source_media_html,
            )
        except Exception as exc:
            current_app.logger.warning("daily digest blog media refresh failed: %s", exc)
    else:
        if not _digest_collab_enabled():
            if _digest_collab_max_image_assets() > 0:
                try:
                    generated_poster, _ = _generate_poster_asset(
                        user=owner,
                        records_text=records_text,
                        image_urls=image_urls,
                        filters=dict(base_filters, pipeline="legacy"),
                        title=title_map["poster_image"],
                        visibility="public",
                        source_day=day_value,
                        is_daily_digest=True,
                    )
                    poster_assets.append(generated_poster)
                except Exception as exc:
                    errors.append(f"poster_image: {exc}")

            if _digest_collab_max_audio_assets() > 0:
                try:
                    generated_audio, _ = _generate_podcast_asset(
                        user=owner,
                        records_text=records_text,
                        image_urls=image_urls,
                        filters=dict(base_filters, pipeline="legacy"),
                        title=title_map["podcast_audio"],
                        visibility="public",
                        source_day=day_value,
                        is_daily_digest=True,
                    )
                    podcast_assets.append(generated_audio)
                except Exception as exc:
                    errors.append(f"podcast_audio: {exc}")

            try:
                blog_asset, _ = _generate_blog_asset(
                    user=owner,
                    records_text=records_text,
                    image_urls=image_urls,
                    filters=dict(base_filters, pipeline="legacy"),
                    title=title_map["blog_html"],
                    visibility="public",
                    poster_asset_id=poster_assets[0].id if poster_assets else None,
                    podcast_asset_id=podcast_assets[0].id if podcast_assets else None,
                    extra_media_html=source_media_html,
                    source_day=day_value,
                    is_daily_digest=True,
                )
            except Exception as exc:
                errors.append(f"blog_html: {exc}")
        else:
            plan, planner_settings = _build_digest_collab_plan(records_text)
            sections_raw = plan.get("sections") or []
            sections = sections_raw if isinstance(sections_raw, list) else []
            slot_source_image_ids = _assign_source_images_to_slots(plan=plan, source_images=source_images)
            source_image_index = {
                str(item.get("id")): item
                for item in source_images
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
            slot_media_assets: dict[str, dict[str, list[GeneratedAsset]]] = {}

            for idx, section in enumerate(sections, start=1):
                if not isinstance(section, dict):
                    continue
                slot = _normalize_slot_name(section.get("slot"), fallback_index=idx)
                section_title = str(section.get("title") or f"章节 {idx}").strip()[:80] or f"章节 {idx}"
                section_focus = _trim_text_balanced(str(section.get("focus") or section_title), limit=1200)
                image_count = max(0, min(_safe_int(section.get("image_count"), default=0), 4))
                audio_count = max(0, min(_safe_int(section.get("audio_count"), default=0), 3))
                slot_media_assets.setdefault(slot, {"images": [], "audios": []})
                section_context = _trim_text_balanced(
                    f"章节标题：{section_title}\n章节重点：{section_focus}\n\n上下文记录：\n{records_text}",
                    limit=5600,
                )

                for media_index in range(image_count):
                    media_filters = dict(base_filters)
                    media_filters.update(
                        {
                            "pipeline": "collab",
                            "slot": slot,
                            "slot_title": section_title,
                            "slot_index": idx,
                            "media_kind": "poster_image",
                            "media_index": media_index + 1,
                        }
                    )
                    media_title = f"{title_map['poster_image']} - {section_title} #{media_index + 1}"
                    try:
                        generated_poster, _ = _generate_poster_asset(
                            user=owner,
                            records_text=section_context,
                            image_urls=image_urls,
                            filters=media_filters,
                            title=media_title[:255],
                            visibility="public",
                            source_day=day_value,
                            is_daily_digest=True,
                        )
                        slot_media_assets[slot]["images"].append(generated_poster)
                        poster_assets.append(generated_poster)
                    except RuntimeError as exc:
                        errors.append(f"poster_image[{slot}#{media_index + 1}]: {exc}")
                    except Exception as exc:
                        errors.append(f"poster_image[{slot}#{media_index + 1}]: {exc}")

                for media_index in range(audio_count):
                    media_filters = dict(base_filters)
                    media_filters.update(
                        {
                            "pipeline": "collab",
                            "slot": slot,
                            "slot_title": section_title,
                            "slot_index": idx,
                            "media_kind": "podcast_audio",
                            "media_index": media_index + 1,
                        }
                    )
                    media_title = f"{title_map['podcast_audio']} - {section_title} #{media_index + 1}"
                    try:
                        generated_audio, _ = _generate_podcast_asset(
                            user=owner,
                            records_text=section_context,
                            image_urls=image_urls,
                            filters=media_filters,
                            title=media_title[:255],
                            visibility="public",
                            source_day=day_value,
                            is_daily_digest=True,
                        )
                        slot_media_assets[slot]["audios"].append(generated_audio)
                        podcast_assets.append(generated_audio)
                    except RuntimeError as exc:
                        errors.append(f"podcast_audio[{slot}#{media_index + 1}]: {exc}")
                    except Exception as exc:
                        errors.append(f"podcast_audio[{slot}#{media_index + 1}]: {exc}")

            primary_poster_id = poster_assets[0].id if poster_assets else None
            primary_podcast_id = podcast_assets[0].id if podcast_assets else None

            try:
                blog_draft, writer_settings = _build_collab_blog_html(
                    title=title_map["blog_html"],
                    records_text=records_text,
                    image_urls=image_urls,
                    plan=plan,
                )
                iter_html = blog_draft
                for round_index in range(_digest_collab_review_rounds()):
                    try:
                        review_feedback, _ = _collab_blog_review_feedback(plan=plan, blog_html=iter_html)
                    except Exception as exc:
                        errors.append(f"critic_round_{round_index + 1}: {exc}")
                        break
                    if review_feedback.get("approved"):
                        break
                    try:
                        iter_html, _ = _collab_blog_rewrite(
                            title=title_map["blog_html"],
                            plan=plan,
                            records_text=records_text,
                            current_html=iter_html,
                            review_feedback=review_feedback,
                        )
                    except Exception as exc:
                        errors.append(f"rewrite_round_{round_index + 1}: {exc}")
                        break

                slot_media_html: dict[str, str] = {}
                slot_source_id_map: dict[str, list[str]] = {}
                for idx, section in enumerate(sections, start=1):
                    if not isinstance(section, dict):
                        continue
                    slot = _normalize_slot_name(section.get("slot"), fallback_index=idx)
                    title = str(section.get("title") or f"章节 {idx}").strip()[:80] or f"章节 {idx}"
                    media_group = slot_media_assets.get(slot) or {}
                    assigned_source_ids = [str(item or "").strip() for item in (slot_source_image_ids.get(slot) or [])]
                    slot_source_images: list[dict] = []
                    valid_source_ids: list[str] = []
                    for image_id in assigned_source_ids:
                        source_item = source_image_index.get(image_id)
                        if not source_item:
                            continue
                        slot_source_images.append(source_item)
                        valid_source_ids.append(image_id)
                    slot_source_id_map[slot] = valid_source_ids
                    slot_block = _digest_media_section_assets_html(
                        poster_assets=list(media_group.get("images") or []),
                        podcast_assets=list(media_group.get("audios") or []),
                        source_images=slot_source_images,
                        heading=f"{title} · 多媒体",
                        class_name="benoss-slot-media",
                    )
                    if slot_block:
                        slot_media_html[slot] = slot_block

                blog_with_slots, used_slots = _inject_slot_media_markers(
                    iter_html,
                    slot_media_html=slot_media_html,
                )
                used_source_ids: set[str] = set()
                for slot in used_slots:
                    used_source_ids.update(slot_source_id_map.get(slot) or [])

                leftover_posters = [asset for asset in poster_assets if _asset_slot_name(asset) not in used_slots]
                leftover_podcasts = [asset for asset in podcast_assets if _asset_slot_name(asset) not in used_slots]
                leftover_source_images = [
                    item
                    for item in source_images
                    if str(item.get("id") or "").strip() and str(item.get("id") or "").strip() not in used_source_ids
                ]
                if leftover_posters or leftover_podcasts or leftover_source_images or source_audios or source_videos:
                    leftover_html = _digest_media_section_assets_html(
                        poster_assets=leftover_posters,
                        podcast_assets=leftover_podcasts,
                        source_images=leftover_source_images,
                        source_audios=source_audios,
                        source_videos=source_videos,
                        heading="补充媒体",
                        class_name="benoss-media-appendix",
                    )
                    if leftover_html:
                        injected = blog_with_slots
                        if "<html" in injected.lower():
                            for tag in ("main", "article", "body"):
                                updated = _inject_html_after_opening_tag(injected, leftover_html, tag)
                                if updated != injected:
                                    injected = updated
                                    break
                            else:
                                for tag in ("body", "html"):
                                    updated = _inject_html_before_closing_tag(injected, leftover_html, tag)
                                    if updated != injected:
                                        injected = updated
                                        break
                        else:
                            injected = f"{leftover_html}\n{injected}"
                        blog_with_slots = injected

                final_html = _wrap_blog_html_document(
                    blog_with_slots,
                    title=title_map["blog_html"],
                    poster_asset_id=primary_poster_id if not slot_media_html else None,
                    podcast_asset_id=primary_podcast_id if not slot_media_html else None,
                )
                model_label = f"{writer_settings.get('model')} + collab"
                if (
                    planner_settings
                    and planner_settings.get("model")
                    and planner_settings.get("model") != writer_settings.get("model")
                ):
                    model_label = f"{writer_settings.get('model')} + planner"
                blog_asset = _save_generated_asset(
                    user=owner,
                    kind="blog_html",
                    title=title_map["blog_html"],
                    provider=str(writer_settings.get("provider") or "ai"),
                    model=str(model_label or "collab")[:128],
                    content_type="text/html; charset=utf-8",
                    ext=".html",
                    data=final_html.encode("utf-8"),
                    filters=dict(base_filters, pipeline="collab"),
                    visibility="public",
                    source_day=day_value,
                    is_daily_digest=True,
                )
            except Exception as exc:
                errors.append(f"blog_html_collab: {exc}")
                try:
                    blog_asset, _ = _generate_blog_asset(
                        user=owner,
                        records_text=records_text,
                        image_urls=image_urls,
                        filters=base_filters,
                        title=title_map["blog_html"],
                        visibility="public",
                        poster_asset_id=primary_poster_id,
                        podcast_asset_id=primary_podcast_id,
                        extra_media_html=source_media_html,
                        source_day=day_value,
                        is_daily_digest=True,
                    )
                except RuntimeError as fallback_exc:
                    errors.append(f"blog_html_fallback: {fallback_exc}")
                except Exception as fallback_exc:
                    errors.append(f"blog_html_fallback: {fallback_exc}")

    primary_poster = poster_assets[0] if poster_assets else None
    primary_podcast = podcast_assets[0] if podcast_assets else None
    job.blog_asset_id = blog_asset.id if blog_asset else None
    job.poster_asset_id = primary_poster.id if primary_poster else None
    job.podcast_asset_id = primary_podcast.id if primary_podcast else None

    needs_poster = _digest_collab_max_image_assets() > 0
    needs_podcast = _digest_collab_max_audio_assets() > 0
    poster_ready = bool(primary_poster or not needs_poster)
    podcast_ready = bool(primary_podcast or not needs_podcast)

    if blog_asset and poster_ready and podcast_ready:
        job.status = "ready"
    elif blog_asset:
        job.status = "partial"
    else:
        job.status = "failed"
    job.finished_at = datetime.utcnow()
    job.error = " | ".join(errors)[:4000] if errors else ""
    db.session.add(job)
    db.session.commit()

    ordered_assets: list[GeneratedAsset] = []
    seen_ids: set[int] = set()
    for item in [blog_asset] + podcast_assets + poster_assets:
        if not item:
            continue
        if int(item.id) in seen_ids:
            continue
        seen_ids.add(int(item.id))
        ordered_assets.append(item)
    return {
        "day": day_value.isoformat(),
        "timezone": tz_name,
        "record_count": record_count_total,
        "status": job.status,
        "error": job.error,
        "assets": [_generated_asset_payload(item) for item in ordered_assets],
    }


def _archive_and_index_records(
    *,
    day_value: date,
    records: list[Record],
    scope: str,
    source: str,
    timezone_name: str,
) -> dict:
    retention_info = apply_archive_retention_policy()
    vector_info = index_meta()

    if not records:
        if (
            scope == "public"
            and int(retention_info.get("deleted_count") or 0) > 0
            and get_setting_bool("VECTOR_AUTO_REBUILD", default=True)
        ):
            try:
                vector_info = build_index(max_docs=get_setting_int("VECTOR_MAX_DOCS", default=4000))
            except Exception as exc:
                current_app.logger.warning("vector rebuild failed after retention cleanup: %s", exc)
        return {
            "saved": False,
            "reason": "no_records",
            "retention": retention_info,
            "vector": vector_info,
        }

    try:
        archive_info = save_daily_archive(
            day_value,
            records,
            scope=scope,
            source=source,
            timezone_name=timezone_name,
        )
    except Exception as exc:
        current_app.logger.warning("daily archive save failed: %s", exc)
        return {
            "saved": False,
            "reason": str(exc),
            "retention": retention_info,
            "vector": vector_info,
        }

    retention_after = archive_info.get("retention") or {}
    if retention_after:
        merged_days = sorted(
            set((retention_info.get("deleted_days") or []) + (retention_after.get("deleted_days") or []))
        )
        retention_info = dict(retention_after)
        retention_info["deleted_days"] = merged_days
        retention_info["deleted_count"] = len(merged_days)
    should_rebuild = bool(archive_info.get("changed")) or int(retention_info.get("deleted_count") or 0) > 0
    if scope == "public" and should_rebuild and get_setting_bool("VECTOR_AUTO_REBUILD", default=True):
        try:
            vector_info = build_index(max_docs=get_setting_int("VECTOR_MAX_DOCS", default=4000))
        except Exception as exc:
            current_app.logger.warning("vector rebuild failed: %s", exc)

    return {
        "saved": True,
        "archive": archive_info,
        "retention": retention_info,
        "vector": vector_info,
    }


def _daily_digest_assets_for_day(day_value: date) -> list[GeneratedAsset]:
    assets = (
        GeneratedAsset.query.options(joinedload(GeneratedAsset.user))
        .filter(
            GeneratedAsset.kind.in_(("blog_html", "podcast_audio", "poster_image")),
            GeneratedAsset.visibility == "public",
            GeneratedAsset.status == "ready",
            GeneratedAsset.is_daily_digest.is_(True),
            GeneratedAsset.source_day == day_value,
        )
        .order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id.desc())
        .all()
    )
    order = {"blog_html": 0, "podcast_audio": 1, "poster_image": 2}
    assets.sort(key=lambda item: order.get(str(item.kind or "").strip().lower(), 99))
    return assets


def _public_record_count_for_day(*, day_value: date, timezone_name: str) -> int:
    start, end = _utc_bounds_for_local_day(day_value, timezone_name)
    total = (
        db.session.query(func.count(Record.id))
        .filter(
            Record.visibility == "public",
            Record.created_at >= start,
            Record.created_at < end,
        )
        .scalar()
    )
    return int(total or 0)


def _maybe_auto_build_digest_for_day(*, day_value: date, timezone_name: str, record_count: int) -> dict:
    enabled = get_setting_bool("HOME_AUTO_BUILD_DAILY_ASSETS", default=True)
    state = {
        "day": day_value.isoformat(),
        "enabled": bool(enabled),
        "triggered": False,
        "status": "disabled" if not enabled else "skipped",
        "message": "auto build disabled" if not enabled else "",
    }
    if not enabled:
        return state

    if record_count <= 0:
        state["status"] = "no_records"
        state["message"] = f"{day_value.isoformat()} has no public records"
        return state

    if not _ai_provider_settings():
        state["status"] = "ai_not_configured"
        state["message"] = "AI provider not configured"
        return state

    ready_assets = _daily_digest_assets_for_day(day_value)
    ready_kinds = {item.kind for item in ready_assets}
    required_kinds = {"blog_html"}
    if required_kinds.issubset(ready_kinds):
        missing_optional = {"podcast_audio", "poster_image"} - ready_kinds
        if missing_optional:
            state["status"] = "partial"
            state["message"] = "blog ready; missing " + ", ".join(sorted(missing_optional))
        else:
            state["status"] = "ready"
            state["message"] = "assets already ready"
        return state

    retry_minutes = max(1, min(get_setting_int("HOME_DIGEST_RETRY_MINUTES", default=30), 720))
    now = datetime.utcnow()
    job = DailyDigestJob.query.filter_by(day=day_value, timezone=timezone_name).first()
    if job and job.status == "running":
        state["status"] = "running"
        state["message"] = "daily digest job is running"
        return state
    if (
        job
        and job.status in {"failed", "partial"}
        and job.finished_at
        and (now - job.finished_at).total_seconds() < retry_minutes * 60
    ):
        state["status"] = job.status
        state["message"] = f"recent {job.status}, retry later"
        return state

    try:
        result = build_daily_public_digest(day_value=day_value, force=False, timezone_name=timezone_name)
        state["triggered"] = True
        state["status"] = str(result.get("status") or "ready")
        state["message"] = str(result.get("error") or "").strip() or "daily digest updated"
    except Exception as exc:
        state["triggered"] = True
        state["status"] = "error"
        state["message"] = str(exc)
    return state


@api_bp.route("/api/uploads/prepare", methods=["POST"])
@login_required()
def prepare_upload():
    user = g.get("user")
    payload = _form_or_json_payload()

    filename_raw = str(payload.get("filename") or "").strip()
    if not filename_raw:
        return jsonify({"error": "filename is required"}), 400

    try:
        filename = _normalize_upload_filename(filename_raw, allow_empty=True)
        size_bytes = _normalize_upload_size_bytes(payload.get("size_bytes"), allow_empty=True)
        sha256 = _normalize_upload_sha256(payload.get("sha256"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    content_type = _normalize_upload_content_type(payload.get("content_type"), filename)
    oss_key = record_content_key(new_uuid(), filename)
    upload_token = _issue_direct_upload_token(
        user_id=user.id,
        oss_key=oss_key,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )

    direct_enabled = bool(_direct_upload_enabled() and has_remote_backend())
    direct_url = ""
    if direct_enabled:
        try:
            direct_url = sign_put_url(oss_key, expires=_direct_upload_expires_seconds(), content_type=content_type)
        except Exception:
            direct_url = ""
            current_app.logger.warning("failed to sign direct OSS upload url", exc_info=True)

    response = {
        "strategy": "direct_oss" if direct_url else "server_sdk",
        "fallback": "server_sdk",
        "upload_token": upload_token,
        "file": {
            "oss_key": oss_key,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
        },
        "direct": {
            "enabled": bool(direct_url),
            "method": "PUT",
            "url": direct_url,
            "headers": {"Content-Type": content_type} if direct_url else {},
            "expires_in": _direct_upload_expires_seconds(),
            "reason": "" if direct_url else ("not_available" if not direct_enabled else "sign_failed"),
        },
    }
    return jsonify(response)


@api_bp.route("/api/push", methods=["POST"])
@login_required()
def push_record_alias():
    user = g.get("user")
    return _create_record_for_user(user)


@api_bp.route("/api/pull", methods=["GET"])
@login_required()
def pull_records_alias():
    user = g.get("user")
    query = _record_query_for(user, include_comments=False, public_only=False)
    try:
        query = _apply_record_filters(query)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    per = min(max(int(request.args.get("per") or 200), 1), 1000)
    content_source = _normalize_content_text_source(request.args.get("content_source"), default="summary")
    include_signed_url = _truthy(request.args.get("include_signed_url"))
    signed_url_expires = _normalize_signed_url_expires(request.args.get("signed_url_expires"), default=300)
    records = query.order_by(Record.created_at.desc(), Record.id.desc()).limit(per).all()
    return jsonify(
        {
            "items": [
                _record_payload(
                    item,
                    viewer=user,
                    include_content=True,
                    include_comments=False,
                    content_text_source=content_source,
                    include_signed_url=include_signed_url,
                    signed_url_expires=signed_url_expires,
                )
                for item in records
            ]
        }
    )


@api_bp.route("/api/records", methods=["POST"])
@login_required()
def create_record():
    user = g.get("user")
    return _create_record_for_user(user)


@api_bp.route("/api/records", methods=["GET"])
@login_required()
def list_records():
    user = g.get("user")
    include_content = str(request.args.get("include_content") or "0") == "1"
    include_comments = str(request.args.get("include_comments") or "0") == "1"
    public_only = str(request.args.get("public_only") or "0") == "1"
    content_source = _normalize_content_text_source(request.args.get("content_source"), default="summary")
    include_signed_url = _truthy(request.args.get("include_signed_url"))
    signed_url_expires = _normalize_signed_url_expires(request.args.get("signed_url_expires"), default=300)

    query = _record_query_for(user, include_comments=include_comments, public_only=public_only)
    try:
        query = _apply_record_filters(query)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    order = str(request.args.get("order") or "desc").strip().lower()
    if order == "asc":
        query = query.order_by(Record.created_at.asc(), Record.id.asc())
    else:
        query = query.order_by(Record.created_at.desc(), Record.id.desc())

    page = max(int(request.args.get("page") or 1), 1)
    per = min(max(int(request.args.get("per") or 30), 1), 100)

    total = query.order_by(None).count()
    rows = query.offset((page - 1) * per).limit(per).all()

    return jsonify(
        {
            "items": [
                _record_payload(
                    row,
                    viewer=user,
                    include_content=include_content,
                    include_comments=include_comments,
                    content_text_source=content_source,
                    include_signed_url=include_signed_url,
                    signed_url_expires=signed_url_expires,
                )
                for row in rows
            ],
            "total": total,
            "page": page,
            "per": per,
        }
    )


@api_bp.route("/api/records/<int:record_id>", methods=["GET"])
@login_required()
def get_record(record_id: int):
    user = g.get("user")
    record = (
        Record.query.options(
            joinedload(Record.user),
            joinedload(Record.content),
            joinedload(Record.comments).joinedload(Comment.user),
        )
        .filter_by(id=record_id)
        .first_or_404()
    )

    if not _is_record_visible(record, user):
        return jsonify({"error": "forbidden"}), 403

    include_comments = str(request.args.get("include_comments") or "1") != "0"
    content_source = _normalize_content_text_source(request.args.get("content_source"), default="full")
    include_signed_url = _truthy(request.args.get("include_signed_url"))
    signed_url_expires = _normalize_signed_url_expires(request.args.get("signed_url_expires"), default=300)
    return jsonify(
        {
            "record": _record_payload(
                record,
                viewer=user,
                include_content=True,
                include_comments=include_comments,
                content_text_source=content_source,
                include_signed_url=include_signed_url,
                signed_url_expires=signed_url_expires,
            )
        }
    )


@api_bp.route("/api/records/<int:record_id>", methods=["PATCH"])
@login_required()
def update_record(record_id: int):
    user = g.get("user")
    record = Record.query.options(joinedload(Record.content)).filter_by(id=record_id).first_or_404()

    if record.user_id != user.id:
        return jsonify({"error": "forbidden"}), 403

    payload = _form_or_json_payload()
    try:
        uploaded_file_token = _request_single_uploaded_file_token(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    uploaded_oss_keys: list[str] = []
    stale_oss_keys: list[str] = []
    next_tags = record.get_tags()

    if "visibility" in payload:
        record.visibility = _normalize_visibility(payload.get("visibility"), default=record.visibility)

    if "tags" in payload:
        next_tags = _parse_tags(payload.get("tags"))

    if record.content.kind == "text" and "text" in payload:
        text = str(payload.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text cannot be empty"}), 400
        auto_tags = _auto_tags_from_text(text)
        if auto_tags:
            next_tags = _parse_tags(next_tags + auto_tags)

        old_key = str(record.content.oss_key or "").strip()
        content = _text_to_content(text)
        if content.oss_key:
            uploaded_oss_keys.append(content.oss_key)

        record.content.kind = "text"
        record.content.file_type = "text"
        record.content.text_content = content.text_content or ""
        record.content.filename = content.filename or ""
        record.content.content_type = content.content_type or ""
        record.content.size_bytes = int(content.size_bytes or 0)
        record.content.sha256 = content.sha256 or ""
        record.content.oss_key = content.oss_key or ""
        record.preview = _preview_text(text)
        if old_key and old_key != record.content.oss_key:
            stale_oss_keys.append(old_key)

    if "tags" in payload or (record.content.kind == "text" and "text" in payload):
        record.set_tags(next_tags)

    new_file = request.files.get("file")
    if new_file and uploaded_file_token:
        return jsonify({"error": "file and uploaded_file_token cannot be used together"}), 400

    if new_file:
        old_key = str(record.content.oss_key or "").strip()
        content, preview = _file_to_content(new_file)
        if content.oss_key:
            uploaded_oss_keys.append(content.oss_key)

        record.content.kind = content.kind
        record.content.file_type = content.file_type or _detect_file_type(content.content_type, content.filename)
        # Content.text_content is non-null in schema; keep empty string for file records.
        record.content.text_content = content.text_content or ""
        record.content.filename = content.filename or ""
        record.content.content_type = content.content_type or ""
        record.content.size_bytes = int(content.size_bytes or 0)
        record.content.sha256 = content.sha256 or ""
        record.content.oss_key = content.oss_key or ""

        record.preview = preview

        if old_key and old_key != record.content.oss_key:
            stale_oss_keys.append(old_key)
    elif uploaded_file_token:
        old_key = str(record.content.oss_key or "").strip()
        try:
            content, preview = _content_from_uploaded_file_token(uploaded_file_token, user_id=user.id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if content.oss_key:
            uploaded_oss_keys.append(content.oss_key)

        record.content.kind = content.kind
        record.content.file_type = content.file_type or _detect_file_type(content.content_type, content.filename)
        # Content.text_content is non-null in schema; keep empty string for file records.
        record.content.text_content = content.text_content or ""
        record.content.filename = content.filename or ""
        record.content.content_type = content.content_type or ""
        record.content.size_bytes = int(content.size_bytes or 0)
        record.content.sha256 = content.sha256 or ""
        record.content.oss_key = content.oss_key or ""
        record.preview = preview

        if old_key and old_key != record.content.oss_key:
            stale_oss_keys.append(old_key)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        for oss_key in set(uploaded_oss_keys):
            try:
                delete_object(oss_key)
            except Exception:
                pass
        raise

    for oss_key in set(stale_oss_keys):
        try:
            delete_object(oss_key)
        except Exception:
            pass

    return jsonify({"ok": True})


@api_bp.route("/api/records/<int:record_id>", methods=["DELETE"])
@login_required()
def delete_record(record_id: int):
    user = g.get("user")
    record = Record.query.options(joinedload(Record.content)).filter_by(id=record_id).first_or_404()

    if record.user_id != user.id:
        return jsonify({"error": "forbidden"}), 403

    oss_key = record.content.oss_key if record.content and record.content.oss_key else ""
    content = record.content

    db.session.delete(record)
    if content:
        db.session.delete(content)
    db.session.commit()

    if oss_key:
        try:
            delete_object(oss_key)
        except Exception:
            pass

    return jsonify({"ok": True})


@api_bp.route("/api/records/<int:record_id>/clone", methods=["POST"])
@login_required()
def clone_record(record_id: int):
    user = g.get("user")
    source_record = (
        Record.query.options(joinedload(Record.user), joinedload(Record.content))
        .filter_by(id=record_id)
        .first_or_404()
    )
    if not _is_record_visible(source_record, user):
        return jsonify({"error": "forbidden"}), 403

    payload = _form_or_json_payload()
    visibility = _normalize_visibility(payload.get("visibility"), default="private")

    source_content = source_record.content
    if not source_content:
        return jsonify({"error": "source content unavailable"}), 409

    uploaded_oss_key = ""
    if source_content.kind == "text":
        source_text = _read_text_content(source_content)
        cloned_content = _text_to_content(source_text)
        uploaded_oss_key = str(cloned_content.oss_key or "").strip()
    else:
        source_key = str(source_content.oss_key or "").strip()
        if not source_key:
            return jsonify({"error": "source file unavailable"}), 409
        filename = source_content.filename or f"clone-{source_content.id}"
        uploaded_oss_key = record_content_key(new_uuid(), filename)
        try:
            copy_object(source_key, uploaded_oss_key)
        except Exception as exc:
            return jsonify({"error": f"clone file failed: {exc}"}), 502
        cloned_content = Content(
            kind="file",
            file_type=_normalize_stored_file_type(
                getattr(source_content, "file_type", ""),
                fallback=_detect_file_type(source_content.content_type, source_content.filename),
            ),
            text_content="",
            filename=source_content.filename or "",
            content_type=source_content.content_type or "",
            size_bytes=int(source_content.size_bytes or 0),
            sha256=source_content.sha256 or "",
            oss_key=uploaded_oss_key,
        )

    cloned_record = Record(
        user_id=user.id,
        content=cloned_content,
        visibility=visibility,
        preview=source_record.preview or "",
    )
    cloned_record.set_tags(source_record.get_tags())

    db.session.add(cloned_content)
    db.session.add(cloned_record)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        if uploaded_oss_key:
            try:
                delete_object(uploaded_oss_key)
            except Exception:
                pass
        raise

    loaded = (
        Record.query.options(joinedload(Record.user), joinedload(Record.content))
        .filter_by(id=cloned_record.id)
        .first_or_404()
    )
    return jsonify({"record": _record_payload(loaded, viewer=user, include_content=False, include_comments=False)}), 201


@api_bp.route("/api/records/<int:record_id>/comments", methods=["GET"])
@login_required()
def list_comments(record_id: int):
    user = g.get("user")
    record = Record.query.get_or_404(record_id)

    if not _is_record_visible(record, user):
        return jsonify({"error": "forbidden"}), 403

    comments = (
        Comment.query.options(joinedload(Comment.user))
        .filter(Comment.record_id == record.id)
        .order_by(Comment.created_at.asc(), Comment.id.asc())
        .all()
    )
    return jsonify({"items": [_comment_payload(item) for item in comments]})


@api_bp.route("/api/records/<int:record_id>/comments", methods=["POST"])
@login_required()
def create_comment(record_id: int):
    user = g.get("user")
    record = Record.query.get_or_404(record_id)

    if not _is_record_visible(record, user):
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    body = str(payload.get("body") or "").strip()
    if not body:
        return jsonify({"error": "missing comment body"}), 400
    if len(body) > 2000:
        return jsonify({"error": "comment too long"}), 400

    comment = Comment(record_id=record.id, user_id=user.id, body=body)
    db.session.add(comment)
    db.session.commit()

    loaded = Comment.query.options(joinedload(Comment.user)).get(comment.id)
    return jsonify({"comment": _comment_payload(loaded)}), 201


@api_bp.route("/api/contents/<int:content_id>/blob", methods=["GET"])
@login_required()
def get_content_blob(content_id: int):
    user = g.get("user")
    content = Content.query.options(
        joinedload(Content.record).joinedload(Record.user),
    ).get_or_404(content_id)

    record = content.record
    if not record or not _is_record_visible(record, user):
        return jsonify({"error": "forbidden"}), 403

    if content.kind == "text":
        if content.oss_key:
            try:
                data = get_object_bytes(content.oss_key)
                response = Response(data, mimetype=content.content_type or "text/plain; charset=utf-8")
                if content.filename:
                    response.headers["Content-Disposition"] = f'inline; filename="{content.filename}"'
                return response
            except Exception:
                pass

        fallback_text = content.text_content or ""
        if fallback_text:
            return Response(fallback_text, mimetype="text/plain; charset=utf-8")
        return jsonify({"error": "content unavailable"}), 404

    if not content.oss_key:
        return jsonify({"error": "missing content"}), 404

    try:
        data = get_object_bytes(content.oss_key)
    except Exception:
        return jsonify({"error": "content unavailable"}), 404

    response = Response(data, mimetype=content.content_type or "application/octet-stream")
    if content.filename:
        response.headers["Content-Disposition"] = f'inline; filename="{content.filename}"'
    return response


@api_bp.route("/api/board", methods=["GET"])
@login_required()
def board_summary():
    user = g.get("user")

    days = int(request.args.get("days") or get_setting_int("BOARD_DEFAULT_DAYS", default=7))
    days = min(max(days, 1), 30)

    tag = str(request.args.get("tag") or "").strip()

    today = datetime.utcnow().date()
    start_day = today - timedelta(days=days - 1)
    start_dt, _ = _day_bounds(start_day)
    _, end_dt = _day_bounds(today)

    dates = [(start_day + timedelta(days=i)).isoformat() for i in range(days)]

    query = (
        db.session.query(Record.user_id, func.date(Record.created_at), func.count(Record.id))
        .filter(Record.created_at >= start_dt, Record.created_at < end_dt)
        .filter(_visible_filter(user.id, public_only=False))
    )
    if tag:
        query = query.filter(Record.tags_json.contains(f'"{tag}"'))

    rows = query.group_by(Record.user_id, func.date(Record.created_at)).all()

    matrix: dict[str, dict[str, int]] = {}
    for user_id, date_str, count in rows:
        uid = str(int(user_id))
        day_key = str(date_str)
        matrix.setdefault(uid, {})[day_key] = int(count or 0)

    users = User.query.filter_by(is_active=True).order_by(User.username.asc()).all()

    return jsonify(
        {
            "dates": dates,
            "users": [{"id": item.id, "username": item.username} for item in users],
            "matrix": matrix,
        }
    )


@api_bp.route("/api/board/cell", methods=["GET"])
@login_required()
def board_cell_records():
    user = g.get("user")

    user_id = str(request.args.get("user_id") or "").strip()
    day = str(request.args.get("day") or "").strip()
    tag = str(request.args.get("tag") or "").strip()

    if not user_id.isdigit():
        return jsonify({"error": "invalid user_id"}), 400
    try:
        day_value = _parse_day(day)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    start, end = _day_bounds(day_value)

    query = (
        _record_query_for(user, include_comments=False, public_only=False)
        .filter(Record.user_id == int(user_id))
        .filter(Record.created_at >= start, Record.created_at < end)
    )
    if tag:
        query = query.filter(Record.tags_json.contains(f'"{tag}"'))

    records = query.order_by(Record.created_at.desc(), Record.id.desc()).all()
    return jsonify(
        {
            "items": [
                _record_payload(item, viewer=user, include_content=False, include_comments=False)
                for item in records
            ]
        }
    )


@api_bp.route("/api/board/user/<int:user_id>/records", methods=["GET"])
@login_required()
def board_user_records(user_id: int):
    user = g.get("user")
    tag = str(request.args.get("tag") or "").strip()

    query = _record_query_for(user, include_comments=False, public_only=False).filter(Record.user_id == user_id)
    if tag:
        query = query.filter(Record.tags_json.contains(f'"{tag}"'))

    records = query.order_by(Record.created_at.desc(), Record.id.desc()).limit(500).all()
    return jsonify(
        {
            "items": [
                _record_payload(item, viewer=user, include_content=False, include_comments=False)
                for item in records
            ]
        }
    )


@api_bp.route("/api/board/date/<day>", methods=["GET"])
@login_required()
def board_day_records(day: str):
    user = g.get("user")
    tag = str(request.args.get("tag") or "").strip()
    try:
        day_value = _parse_day(day)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    start, end = _day_bounds(day_value)

    query = (
        _record_query_for(user, include_comments=False, public_only=True)
        .filter(Record.created_at >= start, Record.created_at < end)
    )
    if tag:
        query = query.filter(Record.tags_json.contains(f'"{tag}"'))

    records = query.order_by(Record.created_at.desc(), Record.id.desc()).limit(1000).all()
    return jsonify(
        {
            "items": [
                _record_payload(item, viewer=user, include_content=False, include_comments=False)
                for item in records
            ]
        }
    )


@api_bp.route("/api/echoes", methods=["GET"])
@login_required()
def echoes_feed():
    user = g.get("user")
    scope = str(request.args.get("scope") or "with_mine").strip().lower()
    if scope not in {"public", "with_mine"}:
        return jsonify({"error": "invalid scope"}), 400

    include_private = scope == "with_mine"
    records_query = _record_query_for(user, include_comments=False, public_only=not include_private)
    assets_query = GeneratedAsset.query.options(joinedload(GeneratedAsset.user)).filter(GeneratedAsset.status == "ready")
    if include_private:
        assets_query = assets_query.filter(or_(GeneratedAsset.visibility == "public", GeneratedAsset.user_id == user.id))
    else:
        assets_query = assets_query.filter(GeneratedAsset.visibility == "public")

    tag = str(request.args.get("tag") or "").strip()
    if tag:
        records_query = records_query.filter(Record.tags_json.contains(f'"{tag}"'))
        assets_query = assets_query.filter(GeneratedAsset.source_filters_json.contains(f'"tag": "{tag}"'))

    day = str(request.args.get("day") or "").strip()
    day_value = None
    if day:
        try:
            day_value = _parse_day(day)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        start, end = _day_bounds(day_value)
        records_query = records_query.filter(Record.created_at >= start, Record.created_at < end)
        assets_query = assets_query.filter(GeneratedAsset.source_day == day_value)

    try:
        file_type = _normalize_echo_file_type(request.args.get("file_type"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if file_type:
        record_filter = _record_file_type_filter(file_type)
        if record_filter is not None:
            records_query = records_query.filter(Record.content.has(record_filter))
        asset_filter = _asset_file_type_filter(file_type)
        if asset_filter is not None:
            assets_query = assets_query.filter(asset_filter)

    limit = min(max(int(request.args.get("limit") or request.args.get("per") or 24), 1), 80)

    cursor_time_raw = str(request.args.get("cursor_time") or "").strip()
    cursor_kind = str(request.args.get("cursor_kind") or "").strip().lower()
    cursor_id_raw = str(request.args.get("cursor_id") or "").strip()
    cursor_time = None
    cursor_id = 0

    if cursor_time_raw or cursor_kind or cursor_id_raw:
        if not (cursor_time_raw and cursor_kind and cursor_id_raw):
            return jsonify({"error": "invalid cursor"}), 400
        if cursor_kind not in {"record", "asset"}:
            return jsonify({"error": "invalid cursor_kind"}), 400
        if not cursor_id_raw.isdigit() or int(cursor_id_raw) <= 0:
            return jsonify({"error": "invalid cursor_id"}), 400
        cursor_id = int(cursor_id_raw)
        try:
            cursor_time = _parse_iso_datetime(cursor_time_raw)
        except ValueError:
            return jsonify({"error": "invalid cursor_time"}), 400

    if cursor_time:
        if cursor_kind == "record":
            records_query = records_query.filter(
                or_(
                    Record.created_at < cursor_time,
                    and_(Record.created_at == cursor_time, Record.id < cursor_id),
                )
            )
            assets_query = assets_query.filter(GeneratedAsset.created_at <= cursor_time)
        else:
            records_query = records_query.filter(Record.created_at < cursor_time)
            assets_query = assets_query.filter(
                or_(
                    GeneratedAsset.created_at < cursor_time,
                    and_(GeneratedAsset.created_at == cursor_time, GeneratedAsset.id < cursor_id),
                )
            )

    window_size = limit + 1
    records = (
        records_query.order_by(Record.created_at.desc(), Record.id.desc())
        .limit(window_size)
        .all()
    )
    assets = (
        assets_query.order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id.desc())
        .limit(window_size)
        .all()
    )

    candidates = []
    for item in records:
        record_payload = _record_payload(
            item,
            viewer=user,
            include_content=True,
            include_comments=False,
            content_text_source="db_preview",
            include_signed_url=False,
        )
        candidates.append(
            {
                "entry_type": "record",
                "id": item.id,
                "created_at": item.created_at,
                "file_type": _record_echo_file_type(item),
                "record": record_payload,
            }
        )
    for item in assets:
        asset_payload = _generated_asset_payload(item, viewer=user)
        candidates.append(
            {
                "entry_type": "asset",
                "id": item.id,
                "created_at": item.created_at,
                "file_type": _asset_echo_file_type(item),
                "asset": asset_payload,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["created_at"] or datetime.min,
            -(0 if item["entry_type"] == "record" else 1),
            item["id"],
        ),
        reverse=True,
    )

    selected = candidates[:limit]
    has_more = len(candidates) > limit
    next_cursor = None
    if has_more and selected:
        last = selected[-1]
        next_cursor = {
            "created_at": _iso(last["created_at"]),
            "entry_type": last["entry_type"],
            "id": int(last["id"]),
        }

    items_payload = []
    assets_payload = []
    entries_payload = []
    for entry in selected:
        if entry["entry_type"] == "record":
            payload = entry["record"]
            items_payload.append(payload)
            entries_payload.append(
                {
                    "entry_type": "record",
                    "id": int(entry["id"]),
                    "created_at": _iso(entry["created_at"]),
                    "file_type": entry["file_type"],
                    "record": payload,
                }
            )
        else:
            payload = entry["asset"]
            assets_payload.append(payload)
            entries_payload.append(
                {
                    "entry_type": "asset",
                    "id": int(entry["id"]),
                    "created_at": _iso(entry["created_at"]),
                    "file_type": entry["file_type"],
                    "asset": payload,
                }
            )

    return jsonify(
        {
            "entries": entries_payload,
            "items": items_payload,
            "assets": assets_payload,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit,
            "file_type": file_type or "all",
            "scope": scope,
        }
    )


@api_bp.route("/api/notice/render", methods=["GET"])
@login_required()
def notice_render():
    user = g.get("user")

    query = _record_query_for(user, include_comments=False, public_only=False)
    try:
        query = _apply_record_filters(query)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    order = str(request.args.get("order") or "asc").strip().lower()
    if order == "desc":
        query = query.order_by(Record.created_at.desc(), Record.id.desc())
    else:
        query = query.order_by(Record.created_at.asc(), Record.id.asc())

    limit = min(max(int(request.args.get("limit") or 1000), 1), 2000)
    records = query.limit(limit).all()

    day = str(request.args.get("day") or "").strip()
    user_id = str(request.args.get("user_id") or "").strip()
    tag = str(request.args.get("tag") or "").strip()

    html_output = _render_notice_html(records, day=day, user_id=user_id, tag=tag, viewer=user)

    return jsonify(
        {
            "count": len(records),
            "rendered_html": html_output,
            "records": [
                _record_payload(item, viewer=user, include_content=False, include_comments=False)
                for item in records
            ],
        }
    )


@api_bp.route("/api/generated-assets", methods=["GET"])
@login_required()
def generated_assets():
    user = g.get("user")
    visibility = str(request.args.get("visibility") or "").strip().lower()
    if visibility and visibility not in _VALID_VISIBILITY:
        return jsonify({"error": "invalid visibility"}), 400

    day = str(request.args.get("day") or "").strip()
    kind = str(request.args.get("kind") or "").strip()
    daily_digest = str(request.args.get("daily_digest") or "0").strip().lower() in {"1", "true", "yes", "on"}
    page = max(int(request.args.get("page") or 1), 1)
    per = min(max(int(request.args.get("per") or 40), 1), 200)

    query = GeneratedAsset.query.options(joinedload(GeneratedAsset.user))
    if visibility == "public":
        query = query.filter(GeneratedAsset.visibility == "public")
    elif visibility == "private":
        query = query.filter(GeneratedAsset.user_id == user.id)
    else:
        query = query.filter(or_(GeneratedAsset.visibility == "public", GeneratedAsset.user_id == user.id))

    if kind:
        query = query.filter(GeneratedAsset.kind == kind)
    if day:
        try:
            day_value = _parse_day(day)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        query = query.filter(GeneratedAsset.source_day == day_value)
    if daily_digest:
        query = query.filter(GeneratedAsset.is_daily_digest.is_(True))

    total = query.order_by(None).count()
    assets = (
        query.order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id.desc())
        .offset((page - 1) * per)
        .limit(per)
        .all()
    )

    return jsonify(
        {
            "items": [_generated_asset_payload(item, viewer=user) for item in assets],
            "total": total,
            "page": page,
            "per": per,
        }
    )


@api_bp.route("/api/generated-assets/<int:asset_id>", methods=["GET"])
@login_required()
def get_generated_asset(asset_id: int):
    user = g.get("user")
    asset = GeneratedAsset.query.options(joinedload(GeneratedAsset.user)).filter_by(id=asset_id).first_or_404()
    if not _is_generated_asset_visible(asset, user):
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"asset": _generated_asset_payload(asset, viewer=user)})


@api_bp.route("/api/generated-assets/<int:asset_id>", methods=["PATCH"])
@login_required()
def update_generated_asset(asset_id: int):
    user = g.get("user")
    asset = GeneratedAsset.query.options(joinedload(GeneratedAsset.user)).filter_by(id=asset_id).first_or_404()
    if not _can_manage_generated_asset(asset, user):
        return jsonify({"error": "forbidden"}), 403

    payload = _form_or_json_payload()
    uploaded_oss_keys: list[str] = []
    stale_oss_keys: list[str] = []
    changed = False

    if "title" in payload:
        asset.title = str(payload.get("title") or "").strip()[:255]
        changed = True
    if "visibility" in payload:
        asset.visibility = _normalize_visibility(payload.get("visibility"), default=asset.visibility)
        changed = True

    new_file = request.files.get("file")
    has_html_replacement = "html" in payload
    if new_file and has_html_replacement:
        return jsonify({"error": "provide either file or html, not both"}), 400

    replacement_data: bytes | None = None
    replacement_content_type = ""
    replacement_ext = ""

    if new_file:
        default_ext, default_content_type = _generated_asset_default_file_meta(asset.kind)
        source_name = secure_filename(new_file.filename or "") or f"asset{default_ext}"
        file_bytes = new_file.read() or b""
        if not file_bytes:
            return jsonify({"error": "replacement file is empty"}), 400

        guessed_type = mimetypes.guess_type(source_name)[0] or ""
        content_type = str(new_file.mimetype or guessed_type or default_content_type).strip() or default_content_type
        ext = Path(source_name).suffix.lower() or default_ext
        if not _generated_asset_file_matches_kind(asset.kind, content_type=content_type, filename_or_ext=source_name):
            return jsonify({"error": _generated_asset_file_error(asset.kind)}), 400

        replacement_data = file_bytes
        replacement_content_type = content_type
        replacement_ext = ext
    elif has_html_replacement:
        if str(asset.kind or "").strip().lower() != "blog_html":
            return jsonify({"error": "html replacement is only supported for blog_html assets"}), 400
        html_text = str(payload.get("html") or "")
        if not html_text.strip():
            return jsonify({"error": "html cannot be empty"}), 400
        replacement_data = html_text.encode("utf-8")
        replacement_content_type = "text/html; charset=utf-8"
        replacement_ext = ".html"

    if replacement_data is not None:
        default_ext, _ = _generated_asset_default_file_meta(asset.kind)
        safe_ext = str(replacement_ext or default_ext).strip().lower() or default_ext
        if not safe_ext.startswith("."):
            safe_ext = f".{safe_ext}"
        safe_ext = safe_ext[:16] if len(safe_ext) > 16 else safe_ext

        new_key = generated_asset_key(asset.user_id, asset.kind, new_uuid(), f"asset{safe_ext}")
        try:
            put_object_bytes(new_key, replacement_data, content_type=replacement_content_type)
        except Exception as exc:
            db.session.rollback()
            return jsonify({"error": f"asset upload failed: {exc}"}), 502

        old_key = str(asset.oss_key or "").strip()
        uploaded_oss_keys.append(new_key)

        asset.oss_key = new_key
        asset.content_type = replacement_content_type[:255]
        asset.ext = safe_ext
        asset.file_type = _detect_asset_file_type(asset)
        asset.size_bytes = len(replacement_data)
        asset.sha256 = _sha256_bytes(replacement_data)
        asset.status = "ready"
        changed = True

        if old_key and old_key != new_key:
            stale_oss_keys.append(old_key)

    if not changed:
        return jsonify({"error": "nothing to update"}), 400

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        for oss_key in set(uploaded_oss_keys):
            try:
                delete_object(oss_key)
            except Exception:
                pass
        raise

    for oss_key in set(stale_oss_keys):
        try:
            delete_object(oss_key)
        except Exception:
            pass

    return jsonify({"ok": True, "asset": _generated_asset_payload(asset, viewer=user)})


@api_bp.route("/api/generated-assets/<int:asset_id>", methods=["DELETE"])
@login_required()
def delete_generated_asset(asset_id: int):
    user = g.get("user")
    asset = GeneratedAsset.query.options(joinedload(GeneratedAsset.user)).filter_by(id=asset_id).first_or_404()
    if not _can_manage_generated_asset(asset, user):
        return jsonify({"error": "forbidden"}), 403

    oss_key = str(asset.oss_key or "").strip()
    jobs = DailyDigestJob.query.filter(
        or_(
            DailyDigestJob.blog_asset_id == asset.id,
            DailyDigestJob.podcast_asset_id == asset.id,
            DailyDigestJob.poster_asset_id == asset.id,
        )
    ).all()
    for job in jobs:
        if job.blog_asset_id == asset.id:
            job.blog_asset_id = None
        if job.podcast_asset_id == asset.id:
            job.podcast_asset_id = None
        if job.poster_asset_id == asset.id:
            job.poster_asset_id = None

    db.session.delete(asset)
    db.session.commit()

    if oss_key:
        try:
            delete_object(oss_key)
        except Exception:
            pass

    return jsonify({"ok": True})


@api_bp.route("/api/digest/daily", methods=["POST"])
@login_required()
def digest_daily():
    user = g.get("user")
    if user.role != "admin":
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    day_raw = str(payload.get("day") or "").strip()
    timezone_name = str(payload.get("timezone") or _digest_timezone()).strip() or _digest_timezone()
    force = str(payload.get("force") or "0").strip().lower() in {"1", "true", "yes", "on"}

    if day_raw:
        try:
            day_value = _parse_day(day_raw)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            tz = timezone.utc
        day_value = datetime.now(tz).date() - timedelta(days=1)

    try:
        result = build_daily_public_digest(day_value=day_value, force=force, timezone_name=timezone_name)
    except RuntimeError as exc:
        message = str(exc)
        if "not configured" in message:
            return jsonify({"error": message}), 501
        return jsonify({"error": message}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


@api_bp.route("/api/generated-assets/<int:asset_id>/blob", methods=["GET"])
@login_required()
def generated_asset_blob(asset_id: int):
    user = g.get("user")
    asset = GeneratedAsset.query.get_or_404(asset_id)
    if not _is_generated_asset_visible(asset, user):
        return jsonify({"error": "forbidden"}), 403

    try:
        data = get_object_bytes(asset.oss_key)
    except Exception:
        return jsonify({"error": "asset unavailable"}), 404

    response = Response(data, mimetype=asset.content_type or "application/octet-stream")
    ext = (asset.ext or "").strip() or ""
    filename = f"{asset.kind}_{asset.id}{ext}"
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@api_bp.route("/api/home/today", methods=["GET"])
@login_required()
def home_today():
    user = g.get("user")
    timezone_name = _digest_timezone()
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
    today = datetime.now(tz).date()
    digest_day = today - timedelta(days=1)
    start, end = _utc_bounds_for_local_day(today, timezone_name)

    records = (
        _record_query_for(user, include_comments=False, public_only=True)
        .filter(Record.created_at >= start, Record.created_at < end)
        .order_by(Record.created_at.desc(), Record.id.desc())
        .all()
    )

    archive_state = _archive_and_index_records(
        day_value=today,
        records=records,
        scope="public",
        source="home_today",
        timezone_name=timezone_name,
    )
    vector_state = index_meta()
    if not vector_state.get("ready"):
        try:
            vector_state = ensure_index()
        except Exception as exc:
            vector_state = index_meta()
            vector_state["ready"] = False
            vector_state["error"] = str(exc)

    digest_record_count = _public_record_count_for_day(day_value=digest_day, timezone_name=timezone_name)
    digest_build_state = _maybe_auto_build_digest_for_day(
        day_value=digest_day,
        timezone_name=timezone_name,
        record_count=digest_record_count,
    )
    daily_assets = _daily_digest_assets_for_day(digest_day)
    digest_assets_payload = [_generated_asset_payload(item, viewer=user) for item in daily_assets]

    ai_settings = _ai_provider_settings()

    return jsonify(
        {
            "date": today.isoformat(),
            "timezone": timezone_name,
            "digest_day": digest_day.isoformat(),
            "public_records": [
                _record_payload(item, viewer=user, include_content=False, include_comments=False)
                for item in records
            ],
            "today_assets": digest_assets_payload,
            "digest_assets": digest_assets_payload,
            "digest_build": digest_build_state,
            "ai": {
                "enabled": bool(ai_settings),
                "message": (
                    f"AI 可用（provider={ai_settings.get('provider')}, model={ai_settings.get('model')})"
                    if ai_settings
                    else "未配置 AI provider，将使用非 AI 拼接渲染内容。"
                ),
            },
            "archive": archive_state,
            "vector": vector_state,
        }
    )


def _vector_chat_fallback_answer(query: str, hits: list[dict]) -> str:
    if not hits:
        return f"问题“{query}”在当前本地归档中没有检索到高相关内容。"

    lines = ["以下回答基于本地归档检索结果："]
    for idx, hit in enumerate(hits[:3], start=1):
        lines.append(
            f"{idx}. [{hit.get('day', '')} #{hit.get('record_id', 0)} | score={hit.get('score', 0)}] "
            f"{str(hit.get('snippet') or '').strip()}"
        )
    return "\n".join(lines)


@api_bp.route("/api/vector/rebuild", methods=["POST"])
@login_required()
def vector_rebuild():
    payload = request.get_json(silent=True) or {}
    max_docs = payload.get("max_docs")
    force = str(payload.get("force") or "0").strip().lower() in {"1", "true", "yes", "on"}
    try:
        result = build_index(
            max_docs=int(max_docs) if max_docs not in (None, "") else None,
            force=force,
        )
    except RuntimeError as exc:
        message = str(exc)
        if "not configured" in message:
            return jsonify({"error": message}), 501
        return jsonify({"error": message}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@api_bp.route("/api/vector/chat", methods=["POST"])
@login_required()
def vector_chat():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "missing query"}), 400

    top_k = max(1, min(int(payload.get("top_k") or get_setting_int("VECTOR_TOP_K", default=6)), 20))
    use_ai = str(payload.get("use_ai") if payload.get("use_ai") is not None else "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    try:
        result = vector_search(query, top_k=top_k)
    except RuntimeError as exc:
        message = str(exc)
        if "not configured" in message:
            return jsonify({"error": message}), 501
        return jsonify({"error": message}), 502
    hits = result.get("hits") or []
    citations = [
        {
            "id": hit.get("id"),
            "day": hit.get("day"),
            "record_id": hit.get("record_id"),
            "username": hit.get("username"),
            "tags": hit.get("tags") or [],
            "score": hit.get("score"),
            "snippet": hit.get("snippet") or "",
        }
        for hit in hits
    ]

    ai_answer = ""
    ai_used = False
    ai_error = ""
    if use_ai and hits and _ai_provider_settings():
        context_text = build_chat_context(hits, max_chars=5000)
        prompt = (
            f"用户问题：{query}\n\n"
            f"本地向量检索结果：\n{context_text}\n\n"
            "请只基于检索结果回答，若证据不足要明确说“归档中暂无充分依据”。"
        )
        messages = [
            {
                "role": "system",
                "content": get_setting_str(
                    "PROMPT_VECTOR_CHAT_SYSTEM",
                    default=DEFAULT_VECTOR_CHAT_SYSTEM_PROMPT,
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]
        try:
            ai_answer, _ = _ai_chat(messages=messages, temperature=0.2, max_tokens=800)
            ai_used = True
        except RuntimeError as exc:
            ai_error = str(exc)

    answer = ai_answer.strip() if ai_answer else _vector_chat_fallback_answer(query, hits)
    return jsonify(
        {
            "query": query,
            "answer": answer,
            "ai_used": ai_used,
            "ai_error": ai_error,
            "citations": citations,
            "hits_count": len(citations),
            "vector": result.get("meta") or index_meta(),
        }
    )
