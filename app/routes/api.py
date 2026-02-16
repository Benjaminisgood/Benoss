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
from sqlalchemy import and_, func, not_, or_
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Comment, Content, DailyDigestJob, GeneratedAsset, Record, User
from ..oss import delete_object, get_object_bytes, put_object_bytes, put_object_from_file, sign_get_url
from ..utils.ids import new_uuid
from ..utils.local_archive import archive_file_path, load_archive, save_daily_archive
from ..utils.local_vector_db import (
    build_chat_context,
    build_index,
    ensure_index,
    index_meta,
    search as vector_search,
)
from ..utils.oss_paths import generated_asset_key, record_content_key
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
_IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
_VIDEO_FILE_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi")
_AUDIO_FILE_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
_ECHO_FILE_TYPES = {"text", "image", "video", "audio", "file"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _preview_text(value: str, *, limit: int = 220) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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


def _content_media_type(content: Content) -> str:
    ctype = (content.content_type or "").lower()
    name = (content.filename or "").lower()
    if ctype.startswith("image/") or name.endswith(_IMAGE_FILE_EXTENSIONS):
        return "image"
    if ctype.startswith("video/") or name.endswith(_VIDEO_FILE_EXTENSIONS):
        return "video"
    if ctype.startswith("audio/") or name.endswith(_AUDIO_FILE_EXTENSIONS):
        return "audio"
    if ctype.startswith("text/") or name.endswith((".txt", ".md", ".json", ".py", ".js", ".html", ".css", ".csv")):
        return "text"
    return "file"


def _normalize_echo_file_type(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"", "all"}:
        return ""
    if value not in _ECHO_FILE_TYPES:
        raise ValueError("invalid file_type")
    return value


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


def _record_file_type_filter(file_type: str):
    ctype = func.lower(func.coalesce(Content.content_type, ""))
    filename = func.lower(func.coalesce(Content.filename, ""))
    text_mime_values = tuple(sorted(_TEXT_FILE_MIME_TYPES))
    text_suffix_values = tuple(sorted(_TEXT_FILE_EXTENSIONS))

    image_match = or_(ctype.like("image/%"), _suffix_like_filter(filename, _IMAGE_FILE_EXTENSIONS))
    video_match = or_(ctype.like("video/%"), _suffix_like_filter(filename, _VIDEO_FILE_EXTENSIONS))
    audio_match = or_(ctype.like("audio/%"), _suffix_like_filter(filename, _AUDIO_FILE_EXTENSIONS))
    text_match = or_(
        ctype.like("text/%"),
        ctype.in_(text_mime_values),
        _suffix_like_filter(filename, text_suffix_values),
    )

    if file_type == "text":
        return or_(
            Content.kind == "text",
            and_(Content.kind == "file", text_match),
        )
    if file_type == "image":
        return and_(Content.kind == "file", image_match)
    if file_type == "video":
        return and_(Content.kind == "file", video_match)
    if file_type == "audio":
        return and_(Content.kind == "file", audio_match)
    if file_type == "file":
        return and_(
            Content.kind == "file",
            not_(or_(image_match, video_match, audio_match, text_match)),
        )
    return None


def _asset_file_type_filter(file_type: str):
    kind = func.lower(func.coalesce(GeneratedAsset.kind, ""))
    ctype = func.lower(func.coalesce(GeneratedAsset.content_type, ""))
    ext = func.lower(func.coalesce(GeneratedAsset.ext, ""))
    text_mime_values = tuple(sorted(_TEXT_FILE_MIME_TYPES))
    text_ext_values = tuple(sorted(_TEXT_FILE_EXTENSIONS))

    image_match = or_(ctype.like("image/%"), ext.in_(_IMAGE_FILE_EXTENSIONS))
    video_match = or_(ctype.like("video/%"), ext.in_(_VIDEO_FILE_EXTENSIONS))
    audio_match = or_(ctype.like("audio/%"), ext.in_(_AUDIO_FILE_EXTENSIONS))
    text_match = or_(
        kind == "blog_html",
        ctype.like("text/%"),
        ctype.in_(text_mime_values),
        ext.in_(text_ext_values),
    )

    if file_type == "text":
        return text_match
    if file_type == "image":
        return image_match
    if file_type == "video":
        return video_match
    if file_type == "audio":
        return audio_match
    if file_type == "file":
        return and_(
            kind != "blog_html",
            not_(or_(image_match, video_match, audio_match, text_match)),
        )
    return None


def _record_echo_file_type(record: Record) -> str:
    if not record.content:
        return "file"
    if record.content.kind == "text":
        return "text"
    media = _content_media_type(record.content)
    return media if media in _ECHO_FILE_TYPES else "file"


def _asset_echo_file_type(asset: GeneratedAsset) -> str:
    kind = str(asset.kind or "").strip().lower()
    ctype = str(asset.content_type or "").split(";", 1)[0].strip().lower()
    ext = str(asset.ext or "").strip().lower()

    if kind == "blog_html":
        return "text"
    if ctype.startswith("image/") or ext in _IMAGE_FILE_EXTENSIONS:
        return "image"
    if ctype.startswith("video/") or ext in _VIDEO_FILE_EXTENSIONS:
        return "video"
    if ctype.startswith("audio/") or ext in _AUDIO_FILE_EXTENSIONS:
        return "audio"
    if ctype.startswith("text/") or ctype in _TEXT_FILE_MIME_TYPES or ext in _TEXT_FILE_EXTENSIONS:
        return "text"
    return "file"


def _content_payload(content: Content) -> dict:
    payload = {
        "id": content.id,
        "kind": content.kind,
        "created_at": _iso(content.created_at),
        "updated_at": _iso(content.updated_at),
    }
    if content.kind == "text":
        payload["text"] = content.text_content or ""
        payload["media_type"] = "text"
        return payload

    payload.update(
        {
            "filename": content.filename or "",
            "content_type": content.content_type or "",
            "size_bytes": int(content.size_bytes or 0),
            "sha256": content.sha256 or "",
            "media_type": _content_media_type(content),
            "blob_url": url_for("api.get_content_blob", content_id=content.id),
            "signed_url": sign_get_url(content.oss_key, expires=3600) if content.oss_key else "",
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
) -> dict:
    payload = {
        "id": record.id,
        "record_no": record.id,
        "format": record.format,
        "visibility": record.visibility,
        "tags": record.get_tags(),
        "preview": record.preview or "",
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "can_edit": bool(viewer and record.user_id == viewer.id),
        "can_comment": _is_record_visible(record, viewer),
        "user": {
            "id": record.user.id if record.user else record.user_id,
            "username": record.user.username if record.user else "",
        },
    }

    if include_content:
        payload["content"] = _content_payload(record.content)

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


def _detect_format(content: Content, requested: str | None) -> str:
    candidate = str(requested or "").strip().lower()
    if candidate:
        return candidate[:32]
    if content.kind == "text":
        return "text"
    return _content_media_type(content)


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


def _create_record_for_user(user: User):
    payload = _form_or_json_payload()
    tags = _parse_tags(payload.get("tags"))
    visibility = _normalize_visibility(payload.get("visibility"), default="private")
    requested_format = payload.get("format")

    file_obj = request.files.get("file")
    if file_obj:
        content, preview = _file_to_content(file_obj)
    else:
        text = str(payload.get("text") or "").strip()
        if not text:
            return jsonify({"error": "missing text or file"}), 400
        content = Content(kind="text", text_content=text)
        preview = _preview_text(text)

    record = Record(
        user_id=user.id,
        content=content,
        visibility=visibility,
        preview=preview,
    )
    record.set_tags(tags)
    record.format = _detect_format(content, requested_format)

    db.session.add(content)
    db.session.add(record)
    db.session.commit()

    loaded = (
        Record.query.options(joinedload(Record.user), joinedload(Record.content))
        .filter_by(id=record.id)
        .first()
    )
    return jsonify({"record": _record_payload(loaded, viewer=user, include_content=False, include_comments=False)}), 201


def _record_html_content(content: Content) -> str:
    payload = _content_payload(content)
    if payload["kind"] == "text":
        return f"<pre>{html.escape(payload.get('text') or '')}</pre>"

    media_type = payload.get("media_type")
    src = payload.get("blob_url") or payload.get("signed_url") or ""
    if not src:
        return "<p class=\"muted\">文件不可用</p>"

    escaped_src = html.escape(src, quote=True)
    escaped_name = html.escape(payload.get("filename") or "file")

    if media_type == "image":
        return f"<img src=\"{escaped_src}\" alt=\"{escaped_name}\">"
    if media_type == "video":
        return f"<video controls src=\"{escaped_src}\"></video>"
    if media_type == "audio":
        return f"<audio controls src=\"{escaped_src}\"></audio>"
    return f"<p><a href=\"{escaped_src}\" target=\"_blank\" rel=\"noreferrer\">{escaped_name}</a></p>"


def _render_notice_html(records: list[Record], *, day: str, user_id: str, tag: str) -> str:
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

    lines: list[str] = [
        "<article class=\"notice-render\">",
        "<header class=\"notice-render-summary\">",
        "<h2>Notice 内容拼接页</h2>",
        f"<p class=\"notice-summary-line\">总记录数: {len(records)}</p>",
    ]

    if filter_badges:
        lines.append("<p class=\"notice-summary-line\">筛选条件: " + " | ".join(filter_badges) + "</p>")
    if top_tags:
        lines.append(
            "<p class=\"notice-summary-line\">高频标签: "
            + ", ".join(f"#{html.escape(key)}" for key, _ in top_tags.most_common(10))
            + "</p>"
        )

    lines.append("</header>")

    current_day = ""
    for record in records:
        day_key = (record.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
        if day_key != current_day:
            if current_day:
                lines.append("</section>")
            lines.append(f"<section class=\"notice-day\"><p class=\"notice-day-label\">{day_key}</p>")
            current_day = day_key

        user_name = html.escape(record.user.username if record.user else "")
        stamp = html.escape((record.created_at or datetime.utcnow()).strftime("%H:%M"))
        visibility_text = html.escape(record.visibility or "")
        tags = [f"#{html.escape(tag_text)}" for tag_text in record.get_tags()]

        meta_parts: list[str] = []
        if stamp:
            meta_parts.append(f"<span>{stamp}</span>")
        if user_name:
            meta_parts.append(f"<span>{user_name}</span>")
        if visibility_text:
            meta_parts.append(f"<span>{visibility_text}</span>")
        if tags:
            meta_parts.append(f"<span class=\"notice-meta-tags\">{' '.join(tags)}</span>")
        meta_html = " <span class=\"notice-meta-sep\">·</span> ".join(meta_parts) or "<span class=\"muted\">记录</span>"

        preview_text = (record.preview or "").strip()
        content_kind = (record.content.kind if record.content else "").strip().lower()

        lines.extend(
            [
                "<article class=\"notice-block\">",
                f"<p class=\"notice-block-head\"><span class=\"notice-record-id\">#{record.id}</span>{meta_html}</p>",
            ]
        )
        if preview_text and content_kind != "text":
            lines.append(f"<p class=\"notice-preview\">{html.escape(preview_text)}</p>")
        lines.extend(
            [
                f"<div class=\"notice-block-body\">{_record_html_content(record.content)}</div>",
                "</article>",
            ]
        )

    lines.append("</section>")
    lines.append("</article>")
    return "\n".join(lines)


def _ai_provider_settings() -> dict | None:
    provider = _default_ai_provider()
    return _provider_settings_for_chat(provider)


def _normalize_provider(raw: str) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "open_ai": "openai",
        "open-ai": "openai",
        "chat_anywhere": "chatanywhere",
        "chat-anywhere": "chatanywhere",
        "dashscope": "aliyun",
    }
    return aliases.get(value, value)


def _default_ai_provider() -> str:
    primary = _normalize_provider(get_setting_str("AI_PRIMARY_PROVIDER", default=""))
    return primary


def _provider_raw_config(provider: str) -> dict | None:
    choices = {
        "openai": {
            "api_key": get_setting_str("OPENAI_API_KEY", default=""),
            "base_url": get_setting_str("OPENAI_API_BASE_URL", default="https://api.openai.com/v1"),
            "chat_model": get_setting_str("OPENAI_CHAT_MODEL", default="gpt-4o-mini"),
        },
        "chatanywhere": {
            "api_key": get_setting_str("CHAT_ANYWHERE_API_KEY", default=""),
            "base_url": get_setting_str("CHAT_ANYWHERE_API_BASE_URL", default="https://api.chatanywhere.tech/v1"),
            "chat_model": get_setting_str("CHAT_ANYWHERE_CHAT_MODEL", default="gpt-4o-mini"),
        },
        "deepseek": {
            "api_key": get_setting_str("DEEPSEEK_API_KEY", default=""),
            "base_url": get_setting_str("DEEPSEEK_API_BASE_URL", default="https://api.deepseek.com/v1"),
            "chat_model": get_setting_str("DEEPSEEK_CHAT_MODEL", default="deepseek-chat"),
        },
        "aliyun": {
            "api_key": get_setting_str("ALIYUN_AI_API_KEY", default=""),
            "base_url": get_setting_str("ALIYUN_AI_API_BASE_URL", default="https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "chat_model": get_setting_str("ALIYUN_AI_CHAT_MODEL", default="qwen-plus"),
        },
    }
    selected = choices.get(provider)
    if not selected:
        return None

    api_key = str(selected.get("api_key") or "").strip()
    base_url = str(selected.get("base_url") or "").strip().rstrip("/")
    chat_model = str(selected.get("chat_model") or "").strip()
    if not api_key or not base_url:
        return None
    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
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
    defaults = {
        "tts": {
            "openai": "gpt-4o-mini-tts",
            "chatanywhere": "gpt-4o-mini-tts",
            "deepseek": "unsupported",
            "aliyun": "qwen3-tts-instruct-flash",
        },
        "image": {
            "openai": "gpt-image-1",
            "chatanywhere": "gpt-image-1",
            "deepseek": "unsupported",
            "aliyun": "qwen-image-max",
        },
    }
    selected = defaults.get(capability) or {}
    return str(selected.get(provider) or "")


def _capability_provider_order(capability: str) -> list[str]:
    provider_key = {
        "tts": "AI_TTS_PROVIDER",
        "image": "AI_IMAGE_PROVIDER",
    }.get(capability, "")

    ordered: list[str] = []
    if provider_key:
        ordered.append(_normalize_provider(get_setting_str(provider_key, default="")))
    ordered.append(_default_ai_provider())
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
    keys = {
        "tts": {
            "openai": "OPENAI_TTS_MODEL",
            "chatanywhere": "CHAT_ANYWHERE_TTS_MODEL",
            "deepseek": "DEEPSEEK_TTS_MODEL",
            "aliyun": "ALIYUN_AI_TTS_MODEL",
        },
        "image": {
            "openai": "OPENAI_IMAGE_MODEL",
            "chatanywhere": "CHAT_ANYWHERE_IMAGE_MODEL",
            "deepseek": "DEEPSEEK_IMAGE_MODEL",
            "aliyun": "ALIYUN_AI_IMAGE_MODEL",
        },
    }
    selected = keys.get(capability) or {}
    return str(selected.get(provider) or "")


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
    ctype = str(content.content_type or "").split(";", 1)[0].strip().lower()
    filename = str(content.filename or "").lower()
    suffix = Path(filename).suffix

    if ctype.startswith("text/"):
        return True
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
        text = _normalize_prompt_text(record.content.text_content or "")
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
        payload = {
            "model": settings["model"],
            "voice": tts_voice,
            "input": f"{style_hint}\n\n{safe_text}",
            "response_format": response_format,
        }
        try:
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
        payload = {
            "model": settings["model"],
            "prompt": prompt,
            "size": "1024x1024",
            "response_format": "b64_json",
        }
        try:
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
        image_bytes = _render_local_poster_svg(prompt)
        return image_bytes, "image/svg+xml", ".svg", {"provider": "local", "model": "local-svg-poster-v1"}

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


def _render_local_poster_svg(prompt: str) -> bytes:
    prompt_text = _normalize_prompt_text(prompt)
    if not prompt_text:
        prompt_text = "Daily Digest Poster"

    lines = _wrap_lines(prompt_text, width=26, max_lines=14)
    if not lines:
        lines = ["Daily Digest Poster"]

    y_start = 170
    line_gap = 56
    line_items: list[str] = []
    for idx, line in enumerate(lines):
        safe = html.escape(line)
        y_pos = y_start + idx * line_gap
        line_items.append(f'<text x="92" y="{y_pos}" font-size="34" fill="#132037">{safe}</text>')

    body = "\n    ".join(line_items)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">\n'
        "  <defs>\n"
        '    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">\n'
        '      <stop offset="0%" stop-color="#f8fbff"/>\n'
        '      <stop offset="100%" stop-color="#e5eefc"/>\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        '  <rect width="1024" height="1024" fill="url(#bg)"/>\n'
        '  <rect x="64" y="72" width="896" height="880" rx="28" fill="#ffffff" stroke="#d3dded" stroke-width="2"/>\n'
        '  <text x="92" y="122" font-size="28" fill="#4a5f82">Benoss Local Poster Fallback</text>\n'
        f"  {body}\n"
        "</svg>\n"
    )
    return svg.encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _generated_asset_payload(asset: GeneratedAsset) -> dict:
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
        "content_type": asset.content_type,
        "ext": asset.ext,
        "sha256": asset.sha256,
        "user": {
            "id": asset.user.id if asset.user else asset.user_id,
            "username": asset.user.username if asset.user else "",
        },
        "size_bytes": int(asset.size_bytes or 0),
        "created_at": _iso(asset.created_at),
        "blob_url": url_for("api.generated_asset_blob", asset_id=asset.id),
    }


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


def _wrap_blog_html_document(body_html: str, *, title: str) -> str:
    raw = str(body_html or "").strip()
    if "<html" in raw.lower():
        return raw
    safe_title = html.escape(title or "Daily Digest")
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
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <main>\n"
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
    source_day: date | None = None,
    is_daily_digest: bool = False,
) -> tuple[GeneratedAsset, str]:
    output, settings = _ai_chat(
        messages=_build_notice_ai_prompt("blog", records_text, image_urls=image_urls),
        temperature=0.25,
        max_tokens=2000,
    )
    html_doc = _wrap_blog_html_document(output, title=title)
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
    poster_prompt, prompt_settings = _ai_chat(
        messages=poster_prompt_messages,
        temperature=0.45,
        max_tokens=700,
    )
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
    digest_podcast_style = _normalize_podcast_style(
        get_setting_str("PODCAST_DEFAULT_STYLE", default="dialogue")
    )

    kind_map = {
        "blog_html": {
            "title": f"Daily Digest Blog {day_value.isoformat()}",
            "job_field": "blog_asset_id",
        },
        "podcast_audio": {
            "title": f"Daily Digest Podcast {day_value.isoformat()}",
            "job_field": "podcast_asset_id",
        },
        "poster_image": {
            "title": f"Daily Digest Poster {day_value.isoformat()}",
            "job_field": "poster_asset_id",
        },
    }
    assets: dict[str, GeneratedAsset] = {}
    errors: list[str] = []

    for kind, meta in kind_map.items():
        existing = None
        if not force:
            existing = (
                GeneratedAsset.query.options(joinedload(GeneratedAsset.user))
                .filter(
                    GeneratedAsset.kind == kind,
                    GeneratedAsset.visibility == "public",
                    GeneratedAsset.status == "ready",
                    GeneratedAsset.is_daily_digest.is_(True),
                    GeneratedAsset.source_day == day_value,
                )
                .order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id.desc())
                .first()
            )
        if existing:
            assets[kind] = existing
            setattr(job, meta["job_field"], existing.id)
            continue

        try:
            if kind == "blog_html":
                asset, _ = _generate_blog_asset(
                    user=owner,
                    records_text=records_text,
                    image_urls=image_urls,
                    filters=base_filters,
                    title=meta["title"],
                    visibility="public",
                    source_day=day_value,
                    is_daily_digest=True,
                )
            elif kind == "podcast_audio":
                asset, _ = _generate_podcast_asset(
                    user=owner,
                    records_text=records_text,
                    image_urls=image_urls,
                    filters=base_filters,
                    title=meta["title"],
                    visibility="public",
                    podcast_style=digest_podcast_style,
                    source_day=day_value,
                    is_daily_digest=True,
                )
            else:
                asset, _ = _generate_poster_asset(
                    user=owner,
                    records_text=records_text,
                    image_urls=image_urls,
                    filters=base_filters,
                    title=meta["title"],
                    visibility="public",
                    source_day=day_value,
                    is_daily_digest=True,
                )
            assets[kind] = asset
            setattr(job, meta["job_field"], asset.id)
        except RuntimeError as exc:
            errors.append(f"{kind}: {exc}")
        except Exception as exc:
            errors.append(f"{kind}: {exc}")

    if len(assets) == len(kind_map):
        job.status = "ready"
    elif assets:
        job.status = "partial"
    else:
        job.status = "failed"
    job.finished_at = datetime.utcnow()
    job.error = " | ".join(errors)[:4000] if errors else ""
    db.session.add(job)
    db.session.commit()

    ordered_assets = sorted(assets.values(), key=lambda item: item.created_at or datetime.utcnow(), reverse=True)
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
    if not records:
        return {"saved": False, "reason": "no_records"}

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
        return {"saved": False, "reason": str(exc)}

    vector_info = index_meta()
    if scope == "public" and archive_info.get("changed") and get_setting_bool("VECTOR_AUTO_REBUILD", default=True):
        try:
            vector_info = build_index(max_docs=get_setting_int("VECTOR_MAX_DOCS", default=4000))
        except Exception as exc:
            current_app.logger.warning("vector rebuild failed: %s", exc)

    return {
        "saved": True,
        "archive": archive_info,
        "vector": vector_info,
    }


def _daily_digest_assets_for_day(day_value: date) -> list[GeneratedAsset]:
    return (
        GeneratedAsset.query.options(joinedload(GeneratedAsset.user))
        .filter(
            GeneratedAsset.visibility == "public",
            GeneratedAsset.status == "ready",
            GeneratedAsset.is_daily_digest.is_(True),
            GeneratedAsset.source_day == day_value,
        )
        .order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id.desc())
        .all()
    )


def _maybe_auto_build_today_digest(*, day_value: date, timezone_name: str, record_count: int) -> dict:
    enabled = get_setting_bool("HOME_AUTO_BUILD_DAILY_ASSETS", default=True)
    state = {
        "enabled": bool(enabled),
        "triggered": False,
        "status": "disabled" if not enabled else "skipped",
        "message": "auto build disabled" if not enabled else "",
    }
    if not enabled:
        return state

    if record_count <= 0:
        state["status"] = "no_records"
        state["message"] = "today has no public records"
        return state

    if not _ai_provider_settings():
        state["status"] = "ai_not_configured"
        state["message"] = "AI provider not configured"
        return state

    ready_assets = _daily_digest_assets_for_day(day_value)
    ready_kinds = {item.kind for item in ready_assets}
    required_kinds = {"blog_html", "podcast_audio", "poster_image"}
    if required_kinds.issubset(ready_kinds):
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
    records = query.order_by(Record.created_at.desc(), Record.id.desc()).limit(per).all()
    return jsonify(
        {
            "items": [
                _record_payload(item, viewer=user, include_content=True, include_comments=False)
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
    return jsonify(
        {
            "record": _record_payload(
                record,
                viewer=user,
                include_content=True,
                include_comments=include_comments,
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

    if "visibility" in payload:
        record.visibility = _normalize_visibility(payload.get("visibility"), default=record.visibility)

    if "tags" in payload:
        record.set_tags(_parse_tags(payload.get("tags")))

    if record.content.kind == "text" and "text" in payload:
        text = str(payload.get("text") or "").strip()
        if not text:
            return jsonify({"error": "text cannot be empty"}), 400
        record.content.text_content = text
        record.preview = _preview_text(text)

    new_file = request.files.get("file")
    if new_file:
        old_key = record.content.oss_key
        content, preview = _file_to_content(new_file)

        record.content.kind = content.kind
        # Content.text_content is non-null in schema; keep empty string for file records.
        record.content.text_content = content.text_content or ""
        record.content.filename = content.filename or ""
        record.content.content_type = content.content_type or ""
        record.content.size_bytes = int(content.size_bytes or 0)
        record.content.sha256 = content.sha256 or ""
        record.content.oss_key = content.oss_key or ""

        record.preview = preview
        record.format = _detect_format(record.content, payload.get("format"))

        if old_key:
            try:
                delete_object(old_key)
            except Exception:
                pass

    if "format" in payload and not new_file:
        record.format = _detect_format(record.content, payload.get("format"))

    db.session.commit()
    return jsonify({"ok": True})


@api_bp.route("/api/records/<int:record_id>", methods=["DELETE"])
@login_required()
def delete_record(record_id: int):
    user = g.get("user")
    record = Record.query.options(joinedload(Record.content)).filter_by(id=record_id).first_or_404()

    if record.user_id != user.id:
        return jsonify({"error": "forbidden"}), 403

    oss_key = record.content.oss_key if record.content and record.content.kind == "file" else ""
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
        return Response(content.text_content or "", mimetype="text/plain; charset=utf-8")

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
    records_query = _record_query_for(user, include_comments=False, public_only=True)
    assets_query = GeneratedAsset.query.options(joinedload(GeneratedAsset.user)).filter(
        GeneratedAsset.visibility == "public",
        GeneratedAsset.status == "ready",
    )

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
        record_payload = _record_payload(item, viewer=user, include_content=True, include_comments=False)
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
        asset_payload = _generated_asset_payload(item)
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

    html_output = _render_notice_html(records, day=day, user_id=user_id, tag=tag)

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
            "items": [_generated_asset_payload(item) for item in assets],
            "total": total,
            "page": page,
            "per": per,
        }
    )


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
    if asset.visibility != "public" and asset.user_id != user.id:
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

    digest_build_state = _maybe_auto_build_today_digest(
        day_value=today,
        timezone_name=timezone_name,
        record_count=len(records),
    )
    daily_assets = _daily_digest_assets_for_day(today)

    ai_settings = _ai_provider_settings()

    return jsonify(
        {
            "date": today.isoformat(),
            "timezone": timezone_name,
            "public_records": [
                _record_payload(item, viewer=user, include_content=False, include_comments=False)
                for item in records
            ],
            "today_assets": [_generated_asset_payload(item) for item in daily_assets],
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
