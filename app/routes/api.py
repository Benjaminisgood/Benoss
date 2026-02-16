from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, Response, current_app, g, jsonify, request, url_for
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Comment, Content, DailyDigestJob, GeneratedAsset, Record, User
from ..oss import delete_object, get_object_bytes, put_object_bytes, put_object_from_file, sign_get_url
from ..utils.ids import new_uuid
from ..utils.local_archive import save_daily_archive
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
    if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
        return "image"
    if ctype.startswith("video/") or name.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi")):
        return "video"
    if ctype.startswith("audio/") or name.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
        return "audio"
    if ctype.startswith("text/") or name.endswith((".txt", ".md", ".json", ".py", ".js", ".html", ".css", ".csv")):
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
        "<header>",
        "<h2>Notice 内容拼接页</h2>",
        f"<p>总记录数: {len(records)}</p>",
    ]

    if filter_badges:
        lines.append("<p>筛选条件: " + " | ".join(filter_badges) + "</p>")
    if top_tags:
        lines.append(
            "<p>高频标签: "
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
            lines.append(f"<section class=\"notice-day\"><h3>{day_key}</h3>")
            current_day = day_key

        user_name = html.escape(record.user.username if record.user else "")
        stamp = html.escape((record.created_at or datetime.utcnow()).strftime("%H:%M"))
        tags_text = " ".join(f"<span class=\"tag-pill\">#{html.escape(t)}</span>" for t in record.get_tags())
        if not tags_text:
            tags_text = "<span class=\"muted\">无标签</span>"

        lines.extend(
            [
                "<article class=\"notice-block\">",
                f"<div class=\"notice-block-head\"><strong>#{record.id}</strong><span>{user_name} · {stamp} · {html.escape(record.visibility)}</span></div>",
                f"<p>{html.escape(record.preview or '')}</p>",
                f"<div class=\"tag-line\">{tags_text}</div>",
                _record_html_content(record.content),
                "</article>",
            ]
        )

    lines.append("</section>")
    lines.append("</article>")
    return "\n".join(lines)


def _ai_provider_settings() -> dict | None:
    raw = get_setting_str("AI_AUTOFILL_PROVIDER", default="").strip().lower()
    aliases = {
        "chat_anywhere": "chatanywhere",
        "chat-anywhere": "chatanywhere",
        "dashscope": "aliyun",
    }
    provider = aliases.get(raw, raw)
    if not provider:
        return None

    choices = {
        "chatanywhere": {
            "api_key": get_setting_str("CHAT_ANYWHERE_API_KEY", default=""),
            "base_url": get_setting_str("CHAT_ANYWHERE_API_BASE_URL", default="https://api.chatanywhere.tech/v1"),
            "model": get_setting_str("CHAT_ANYWHERE_MODEL", default="gpt-4o-mini"),
        },
        "deepseek": {
            "api_key": get_setting_str("DEEPSEEK_API_KEY", default=""),
            "base_url": get_setting_str("DEEPSEEK_API_BASE_URL", default="https://api.deepseek.com/v1"),
            "model": get_setting_str("DEEPSEEK_MODEL", default="deepseek-chat"),
        },
        "aliyun": {
            "api_key": get_setting_str("ALIYUN_AI_API_KEY", default=""),
            "base_url": get_setting_str("ALIYUN_AI_API_BASE_URL", default="https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "model": get_setting_str("ALIYUN_AI_MODEL", default="qwen-plus"),
        },
    }
    selected = choices.get(provider)
    if not selected:
        return None

    api_key = str(selected.get("api_key") or "").strip()
    base_url = str(selected.get("base_url") or "").strip().rstrip("/")
    model = str(selected.get("model") or "").strip()
    if not api_key or not base_url or not model:
        return None

    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }


def _ai_chat(*, messages: list[dict], temperature: float = 0.2, max_tokens: int = 1800) -> tuple[str, dict]:
    settings = _ai_provider_settings()
    if not settings:
        raise RuntimeError("AI provider not configured")

    endpoint = settings["base_url"] + "/chat/completions"
    timeout = get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45)
    payload = {
        "model": settings["model"],
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
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
    content = str(message.get("content") or "").strip()
    if not content:
        raise RuntimeError("provider returned empty content")

    return content, settings


def _records_for_ai_prompt(records: list[Record], *, max_chars: int = 18000) -> str:
    chunks: list[str] = []
    for record in records:
        tags = ",".join(record.get_tags()) or "-"
        created = (record.created_at or datetime.utcnow()).isoformat()
        if record.content.kind == "text":
            content_preview = (record.content.text_content or "").strip()
        else:
            content_preview = f"[FILE] {record.content.filename or 'unknown'}"
        content_preview = content_preview[:450]
        chunks.append(
            f"[#{record.id}] user={record.user.username if record.user else record.user_id} "
            f"time={created} visibility={record.visibility} tags={tags}\n{content_preview}"
        )
        if sum(len(x) + 2 for x in chunks) > max_chars:
            break
    return "\n\n".join(chunks)


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


def _build_notice_ai_prompt(action: str, records_text: str, *, podcast_style: str = "dialogue") -> list[dict]:
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
    return [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": f"{task}\n\n输入记录如下：\n{records_text}",
        },
    ]


def _ai_request_json(path: str, payload: dict, *, timeout: int | None = None) -> tuple[dict, dict]:
    settings = _ai_provider_settings()
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


def _ai_request_binary(path: str, payload: dict, *, timeout: int | None = None) -> tuple[bytes, str, dict]:
    settings = _ai_provider_settings()
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


def _ai_tts_audio(script: str, *, podcast_style: str) -> tuple[bytes, str, str, dict]:
    tts_model = get_setting_str("AI_TTS_MODEL", default="gpt-4o-mini-tts").strip()
    if not tts_model:
        raise RuntimeError("AI_TTS_MODEL not configured")

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
    payload = {
        "model": tts_model,
        "voice": tts_voice,
        "input": f"{style_hint}\n\n{safe_text}",
        "response_format": response_format,
    }
    timeout = max(45, get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45))
    audio_bytes, returned_content_type, settings = _ai_request_binary("/audio/speech", payload, timeout=timeout)
    if not audio_bytes:
        raise RuntimeError("audio generation returned empty bytes")

    content_type, ext = _audio_format_meta(response_format)
    if returned_content_type and "application/json" not in returned_content_type:
        content_type = returned_content_type.split(";")[0].strip() or content_type
    return (
        audio_bytes,
        content_type,
        ext,
        {
            "provider": settings["provider"],
            "model": tts_model,
            "voice": tts_voice,
        },
    )


def _ai_generate_poster_image(prompt: str) -> tuple[bytes, str, str, dict]:
    image_model = get_setting_str("AI_IMAGE_MODEL", default="gpt-image-1").strip()
    if not image_model:
        raise RuntimeError("AI_IMAGE_MODEL not configured")

    payload = {
        "model": image_model,
        "prompt": prompt,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    data, settings = _ai_request_json("/images/generations", payload)

    items = data.get("data") or []
    first = items[0] if items else {}
    b64_json = str(first.get("b64_json") or "").strip()
    url = str(first.get("url") or "").strip()

    if b64_json:
        try:
            image_bytes = base64.b64decode(b64_json)
        except Exception as exc:
            raise RuntimeError("image response base64 decode failed") from exc
    elif url:
        try:
            response = requests.get(url, timeout=get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45))
            response.raise_for_status()
            image_bytes = response.content
        except requests.RequestException as exc:
            raise RuntimeError(f"image download failed: {exc}") from exc
    else:
        raise RuntimeError("image response missing b64_json/url")

    if not image_bytes:
        raise RuntimeError("image generation returned empty bytes")

    return image_bytes, "image/png", ".png", {"provider": settings["provider"], "model": image_model}


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
    filters: dict,
    title: str,
    visibility: str,
    source_day: date | None = None,
    is_daily_digest: bool = False,
) -> tuple[GeneratedAsset, str]:
    output, settings = _ai_chat(messages=_build_notice_ai_prompt("blog", records_text), temperature=0.25, max_tokens=2000)
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
    filters: dict,
    title: str,
    visibility: str,
    podcast_style: str = "dialogue",
    source_day: date | None = None,
    is_daily_digest: bool = False,
) -> tuple[GeneratedAsset, str]:
    style = _normalize_podcast_style(podcast_style)
    script, script_info = _ai_chat(
        messages=_build_notice_ai_prompt("podcast", records_text, podcast_style=style),
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
    poster_prompt_messages = [
        {
            "role": "system",
            "content": poster_system_prompt,
        },
        {
            "role": "user",
            "content": poster_user_prompt,
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

    limit = get_setting_int("AI_MAX_NOTICE_RECORDS", default=180)
    records = (
        Record.query.options(joinedload(Record.user), joinedload(Record.content))
        .filter(
            Record.visibility == "public",
            Record.created_at >= start,
            Record.created_at < end,
        )
        .order_by(Record.created_at.asc(), Record.id.asc())
        .limit(max(20, min(limit, 500)))
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

    records_text = _records_for_ai_prompt(records)
    if not records_text:
        job.status = "failed"
        job.finished_at = datetime.utcnow()
        job.error = "public records are empty after prompt reduction"
        db.session.add(job)
        db.session.commit()
        return {
            "day": day_value.isoformat(),
            "timezone": tz_name,
            "record_count": len(records),
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
        "record_count": len(records),
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
        record.content.text_content = content.text_content
        record.content.filename = content.filename
        record.content.content_type = content.content_type
        record.content.size_bytes = content.size_bytes
        record.content.sha256 = content.sha256
        record.content.oss_key = content.oss_key

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
    query = _record_query_for(user, include_comments=False, public_only=True)

    tag = str(request.args.get("tag") or "").strip()
    if tag:
        query = query.filter(Record.tags_json.contains(f'"{tag}"'))

    day = str(request.args.get("day") or "").strip()
    day_value = None
    if day:
        try:
            day_value = _parse_day(day)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        start, end = _day_bounds(day_value)
        query = query.filter(Record.created_at >= start, Record.created_at < end)

    page = max(int(request.args.get("page") or 1), 1)
    per = min(max(int(request.args.get("per") or 40), 1), 100)

    total = query.order_by(None).count()
    records = query.order_by(Record.created_at.desc(), Record.id.desc()).offset((page - 1) * per).limit(per).all()
    assets_query = GeneratedAsset.query.options(joinedload(GeneratedAsset.user)).filter(
        GeneratedAsset.visibility == "public",
        GeneratedAsset.status == "ready",
    )
    if day_value:
        assets_query = assets_query.filter(GeneratedAsset.source_day == day_value)
    if tag:
        assets_query = assets_query.filter(GeneratedAsset.source_filters_json.contains(f'"tag": "{tag}"'))
    assets = assets_query.order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id.desc()).limit(per).all()

    return jsonify(
        {
            "items": [
                _record_payload(item, viewer=user, include_content=True, include_comments=False)
                for item in records
            ],
            "assets": [_generated_asset_payload(item) for item in assets],
            "total": total,
            "page": page,
            "per": per,
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


@api_bp.route("/api/notice/ai", methods=["POST"])
@login_required()
def notice_ai():
    return jsonify({"error": "deprecated endpoint: use /api/notice/assets with action=blog"}), 410


@api_bp.route("/api/notice/assets", methods=["POST"])
@login_required()
def notice_assets():
    user = g.get("user")
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"blog", "podcast", "poster"}:
        return jsonify({"error": "invalid action"}), 400

    visibility = _normalize_visibility(payload.get("visibility"), default="private")
    public_only = str(payload.get("public_only") or "0").strip().lower() in {"1", "true", "yes", "on"}
    filters = payload.get("filters") or {}
    user_id = str(filters.get("user_id") or "").strip()
    tag = str(filters.get("tag") or "").strip()
    day = str(filters.get("day") or "").strip()
    source_day = None
    order = str(filters.get("order") or "asc").strip().lower()
    podcast_style = _normalize_podcast_style(payload.get("podcast_style"))

    query = _record_query_for(user, include_comments=False, public_only=public_only)
    try:
        query = _apply_filter_values(query, user_id=user_id, tag=tag, day=day)
        source_day = _parse_day(day) if day else None
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if order == "desc":
        query = query.order_by(Record.created_at.desc(), Record.id.desc())
    else:
        query = query.order_by(Record.created_at.asc(), Record.id.asc())

    limit = get_setting_int("AI_MAX_NOTICE_RECORDS", default=180)
    records = query.limit(max(20, min(limit, 500))).all()
    records_text = _records_for_ai_prompt(records)
    if not records_text:
        return jsonify({"error": "no records for current filters"}), 400

    asset_filters = dict(filters)
    asset_filters["public_only"] = bool(public_only)
    if action == "podcast":
        asset_filters["podcast_style"] = podcast_style
        if public_only:
            archive_day = source_day or datetime.utcnow().date()
            _archive_and_index_records(
                day_value=archive_day,
                records=records,
                scope="public",
                source="notice_assets",
                timezone_name=_digest_timezone(),
            )

    try:
        if action == "blog":
            asset, blog_html = _generate_blog_asset(
                user=user,
                records_text=records_text,
                filters=asset_filters,
                title=f"Notice Blog {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                visibility=visibility,
                source_day=source_day,
            )
            return jsonify(
                {
                    "action": action,
                    "record_count": len(records),
                    "asset": _generated_asset_payload(asset),
                    "blog_html": blog_html,
                }
            )

        if action == "podcast":
            asset, script = _generate_podcast_asset(
                user=user,
                records_text=records_text,
                filters=asset_filters,
                title=f"Notice Podcast {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                visibility=visibility,
                podcast_style=podcast_style,
                source_day=source_day,
            )
            return jsonify(
                {
                    "action": action,
                    "record_count": len(records),
                    "podcast_style": podcast_style,
                    "asset": _generated_asset_payload(asset),
                    "transcript": script,
                }
            )

        asset, poster_prompt = _generate_poster_asset(
            user=user,
            records_text=records_text,
            filters=asset_filters,
            title=f"Notice Poster {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            visibility=visibility,
            source_day=source_day,
        )
        return jsonify(
            {
                "action": action,
                "record_count": len(records),
                "asset": _generated_asset_payload(asset),
                "poster_prompt": poster_prompt,
            }
        )
    except RuntimeError as exc:
        message = str(exc)
        if "not configured" in message:
            return jsonify({"error": message}), 501
        return jsonify({"error": message}), 502


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

    today = datetime.utcnow().date()
    start, end = _day_bounds(today)

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
        timezone_name=_digest_timezone(),
    )
    vector_state = index_meta()
    if not vector_state.get("ready"):
        vector_state = ensure_index()

    ai_settings = _ai_provider_settings()

    return jsonify(
        {
            "date": today.isoformat(),
            "public_records": [
                _record_payload(item, viewer=user, include_content=False, include_comments=False)
                for item in records
            ],
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
    try:
        result = build_index(max_docs=int(max_docs) if max_docs not in (None, "") else None)
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

    result = vector_search(query, top_k=top_k)
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
