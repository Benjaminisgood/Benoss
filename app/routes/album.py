import json
from pathlib import Path
from typing import Iterable, List, Optional

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db
from ..oss import get_object_json, get_object_text, list_objects, list_objects_with_meta, public_url
from ..services.album_service import (
    AUDIO_EXTS,
    DOC_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    list_everyday_attachments,
    media_type_for_path,
    upsert_everyday_attachment,
)
from ..utils.markdown import find_attachment_refs
from ..utils.oss_paths import blog_prefix, everyday_prefix, note_prefix, resolve_attachment_key
from ..utils.session_auth import login_required


album_bp = Blueprint("album", __name__)

ATTACHMENT_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS | DOC_EXTS


def _build_source_link(module: str, source_id: str) -> str:
    if module == "blog":
        return f"/blog?key={source_id}"
    if module == "note":
        return f"/note?key={source_id}"
    if module == "everyday":
        return f"/dailyreel/view?date={source_id}"
    return ""


def _preview_url(key: str, media_type: str) -> Optional[str]:
    if media_type == "image":
        return public_url(key, params={"x-oss-process": "image/resize,w_480/quality,q_70"})
    if media_type == "video":
        return public_url(key, params={"x-oss-process": "video/snapshot,t_1000,f_jpg,w_480"})
    return None


def _build_item(module: str, key: str, media_type: str) -> dict:
    return {
        "uuid": Path(key).stem,
        "media_type": media_type,
        "oss_key": key,
        "url": public_url(key),
        "preview_url": _preview_url(key, media_type),
        "source_module": module,
        "source_id": None,
        "source_link": None,
    }


def _iter_module_attachments(module: str, prefix: str, media_type: Optional[str]) -> Iterable[dict]:
    for key in list_objects(prefix):
        ext = Path(key).suffix.lower()
        if ext not in ATTACHMENT_EXTS:
            continue
        item_media_type = media_type_for_path(key)
        if media_type and item_media_type != media_type:
            continue
        yield _build_item(module, key, item_media_type)


def _append_items(items: List[dict], source: Iterable[dict], offset: int, limit: int) -> int:
    for item in source:
        if offset > 0:
            offset -= 1
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return offset


def _list_everyday(offset: int, limit: int, media_type: Optional[str]) -> List[dict]:
    records = list_everyday_attachments(
        media_type=media_type,
        limit=limit,
        offset=offset,
    )
    for item in records:
        item["preview_url"] = _preview_url(item["oss_key"], item["media_type"])
        item["source_link"] = _build_source_link(item["source_module"], item["source_id"])
    return records


def _iter_everyday(media_type: Optional[str]) -> Iterable[dict]:
    records = list_everyday_attachments(media_type=media_type, limit=10000, offset=0)
    for item in records:
        item["preview_url"] = _preview_url(item["oss_key"], item["media_type"])
        item["source_link"] = _build_source_link(item["source_module"], item["source_id"])
        yield item


@album_bp.route("/api/album", methods=["GET"])
@login_required()
def list_album():
    module = (request.args.get("module") or "").strip()
    media_type = (request.args.get("media_type") or "").strip() or None
    limit = int(request.args.get("limit", 200))
    offset = int(request.args.get("offset", 0))
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    if module == "everyday":
        return jsonify({"items": _list_everyday(offset, limit, media_type)})

    if module == "blog":
        items = []
        _append_items(items, _iter_module_attachments("blog", blog_prefix(), media_type), offset, limit)
        return jsonify({"items": items})

    if module == "note":
        items = []
        _append_items(items, _iter_module_attachments("note", note_prefix(), media_type), offset, limit)
        return jsonify({"items": items})

    items: List[dict] = []
    remaining = offset
    remaining = _append_items(items, _iter_everyday(media_type), remaining, limit)
    if len(items) < limit:
        remaining = _append_items(items, _iter_module_attachments("blog", blog_prefix(), media_type), remaining, limit)
    if len(items) < limit:
        _append_items(items, _iter_module_attachments("note", note_prefix(), media_type), remaining, limit)
    return jsonify({"items": items})


@album_bp.route("/api/album/source", methods=["GET"])
@login_required()
def find_album_source():
    module = (request.args.get("module") or "").strip()
    uuid = (request.args.get("uuid") or "").strip()
    if module not in {"blog", "note"}:
        return jsonify({"error": "invalid module"}), 400
    if not uuid:
        return jsonify({"error": "missing uuid"}), 400

    prefix = blog_prefix() if module == "blog" else note_prefix()
    for key in list_objects(prefix, suffix=".md"):
        rel = key[len(prefix) + 1 :]
        content = get_object_text(key)
        for ref in find_attachment_refs(content):
            try:
                att_key = resolve_attachment_key(module, rel, ref)
            except ValueError:
                continue
            if Path(att_key).stem == uuid:
                return jsonify(
                    {
                        "source_id": rel,
                        "source_link": _build_source_link(module, rel),
                    }
                )
    return jsonify({"source_id": None, "source_link": ""})


def _parse_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_reindex_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"version": 1, "objects": {}}
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {"version": 1, "objects": {}}
    if not isinstance(data, dict):
        return {"version": 1, "objects": {}}
    if "objects" not in data or not isinstance(data["objects"], dict):
        data["objects"] = {}
    return data


def _save_reindex_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=True, indent=2)


def _reindex_everyday_month(data: dict) -> int:
    days = data.get("days", {})
    if not isinstance(days, dict):
        return 0
    count = 0
    for date_str, day in days.items():
        if not isinstance(day, dict):
            continue
        attachments = day.get("attachments", [])
        if not isinstance(attachments, list):
            continue
        for item in attachments:
            if not isinstance(item, dict):
                continue
            uuid = str(item.get("uuid", "")).strip()
            oss_key = str(item.get("oss_key", "")).strip()
            if not uuid or not oss_key:
                continue
            media_type = str(item.get("media_type", "")).strip() or media_type_for_path(oss_key)
            upsert_everyday_attachment(uuid, media_type, oss_key, str(date_str), commit=False)
            count += 1
    if count:
        db.session.commit()
    return count


def reindex_all(force: bool = False) -> dict:
    state_path = Path(current_app.config["EVERYDAY_REINDEX_STATE_FILE"])
    state = _load_reindex_state(state_path)
    objects = state.setdefault("objects", {})
    stats = {
        "months_seen": 0,
        "months_indexed": 0,
        "months_skipped": 0,
        "attachments_upserted": 0,
        "errors": [],
    }

    for obj in list_objects_with_meta(everyday_prefix(), suffix="index.json"):
        stats["months_seen"] += 1
        key = obj["key"]
        last_modified = int(obj.get("last_modified") or 0)
        last_seen = objects.get(key)
        try:
            last_seen_value = int(last_seen) if last_seen is not None else 0
        except (TypeError, ValueError):
            last_seen_value = 0
        if not force and last_seen_value and last_modified and last_modified <= last_seen_value:
            stats["months_skipped"] += 1
            continue

        try:
            data = get_object_json(key)
        except Exception as exc:
            stats["errors"].append(f"{key}: {exc}")
            continue

        if not isinstance(data, dict):
            stats["errors"].append(f"{key}: invalid json")
            continue

        stats["attachments_upserted"] += _reindex_everyday_month(data)
        stats["months_indexed"] += 1
        if last_modified:
            objects[key] = last_modified

    _save_reindex_state(state_path, state)
    return stats


@album_bp.route("/api/album/reindex", methods=["POST"])
@login_required(role="admin")
def reindex_album():
    payload = request.get_json(silent=True) or {}
    force = _parse_truthy(request.args.get("force"))
    force = force or _parse_truthy(payload.get("force"))
    result = reindex_all(force=force)
    return jsonify(result)
