from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from flask import Blueprint, Response, current_app, g, jsonify, request, url_for
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Comment, Content, GeneratedAsset, Record, User
from ..oss import delete_object, get_object_bytes, put_object_bytes, put_object_from_file, sign_get_url
from ..utils.ids import new_uuid
from ..utils.oss_paths import generated_asset_key, record_content_key
from ..utils.session_auth import login_required


api_bp = Blueprint("api", __name__)

_VALID_VISIBILITY = {"public", "private"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    raw = str(current_app.config.get("AI_AUTOFILL_PROVIDER") or "").strip().lower()
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
            "api_key": current_app.config.get("CHAT_ANYWHERE_API_KEY"),
            "base_url": current_app.config.get("CHAT_ANYWHERE_API_BASE_URL"),
            "model": current_app.config.get("CHAT_ANYWHERE_MODEL"),
        },
        "deepseek": {
            "api_key": current_app.config.get("DEEPSEEK_API_KEY"),
            "base_url": current_app.config.get("DEEPSEEK_API_BASE_URL"),
            "model": current_app.config.get("DEEPSEEK_MODEL"),
        },
        "aliyun": {
            "api_key": current_app.config.get("ALIYUN_AI_API_KEY"),
            "base_url": current_app.config.get("ALIYUN_AI_API_BASE_URL"),
            "model": current_app.config.get("ALIYUN_AI_MODEL"),
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
    timeout = int(current_app.config.get("AI_REQUEST_TIMEOUT_SECONDS") or 45)
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


def _build_notice_ai_prompt(action: str, records_text: str) -> list[dict]:
    if action == "podcast":
        task = "把输入记录整理成一个 3-5 分钟中文播客稿，分段清晰，有开场、主体、结尾。"
    elif action == "poster":
        task = "把输入记录提炼成一份中文海报文案，包含标题、3-6 个重点、结语。"
    else:
        task = (
            "把输入记录生成一段可直接插入网页的 HTML 片段。"
            "必须返回纯 HTML（不要 markdown 代码块），结构清晰，按时间顺序，"
            "并尽量保留每条记录的核心内容。"
        )

    return [
        {
            "role": "system",
            "content": "你是学习小组内容编辑助手，要求输出准确、紧凑、可读。",
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
    timeout_seconds = int(timeout or current_app.config.get("AI_REQUEST_TIMEOUT_SECONDS") or 45)
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


def _ai_tts_audio(script_text: str) -> tuple[bytes, str, dict]:
    settings = _ai_provider_settings()
    if not settings:
        raise RuntimeError("AI provider not configured")

    tts_model = str(current_app.config.get("AI_TTS_MODEL") or "").strip()
    if not tts_model:
        raise RuntimeError("AI_TTS_MODEL not configured")
    tts_voice = str(current_app.config.get("AI_TTS_VOICE") or "alloy").strip() or "alloy"

    endpoint = settings["base_url"] + "/audio/speech"
    timeout_seconds = int(current_app.config.get("AI_REQUEST_TIMEOUT_SECONDS") or 45)
    payload = {
        "model": tts_model,
        "input": script_text,
        "voice": tts_voice,
        "format": "mp3",
    }
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise RuntimeError(f"tts request error: {exc}") from exc

    if not response.ok:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 320:
            detail = detail[:320] + "..."
        raise RuntimeError(f"tts request failed ({response.status_code}): {detail}")

    content_type = str(response.headers.get("Content-Type") or "audio/mpeg").split(";")[0].strip().lower()
    if "json" in content_type:
        raise RuntimeError("tts response is json instead of audio")

    audio_bytes = response.content or b""
    if not audio_bytes:
        raise RuntimeError("tts returned empty audio")

    return audio_bytes, "audio/mpeg", {"provider": settings["provider"], "model": tts_model}


def _ai_generate_poster_image(prompt: str) -> tuple[bytes, str, str, dict]:
    image_model = str(current_app.config.get("AI_IMAGE_MODEL") or "").strip()
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
            response = requests.get(url, timeout=int(current_app.config.get("AI_REQUEST_TIMEOUT_SECONDS") or 45))
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
        "provider": asset.provider,
        "model": asset.model,
        "content_type": asset.content_type,
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

    days = int(request.args.get("days") or current_app.config.get("BOARD_DEFAULT_DAYS") or 7)
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

    return jsonify(
        {
            "items": [
                _record_payload(item, viewer=user, include_content=True, include_comments=False)
                for item in records
            ],
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
    user = g.get("user")
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "optimize").strip().lower()
    if action != "optimize":
        return jsonify({"error": "action must be optimize; use /api/notice/assets for podcast/poster"}), 400

    filters = payload.get("filters") or {}
    user_id = str(filters.get("user_id") or "").strip()
    tag = str(filters.get("tag") or "").strip()
    day = str(filters.get("day") or "").strip()
    order = str(filters.get("order") or "asc").strip().lower()

    query = _record_query_for(user, include_comments=False, public_only=False)
    try:
        query = _apply_filter_values(query, user_id=user_id, tag=tag, day=day)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if order == "desc":
        query = query.order_by(Record.created_at.desc(), Record.id.desc())
    else:
        query = query.order_by(Record.created_at.asc(), Record.id.asc())

    limit = int(current_app.config.get("AI_MAX_NOTICE_RECORDS") or 180)
    records = query.limit(max(20, min(limit, 500))).all()
    records_text = _records_for_ai_prompt(records)
    if not records_text:
        return jsonify({"error": "no records for current filters"}), 400

    messages = _build_notice_ai_prompt("optimize", records_text)
    try:
        output, settings = _ai_chat(messages=messages)
    except RuntimeError as exc:
        message = str(exc)
        if "not configured" in message:
            return jsonify({"error": message}), 501
        return jsonify({"error": message}), 502

    return jsonify(
        {
            "action": action,
            "provider": settings["provider"],
            "model": settings["model"],
            "record_count": len(records),
            "output": output,
        }
    )


@api_bp.route("/api/notice/assets", methods=["POST"])
@login_required()
def notice_assets():
    user = g.get("user")
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"podcast", "poster"}:
        return jsonify({"error": "invalid action"}), 400

    filters = payload.get("filters") or {}
    user_id = str(filters.get("user_id") or "").strip()
    tag = str(filters.get("tag") or "").strip()
    day = str(filters.get("day") or "").strip()
    order = str(filters.get("order") or "asc").strip().lower()

    query = _record_query_for(user, include_comments=False, public_only=False)
    try:
        query = _apply_filter_values(query, user_id=user_id, tag=tag, day=day)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if order == "desc":
        query = query.order_by(Record.created_at.desc(), Record.id.desc())
    else:
        query = query.order_by(Record.created_at.asc(), Record.id.asc())

    limit = int(current_app.config.get("AI_MAX_NOTICE_RECORDS") or 180)
    records = query.limit(max(20, min(limit, 500))).all()
    records_text = _records_for_ai_prompt(records)
    if not records_text:
        return jsonify({"error": "no records for current filters"}), 400

    try:
        if action == "podcast":
            script_messages = _build_notice_ai_prompt("podcast", records_text)
            script, script_settings = _ai_chat(messages=script_messages, temperature=0.4, max_tokens=1400)
            audio_bytes, audio_type, tts_info = _ai_tts_audio(script)
            asset = _save_generated_asset(
                user=user,
                kind="podcast_audio",
                title=f"Notice Podcast {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                provider=script_settings["provider"],
                model=f"{script_settings['model']} + {tts_info['model']}",
                content_type=audio_type,
                ext=".mp3",
                data=audio_bytes,
                filters=filters,
            )
            return jsonify(
                {
                    "action": action,
                    "record_count": len(records),
                    "asset": _generated_asset_payload(asset),
                    "transcript": script,
                }
            )

        poster_prompt_messages = [
            {
                "role": "system",
                "content": "你是视觉总监，请把学习记录提炼成适合图像模型的一段海报提示词。",
            },
            {
                "role": "user",
                "content": (
                    "请输出一段 200-450 字中文提示词，用于生成学习小组海报。"
                    "包含主题、排版、颜色、风格、元素。只输出提示词本身。\n\n"
                    f"记录输入：\n{records_text}"
                ),
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
            title=f"Notice Poster {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            provider=image_info["provider"],
            model=f"{prompt_settings['model']} + {image_info['model']}",
            content_type=image_type,
            ext=ext,
            data=image_bytes,
            filters=filters,
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


@api_bp.route("/api/generated-assets/<int:asset_id>/blob", methods=["GET"])
@login_required()
def generated_asset_blob(asset_id: int):
    user = g.get("user")
    asset = GeneratedAsset.query.get_or_404(asset_id)
    if asset.user_id != user.id:
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
        }
    )
