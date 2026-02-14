import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import WhiteboardAttachment, WhiteboardCard, WhiteboardEvent, WhiteboardLink
from ..oss import copy_object, delete_object, head_object, public_url, put_object_bytes, put_object_from_file, sign_put_url
from ..utils.ids import new_uuid
from ..utils.oss_paths import whiteboard_object_key, whiteboard_prefix
from ..utils.session_auth import login_required


whiteboard_bp = Blueprint("whiteboard", __name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_DOC_EXTS = {".pdf"}
_HEX_DIGITS = set("0123456789abcdef")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,96}$")
_TAG_SPLIT_RE = re.compile(r"[,\n，;；]+")


def _validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("invalid date") from exc
    return date_str


def _parse_sync_cursor(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _cursor_payload(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _board_cursor(date_str: str) -> str:
    card_max = (
        db.session.query(func.max(WhiteboardCard.updated_at))
        .filter(WhiteboardCard.board_date == date_str)
        .scalar()
    )
    event_max = (
        db.session.query(func.max(WhiteboardEvent.created_at))
        .filter(WhiteboardEvent.board_date == date_str)
        .scalar()
    )
    values = [v for v in [card_max, event_max] if isinstance(v, datetime)]
    return _cursor_payload(max(values) if values else None)


def _normalize_idempotency_key(value: str | None) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if not _IDEMPOTENCY_KEY_RE.match(key):
        raise ValueError("invalid idempotency_key")
    return key


def _extract_entry(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    entry = payload.get("entry")
    if isinstance(entry, dict):
        return entry
    return {}


def _normalize_tags(value) -> list[str]:
    parts: list[str] = []
    if isinstance(value, (list, tuple, set)):
        parts = [str(v or "") for v in value]
    elif isinstance(value, str):
        parts = _TAG_SPLIT_RE.split(value)
    elif value is not None:
        parts = [str(value)]

    out: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        item = str(raw or "").strip()
        if item.startswith("#"):
            item = item[1:]
        item = re.sub(r"\s+", " ", item).strip()
        if not item:
            continue
        if len(item) > 24:
            item = item[:24]
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 12:
            break
    return out


def _parse_entry_meta(payload: dict, board_date: str, default_type: str = "note") -> dict:
    entry = _extract_entry(payload)
    raw_entry_date = entry.get("date", payload.get("entry_date", ""))
    entry_date = str(raw_entry_date or "").strip() or board_date
    entry_date = _validate_date(entry_date)

    raw_tags = entry.get("tags", payload.get("entry_tags", payload.get("tags")))
    tags = _normalize_tags(raw_tags)

    raw_mood = entry.get("mood", payload.get("entry_mood", payload.get("mood")))
    mood = str(raw_mood or "").strip()
    if len(mood) > 24:
        mood = mood[:24]

    raw_type = entry.get("type", payload.get("entry_type", payload.get("type")))
    entry_type = str(raw_type or "").strip().lower() or default_type
    if len(entry_type) > 24:
        entry_type = entry_type[:24]

    return {
        "entry_date": entry_date,
        "entry_tags_json": json.dumps(tags, ensure_ascii=False),
        "entry_mood": mood,
        "entry_type": entry_type,
    }


def _parse_entry_meta_updates(payload: dict, board_date: str) -> dict:
    entry = _extract_entry(payload)
    out = {}

    if "entry_date" in payload or "date" in entry:
        entry_date = str(entry.get("date", payload.get("entry_date", "")) or "").strip() or board_date
        out["entry_date"] = _validate_date(entry_date)

    if "entry_tags" in payload or "tags" in payload or "tags" in entry:
        tags = _normalize_tags(entry.get("tags", payload.get("entry_tags", payload.get("tags"))))
        out["entry_tags_json"] = json.dumps(tags, ensure_ascii=False)

    if "entry_mood" in payload or "mood" in payload or "mood" in entry:
        mood = str(entry.get("mood", payload.get("entry_mood", payload.get("mood"))) or "").strip()
        if len(mood) > 24:
            mood = mood[:24]
        out["entry_mood"] = mood

    if "entry_type" in payload or "type" in payload or "type" in entry:
        entry_type = str(entry.get("type", payload.get("entry_type", payload.get("type"))) or "").strip().lower()
        if len(entry_type) > 24:
            entry_type = entry_type[:24]
        out["entry_type"] = entry_type or "note"

    return out


def _entry_tags(card: WhiteboardCard) -> list[str]:
    raw = card.entry_tags_json or "[]"
    try:
        data = json.loads(raw)
    except Exception:
        data = []
    if not isinstance(data, list):
        return []
    return _normalize_tags(data)


def _entry_payload(card: WhiteboardCard) -> dict:
    return {
        "date": (card.entry_date or card.board_date or "").strip(),
        "tags": _entry_tags(card),
        "mood": (card.entry_mood or "").strip(),
        "type": (card.entry_type or "note").strip() or "note",
    }


def _find_idempotent_card(date_str: str, user_id: int | None, key: str) -> WhiteboardCard | None:
    if not key or not user_id:
        return None
    return (
        WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
        .filter(
            WhiteboardCard.board_date == date_str,
            WhiteboardCard.created_by_id == int(user_id),
            WhiteboardCard.idempotency_key == key,
        )
        .order_by(WhiteboardCard.id.asc())
        .first()
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_valid_sha256(value: str) -> bool:
    value = (value or "").strip().lower()
    return len(value) == 64 and all(ch in _HEX_DIGITS for ch in value)


def _media_type_for(filename: str, content_type: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in _IMAGE_EXTS or (content_type or "").startswith("image/"):
        return "image"
    if ext in _VIDEO_EXTS or (content_type or "").startswith("video/"):
        return "video"
    if ext in _AUDIO_EXTS or (content_type or "").startswith("audio/"):
        return "audio"
    if ext in _DOC_EXTS or (content_type or "").lower() == "application/pdf":
        return "pdf"
    return "file"


def _preview_params(media_type: str) -> dict | None:
    if media_type == "image":
        return {"x-oss-process": "image/resize,w_720/quality,q_75"}
    if media_type == "video":
        return {"x-oss-process": "video/snapshot,t_1000,f_jpg,w_720"}
    if media_type == "pdf":
        return {"x-oss-process": "doc/preview,format=jpg,page=1"}
    return None


def _attachment_payload(att: WhiteboardAttachment) -> dict:
    media_type = att.media_type or _media_type_for(att.filename or "", att.content_type or "")
    params = _preview_params(media_type)
    return {
        "id": att.id,
        "filename": att.filename or "",
        "media_type": media_type,
        "content_type": att.content_type or "",
        "size_bytes": int(att.size_bytes or 0),
        "sha256": att.sha256 or "",
        "oss_key": att.oss_key,
        "url": public_url(att.oss_key, expires=3600),
        "preview_url": public_url(att.oss_key, expires=3600, params=params) if params else "",
        "created_at": att.created_at.isoformat() + "Z" if att.created_at else None,
        "updated_at": att.updated_at.isoformat() + "Z" if att.updated_at else None,
    }


def _card_payload(card: WhiteboardCard) -> dict:
    entry = _entry_payload(card)
    text = card.text or ""
    attachments = [_attachment_payload(a) for a in (card.attachments or [])]
    return {
        "id": card.id,
        "date": card.board_date,
        "x": float(card.x or 0),
        "y": float(card.y or 0),
        "text": text,
        "version": int(card.version or 1),
        "idempotency_key": card.idempotency_key or "",
        "entry": entry,
        "entry_date": entry["date"],
        "entry_tags": entry["tags"],
        "entry_mood": entry["mood"],
        "entry_type": entry["type"],
        "is_media_only": bool(attachments and not text.strip()),
        "attachments": attachments,
        "created_at": card.created_at.isoformat() + "Z" if card.created_at else None,
        "updated_at": card.updated_at.isoformat() + "Z" if card.updated_at else None,
    }


def _attachment_export_payload(att: WhiteboardAttachment) -> dict:
    media_type = att.media_type or _media_type_for(att.filename or "", att.content_type or "")
    return {
        "filename": att.filename or "",
        "media_type": media_type,
        "content_type": att.content_type or "",
        "size_bytes": int(att.size_bytes or 0),
        "sha256": att.sha256 or "",
        "oss_key": att.oss_key,
    }


def _card_export_payload(card: WhiteboardCard) -> dict:
    return {
        "id": card.id,
        "x": float(card.x or 0),
        "y": float(card.y or 0),
        "text": card.text or "",
        "entry": _entry_payload(card),
        "attachments": [_attachment_export_payload(a) for a in (card.attachments or [])],
        "updated_at": card.updated_at.isoformat() + "Z" if card.updated_at else None,
    }


def _link_payload(link: WhiteboardLink) -> dict:
    return {
        "id": link.id,
        "date": link.board_date,
        "from_id": int(link.from_card_id),
        "to_id": int(link.to_card_id),
        "created_at": link.created_at.isoformat() + "Z" if link.created_at else None,
        "updated_at": link.updated_at.isoformat() + "Z" if link.updated_at else None,
    }


def _link_export_payload(link: WhiteboardLink) -> dict:
    return {
        "from_id": int(link.from_card_id),
        "to_id": int(link.to_card_id),
    }


def _event_payload(event: WhiteboardEvent) -> dict:
    payload = {}
    try:
        payload = json.loads(event.payload_json or "{}")
    except Exception:
        payload = {}
    return {
        "id": event.id,
        "date": event.board_date,
        "type": event.event_type,
        "card_id": event.card_id,
        "actor_user_id": event.actor_user_id,
        "payload": payload,
        "created_at": event.created_at.isoformat() + "Z" if event.created_at else None,
    }


def _emit_event(date_str: str, event_type: str, card_id: int, actor_user_id: int, payload: dict | None = None) -> None:
    ev = WhiteboardEvent(
        board_date=date_str,
        event_type=event_type,
        card_id=card_id,
        actor_user_id=actor_user_id,
        payload_json=json.dumps(payload or {}, ensure_ascii=True),
    )
    db.session.add(ev)


def _board_snapshot_key(date_str: str) -> str:
    return whiteboard_object_key(date_str, "board.json")


def _build_board_export(date_str: str) -> dict:
    cards = (
        WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
        .filter_by(board_date=date_str)
        .order_by(WhiteboardCard.updated_at.asc(), WhiteboardCard.id.asc())
        .all()
    )
    links = WhiteboardLink.query.filter_by(board_date=date_str).order_by(WhiteboardLink.id.asc()).all()
    return {
        "schema_version": 3,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "date": date_str,
        "cards": [_card_export_payload(card) for card in cards],
        "links": [_link_export_payload(link) for link in links],
    }


def _write_board_snapshot(date_str: str) -> None:
    # Best-effort snapshot for backup/fast preview; DB remains source of truth.
    try:
        payload = _build_board_export(date_str)
        raw = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        put_object_bytes(_board_snapshot_key(date_str), raw, content_type="application/json")
    except Exception:
        current_app.logger.exception("Failed to write whiteboard snapshot for %s", date_str)


_SNAPSHOT_LOCK = threading.Lock()
_SNAPSHOT_STATE: dict[str, dict] = {}


def _schedule_board_snapshot(date_str: str, delay_seconds: float | None = None) -> None:
    # Avoid high-frequency OSS writes while typing: debounce snapshot writes per date.
    app = current_app._get_current_object()
    delay = delay_seconds
    if delay is None:
        try:
            delay = float(app.config.get("WHITEBOARD_SNAPSHOT_DEBOUNCE_SECONDS", 6.0))
        except Exception:
            delay = 6.0
    delay = max(0.0, delay)

    def _worker(token: int) -> None:
        try:
            with app.app_context():
                _write_board_snapshot(date_str)
        finally:
            with _SNAPSHOT_LOCK:
                st = _SNAPSHOT_STATE.get(date_str)
                if st and int(st.get("token") or 0) == token:
                    _SNAPSHOT_STATE.pop(date_str, None)

    with _SNAPSHOT_LOCK:
        st = _SNAPSHOT_STATE.get(date_str) or {}
        token = int(st.get("token") or 0) + 1
        old_timer = st.get("timer")
        if old_timer:
            try:
                old_timer.cancel()
            except Exception:
                pass
        timer = threading.Timer(delay, _worker, args=(token,))
        timer.daemon = True
        _SNAPSHOT_STATE[date_str] = {"timer": timer, "token": token}
        timer.start()


@whiteboard_bp.route("/api/whiteboard/board", methods=["GET"])
@login_required()
def get_board():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    cards = (
        WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
        .filter_by(board_date=date_str)
        .order_by(WhiteboardCard.updated_at.asc(), WhiteboardCard.id.asc())
        .all()
    )
    links = WhiteboardLink.query.filter_by(board_date=date_str).order_by(WhiteboardLink.id.asc()).all()
    last_event_id = (
        db.session.query(func.max(WhiteboardEvent.id)).filter(WhiteboardEvent.board_date == date_str).scalar()
    ) or 0
    return jsonify(
        {
            "date": date_str,
            "cards": [_card_payload(card) for card in cards],
            "links": [_link_payload(link) for link in links],
            "last_event_id": int(last_event_id),
            "sync_cursor": _board_cursor(date_str),
        }
    )


@whiteboard_bp.route("/api/whiteboard/events", methods=["GET"])
@login_required()
def get_events():
    date_str = (request.args.get("date") or "").strip()
    after_id = int(request.args.get("after_id", 0))
    after_id = max(0, after_id)
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    events = (
        WhiteboardEvent.query.filter(WhiteboardEvent.board_date == date_str, WhiteboardEvent.id > after_id)
        .order_by(WhiteboardEvent.id.asc())
        .limit(200)
        .all()
    )
    return jsonify({"date": date_str, "events": [_event_payload(ev) for ev in events]})


@whiteboard_bp.route("/api/whiteboard/changes", methods=["GET"])
@login_required()
def get_changes():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    try:
        cursor_dt = _parse_sync_cursor(request.args.get("cursor"))
    except ValueError:
        return jsonify({"error": "invalid cursor"}), 400

    # First sync or invalid caller state -> return full board snapshot once.
    if cursor_dt is None:
        cards = (
            WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
            .filter_by(board_date=date_str)
            .order_by(WhiteboardCard.updated_at.asc(), WhiteboardCard.id.asc())
            .all()
        )
        links = WhiteboardLink.query.filter_by(board_date=date_str).order_by(WhiteboardLink.id.asc()).all()
        return jsonify(
            {
                "date": date_str,
                "reset": True,
                "cards": [_card_payload(card) for card in cards],
                "deleted_card_ids": [],
                "links_changed": True,
                "links": [_link_payload(link) for link in links],
                "next_cursor": _board_cursor(date_str),
            }
        )

    changed_cards = (
        WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
        .filter(
            WhiteboardCard.board_date == date_str,
            WhiteboardCard.updated_at > cursor_dt,
        )
        .order_by(WhiteboardCard.updated_at.asc(), WhiteboardCard.id.asc())
        .limit(400)
        .all()
    )

    events = (
        WhiteboardEvent.query.filter(
            WhiteboardEvent.board_date == date_str,
            WhiteboardEvent.created_at > cursor_dt,
        )
        .order_by(WhiteboardEvent.id.asc())
        .limit(1200)
        .all()
    )

    deleted_card_ids: set[int] = set()
    links_changed = False
    reset = False
    for ev in events:
        event_type = (ev.event_type or "").strip().lower()
        if event_type == "delete" and int(ev.card_id or 0) > 0:
            deleted_card_ids.add(int(ev.card_id))
        if event_type in {"link_create", "link_delete"}:
            links_changed = True
        if event_type == "reset":
            reset = True
            links_changed = True

    links_payload = []
    if links_changed:
        links = WhiteboardLink.query.filter_by(board_date=date_str).order_by(WhiteboardLink.id.asc()).all()
        links_payload = [_link_payload(link) for link in links]

    if reset:
        all_cards = (
            WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
            .filter_by(board_date=date_str)
            .order_by(WhiteboardCard.updated_at.asc(), WhiteboardCard.id.asc())
            .all()
        )
        return jsonify(
            {
                "date": date_str,
                "reset": True,
                "cards": [_card_payload(card) for card in all_cards],
                "deleted_card_ids": [],
                "links_changed": True,
                "links": links_payload,
                "next_cursor": _board_cursor(date_str),
            }
        )

    return jsonify(
        {
            "date": date_str,
            "reset": False,
            "cards": [_card_payload(card) for card in changed_cards],
            "deleted_card_ids": sorted(deleted_card_ids),
            "links_changed": links_changed,
            "links": links_payload,
            "next_cursor": _board_cursor(date_str),
        }
    )


@whiteboard_bp.route("/api/whiteboard/cards", methods=["POST"])
@login_required()
def create_card():
    user = g.get("user")
    payload = request.get_json(silent=True) or {}
    date_str = str(payload.get("date", "")).strip() or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    text = str(payload.get("text", ""))
    if len(text) > 5000:
        return jsonify({"error": "text too long"}), 400
    try:
        x = float(payload.get("x", 20))
        y = float(payload.get("y", 20))
    except Exception:
        return jsonify({"error": "invalid position"}), 400

    try:
        idempotency_key = _normalize_idempotency_key(payload.get("idempotency_key"))
    except ValueError:
        return jsonify({"error": "invalid idempotency_key"}), 400

    existing = _find_idempotent_card(date_str, user.id if user else None, idempotency_key)
    if existing:
        return jsonify({"card": _card_payload(existing), "idempotent_replay": True})

    try:
        meta = _parse_entry_meta(payload, date_str, default_type="note")
    except ValueError:
        return jsonify({"error": "invalid entry_date"}), 400

    card = WhiteboardCard(
        board_date=date_str,
        text=text,
        x=x,
        y=y,
        version=1,
        idempotency_key=idempotency_key or None,
        entry_date=meta["entry_date"],
        entry_tags_json=meta["entry_tags_json"],
        entry_mood=meta["entry_mood"],
        entry_type=meta["entry_type"],
        created_by_id=user.id if user else None,
    )
    db.session.add(card)
    db.session.flush()
    _emit_event(date_str, "create", card.id, user.id, {"card": _card_payload(card)})
    db.session.commit()
    _write_board_snapshot(date_str)
    return jsonify({"card": _card_payload(card)})


@whiteboard_bp.route("/api/whiteboard/cards/<int:card_id>", methods=["GET"])
@login_required()
def get_card(card_id: int):
    card = WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments)).get_or_404(card_id)
    return jsonify({"card": _card_payload(card)})


@whiteboard_bp.route("/api/whiteboard/cards/upload-direct/init", methods=["POST"])
@login_required()
def init_direct_upload_media_card():
    payload = request.get_json(silent=True) or {}
    date_str = str(payload.get("date", "")).strip() or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    content_type = str(payload.get("content_type") or "").strip() or "application/octet-stream"
    if len(content_type) > 255:
        return jsonify({"error": "invalid content_type"}), 400

    filename = str(payload.get("filename") or "").replace("\\", "/").split("/")[-1].strip() or "file"
    ext = Path(filename).suffix.lower()
    if not ext:
        cleaned = secure_filename(filename)
        ext = Path(cleaned).suffix.lower()

    file_uuid = new_uuid()
    oss_key = whiteboard_object_key(date_str, f"objects/{file_uuid}{ext}")
    expires = int(current_app.config.get("OSS_DIRECT_UPLOAD_EXPIRES", 15 * 60))
    upload_url, headers = sign_put_url(oss_key, expires=expires, content_type=content_type)
    max_bytes = int(current_app.config.get("WHITEBOARD_MAX_MEDIA_BYTES", 100 * 1024 * 1024))
    return jsonify(
        {
            "date": date_str,
            "oss_key": oss_key,
            "upload_url": upload_url,
            "upload_headers": headers,
            "expires_in": expires,
            "max_bytes": max_bytes,
        }
    )


@whiteboard_bp.route("/api/whiteboard/cards/upload-direct/complete", methods=["POST"])
@login_required()
def complete_direct_upload_media_card():
    user = g.get("user")
    payload = request.get_json(silent=True) or {}

    date_str = str(payload.get("date", "")).strip() or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    text = str(payload.get("text", ""))
    if len(text) > 5000:
        return jsonify({"error": "text too long"}), 400
    try:
        x = float(payload.get("x", 20))
        y = float(payload.get("y", 20))
    except Exception:
        return jsonify({"error": "invalid position"}), 400

    try:
        idempotency_key = _normalize_idempotency_key(payload.get("idempotency_key"))
    except ValueError:
        return jsonify({"error": "invalid idempotency_key"}), 400
    existing = _find_idempotent_card(date_str, user.id if user else None, idempotency_key)
    if existing:
        return jsonify({"card": _card_payload(existing), "idempotent_replay": True})

    filename = str(payload.get("filename") or "").replace("\\", "/").split("/")[-1].strip() or "file"
    oss_key = str(payload.get("oss_key") or "").strip()
    if not oss_key:
        return jsonify({"error": "missing oss_key"}), 400
    allowed_prefix = whiteboard_object_key(date_str, "objects") + "/"
    if not oss_key.startswith(allowed_prefix):
        return jsonify({"error": "invalid oss_key"}), 400

    sha256 = str(payload.get("sha256") or "").strip().lower()
    if sha256 and not _is_valid_sha256(sha256):
        return jsonify({"error": "invalid sha256"}), 400

    try:
        meta = head_object(oss_key)
    except Exception:
        return jsonify({"error": "missing object"}), 400

    size_bytes = int(meta.content_length or 0)
    content_type = (meta.content_type or str(payload.get("content_type") or "")).strip()
    max_bytes = int(current_app.config.get("WHITEBOARD_MAX_MEDIA_BYTES", 100 * 1024 * 1024))
    if max_bytes > 0 and size_bytes > max_bytes:
        try:
            delete_object(oss_key)
        except Exception:
            current_app.logger.exception("Failed to delete oversized direct-upload whiteboard object %s", oss_key)
        return jsonify({"error": "file too large"}), 413

    media_type = _media_type_for(filename, content_type)
    default_type = "media" if not text.strip() else "note"
    try:
        meta = _parse_entry_meta(payload, date_str, default_type=default_type)
    except ValueError:
        return jsonify({"error": "invalid entry_date"}), 400

    card = WhiteboardCard(
        board_date=date_str,
        text=text,
        x=x,
        y=y,
        version=1,
        idempotency_key=idempotency_key or None,
        entry_date=meta["entry_date"],
        entry_tags_json=meta["entry_tags_json"],
        entry_mood=meta["entry_mood"],
        entry_type=meta["entry_type"],
        created_by_id=user.id if user else None,
    )
    att = WhiteboardAttachment(
        card=card,
        oss_key=oss_key,
        filename=filename,
        content_type=content_type,
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    db.session.add(card)
    db.session.add(att)
    db.session.flush()
    _emit_event(date_str, "create", card.id, user.id, {"card": _card_payload(card)})
    db.session.commit()
    _write_board_snapshot(date_str)
    return jsonify({"card": _card_payload(card)})


@whiteboard_bp.route("/api/whiteboard/cards/upload", methods=["POST"])
@login_required()
def upload_media_card():
    user = g.get("user")
    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400
    file_obj = request.files["file"]
    if not file_obj or not file_obj.filename:
        return jsonify({"error": "missing filename"}), 400

    date_str = (request.form.get("date") or "").strip() or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    text = str(request.form.get("text", ""))
    if len(text) > 5000:
        return jsonify({"error": "text too long"}), 400
    try:
        x = float(request.form.get("x", 20))
        y = float(request.form.get("y", 20))
    except Exception:
        return jsonify({"error": "invalid position"}), 400

    try:
        idempotency_key = _normalize_idempotency_key(request.form.get("idempotency_key"))
    except ValueError:
        return jsonify({"error": "invalid idempotency_key"}), 400
    existing = _find_idempotent_card(date_str, user.id if user else None, idempotency_key)
    if existing:
        return jsonify({"card": _card_payload(existing), "idempotent_replay": True})

    max_bytes = int(current_app.config.get("WHITEBOARD_MAX_MEDIA_BYTES", 100 * 1024 * 1024))

    filename = (file_obj.filename or "").replace("\\", "/").split("/")[-1].strip() or "file"
    ext = Path(filename).suffix.lower()
    if not ext:
        cleaned = secure_filename(filename)
        ext = Path(cleaned).suffix.lower()

    file_uuid = new_uuid()
    oss_key = whiteboard_object_key(date_str, f"objects/{file_uuid}{ext}")

    tmp_dir = Path(current_app.config["UPLOAD_TMP_DIR"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{file_uuid}{ext}"
    file_obj.save(tmp_path)

    size_bytes = 0
    sha256 = ""
    try:
        size_bytes = int(tmp_path.stat().st_size)
    except Exception:
        size_bytes = 0
    if max_bytes > 0 and size_bytes > max_bytes:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"error": "file too large"}), 413
    try:
        sha256 = _sha256_file(tmp_path)
    except Exception:
        sha256 = ""

    try:
        put_object_from_file(oss_key, str(tmp_path), content_type=file_obj.mimetype or None)
    finally:
        tmp_path.unlink(missing_ok=True)

    media_type = _media_type_for(filename, file_obj.mimetype or "")

    default_type = "media" if not text.strip() else "note"
    payload_for_meta = {
        "entry_date": request.form.get("entry_date"),
        "entry_tags": request.form.get("entry_tags"),
        "entry_mood": request.form.get("entry_mood"),
        "entry_type": request.form.get("entry_type"),
    }
    try:
        meta = _parse_entry_meta(payload_for_meta, date_str, default_type=default_type)
    except ValueError:
        return jsonify({"error": "invalid entry_date"}), 400

    card = WhiteboardCard(
        board_date=date_str,
        text=text,
        x=x,
        y=y,
        version=1,
        idempotency_key=idempotency_key or None,
        entry_date=meta["entry_date"],
        entry_tags_json=meta["entry_tags_json"],
        entry_mood=meta["entry_mood"],
        entry_type=meta["entry_type"],
        created_by_id=user.id if user else None,
    )
    att = WhiteboardAttachment(
        card=card,
        oss_key=oss_key,
        filename=filename,
        content_type=file_obj.mimetype or "",
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    db.session.add(card)
    db.session.add(att)
    db.session.flush()
    _emit_event(date_str, "create", card.id, user.id, {"card": _card_payload(card)})
    db.session.commit()
    _write_board_snapshot(date_str)
    return jsonify({"card": _card_payload(card)})


@whiteboard_bp.route("/api/whiteboard/cards/<int:card_id>", methods=["PATCH"])
@login_required()
def update_card(card_id: int):
    user = g.get("user")
    card = WhiteboardCard.query.get_or_404(card_id)
    payload = request.get_json(silent=True) or {}
    if "expected_version" in payload:
        try:
            expected_version = int(payload.get("expected_version") or 0)
        except Exception:
            return jsonify({"error": "invalid expected_version"}), 400
        current_version = int(card.version or 1)
        if expected_version != current_version:
            return jsonify({"error": "version conflict", "card": _card_payload(card)}), 409

    changed = {}
    if "text" in payload:
        text = str(payload.get("text", ""))
        if len(text) > 5000:
            return jsonify({"error": "text too long"}), 400
        card.text = text
        changed["text"] = text
    if "x" in payload:
        try:
            card.x = float(payload.get("x", card.x or 0))
        except Exception:
            return jsonify({"error": "invalid x"}), 400
        changed["x"] = float(card.x or 0)
    if "y" in payload:
        try:
            card.y = float(payload.get("y", card.y or 0))
        except Exception:
            return jsonify({"error": "invalid y"}), 400
        changed["y"] = float(card.y or 0)
    try:
        meta_updates = _parse_entry_meta_updates(payload, card.board_date)
    except ValueError:
        return jsonify({"error": "invalid entry_date"}), 400
    if "entry_date" in meta_updates:
        card.entry_date = meta_updates["entry_date"]
        changed["entry_date"] = card.entry_date
    if "entry_tags_json" in meta_updates:
        card.entry_tags_json = meta_updates["entry_tags_json"]
        changed["entry_tags"] = _entry_tags(card)
    if "entry_mood" in meta_updates:
        card.entry_mood = meta_updates["entry_mood"]
        changed["entry_mood"] = card.entry_mood
    if "entry_type" in meta_updates:
        card.entry_type = meta_updates["entry_type"]
        changed["entry_type"] = card.entry_type

    if not changed:
        return jsonify({"card": _card_payload(card)})

    card.version = int(card.version or 1) + 1
    changed["version"] = int(card.version)
    db.session.add(card)
    db.session.flush()
    _emit_event(card.board_date, "update", card.id, user.id, {"changes": changed})
    db.session.commit()
    # Text updates can be high-frequency while typing; debounce OSS snapshot writes for that case.
    if set(changed.keys()) == {"text"}:
        _schedule_board_snapshot(card.board_date)
    else:
        _write_board_snapshot(card.board_date)
    return jsonify({"card": _card_payload(card)})


@whiteboard_bp.route("/api/whiteboard/links", methods=["POST"])
@login_required()
def create_link():
    user = g.get("user")
    payload = request.get_json(silent=True) or {}
    date_str = str(payload.get("date", "")).strip() or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    try:
        from_id = int(payload.get("from_id") or 0)
        to_id = int(payload.get("to_id") or 0)
    except Exception:
        return jsonify({"error": "invalid link"}), 400
    if from_id <= 0 or to_id <= 0 or from_id == to_id:
        return jsonify({"error": "invalid link"}), 400

    if not WhiteboardCard.query.filter_by(id=from_id, board_date=date_str).first():
        return jsonify({"error": "invalid from card"}), 400
    if not WhiteboardCard.query.filter_by(id=to_id, board_date=date_str).first():
        return jsonify({"error": "invalid to card"}), 400

    existing = WhiteboardLink.query.filter_by(board_date=date_str, from_card_id=from_id, to_card_id=to_id).first()
    if existing:
        return jsonify({"link": _link_payload(existing), "exists": True})

    link = WhiteboardLink(
        board_date=date_str,
        from_card_id=from_id,
        to_card_id=to_id,
        created_by_id=user.id if user else None,
    )
    db.session.add(link)
    db.session.flush()
    _emit_event(date_str, "link_create", 0, user.id, {"link": _link_payload(link)})
    db.session.commit()
    _write_board_snapshot(date_str)
    return jsonify({"link": _link_payload(link)})


@whiteboard_bp.route("/api/whiteboard/links/<int:link_id>", methods=["DELETE"])
@login_required()
def delete_link(link_id: int):
    user = g.get("user")
    link = WhiteboardLink.query.get_or_404(link_id)
    payload = _link_payload(link)
    date_str = link.board_date
    db.session.delete(link)
    _emit_event(date_str, "link_delete", 0, user.id, {"link": payload})
    db.session.commit()
    _write_board_snapshot(date_str)
    return jsonify({"deleted": True, "id": link_id})


@whiteboard_bp.route("/api/whiteboard/cards/<int:card_id>", methods=["DELETE"])
@login_required()
def delete_card(card_id: int):
    user = g.get("user")
    card = WhiteboardCard.query.get_or_404(card_id)
    date_str = card.board_date
    keys = [a.oss_key for a in (card.attachments or []) if a.oss_key]
    links = (
        WhiteboardLink.query.filter(
            WhiteboardLink.board_date == date_str,
            or_(WhiteboardLink.from_card_id == card_id, WhiteboardLink.to_card_id == card_id),
        )
        .order_by(WhiteboardLink.id.asc())
        .all()
    )
    for link in links:
        db.session.delete(link)
        _emit_event(date_str, "link_delete", 0, user.id, {"link": _link_payload(link)})
    db.session.delete(card)
    _emit_event(date_str, "delete", card_id, user.id, {})
    db.session.commit()
    _write_board_snapshot(date_str)
    for key in keys:
        try:
            delete_object(key)
        except Exception:
            current_app.logger.exception("Failed to delete whiteboard object %s", key)
    return jsonify({"deleted": True, "id": card_id})


@whiteboard_bp.route("/api/whiteboard/export", methods=["GET"])
@login_required()
def export_board():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    return jsonify(_build_board_export(date_str))


@whiteboard_bp.route("/api/whiteboard/snapshot", methods=["GET"])
@login_required()
def get_board_snapshot():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    # Ensure snapshot exists best-effort.
    _write_board_snapshot(date_str)
    key = _board_snapshot_key(date_str)
    return jsonify({"date": date_str, "oss_key": key, "url": public_url(key, expires=3600)})


@whiteboard_bp.route("/api/whiteboard/import", methods=["POST"])
@login_required()
def import_board():
    user = g.get("user")
    payload = request.get_json(silent=True) or {}
    date_str = str(payload.get("date", "")).strip() or datetime.utcnow().strftime("%Y-%m-%d")
    mode = str(payload.get("mode", "merge")).strip().lower() or "merge"
    board = payload.get("board") if isinstance(payload, dict) and "board" in payload else payload

    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    if mode not in {"merge", "replace"}:
        return jsonify({"error": "invalid mode"}), 400
    if not isinstance(board, dict):
        return jsonify({"error": "invalid board"}), 400
    schema_version = int(board.get("schema_version") or 0)
    if schema_version not in {1, 2, 3}:
        return jsonify({"error": "unsupported schema"}), 400

    raw_cards = board.get("cards") or []
    if not isinstance(raw_cards, list):
        return jsonify({"error": "invalid cards"}), 400
    raw_links = board.get("links") or []
    if raw_links and not isinstance(raw_links, list):
        return jsonify({"error": "invalid links"}), 400

    keys_to_delete: list[str] = []
    if mode == "replace":
        WhiteboardLink.query.filter_by(board_date=date_str).delete(synchronize_session=False)
        existing_cards = (
            WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
            .filter_by(board_date=date_str)
            .all()
        )
        for card in existing_cards:
            for att in card.attachments or []:
                if att.oss_key:
                    keys_to_delete.append(att.oss_key)
            db.session.delete(card)

    created_cards = 0
    created_links = 0
    skipped_links = 0
    skipped_attachments = 0
    src_prefix = whiteboard_prefix().rstrip("/") + "/"
    id_map: dict[int, int] = {}

    for raw in raw_cards:
        if not isinstance(raw, dict):
            continue
        try:
            x = float(raw.get("x", 20))
            y = float(raw.get("y", 20))
        except Exception:
            x = 20.0
            y = 20.0
        text = str(raw.get("text", ""))
        if len(text) > 5000:
            text = text[:5000]

        meta_payload = {
            "entry": raw.get("entry") if isinstance(raw.get("entry"), dict) else {},
            "entry_date": raw.get("entry_date"),
            "entry_tags": raw.get("entry_tags"),
            "entry_mood": raw.get("entry_mood"),
            "entry_type": raw.get("entry_type"),
        }
        try:
            meta = _parse_entry_meta(meta_payload, date_str, default_type="note")
        except ValueError:
            meta = _parse_entry_meta({}, date_str, default_type="note")

        card = WhiteboardCard(
            board_date=date_str,
            x=x,
            y=y,
            text=text,
            version=1,
            entry_date=meta["entry_date"],
            entry_tags_json=meta["entry_tags_json"],
            entry_mood=meta["entry_mood"],
            entry_type=meta["entry_type"],
            created_by_id=user.id if user else None,
        )
        db.session.add(card)
        db.session.flush()
        created_cards += 1
        try:
            raw_id = int(raw.get("id") or 0)
        except Exception:
            raw_id = 0
        if raw_id > 0:
            id_map[raw_id] = int(card.id)

        raw_atts = raw.get("attachments") or []
        if not isinstance(raw_atts, list):
            continue
        for raw_att in raw_atts:
            if not isinstance(raw_att, dict):
                continue
            source_key = str(raw_att.get("oss_key", "")).strip().lstrip("/")
            if not source_key:
                continue
            # Prevent copying arbitrary OSS keys into the public whiteboard.
            if not source_key.startswith(src_prefix):
                skipped_attachments += 1
                continue
            ext = Path(source_key).suffix.lower()
            file_uuid = new_uuid()
            target_key = whiteboard_object_key(date_str, f"objects/{file_uuid}{ext}")
            try:
                copy_object(source_key, target_key)
            except Exception:
                skipped_attachments += 1
                current_app.logger.exception("Failed to copy whiteboard object %s", source_key)
                continue

            filename = str(raw_att.get("filename", "")).strip()[:255]
            content_type = str(raw_att.get("content_type", "")).strip()[:255]
            media_type = str(raw_att.get("media_type", "")).strip().lower() or _media_type_for(filename, content_type)
            try:
                size_bytes = int(raw_att.get("size_bytes") or 0)
            except Exception:
                size_bytes = 0
            sha256 = str(raw_att.get("sha256", "")).strip()[:64]

            db.session.add(
                WhiteboardAttachment(
                    card=card,
                    oss_key=target_key,
                    filename=filename,
                    content_type=content_type,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    sha256=sha256,
                )
            )

    existing_pairs: set[tuple[int, int]] = set()
    if mode == "merge":
        for from_id, to_id in (
            db.session.query(WhiteboardLink.from_card_id, WhiteboardLink.to_card_id)
            .filter(WhiteboardLink.board_date == date_str)
            .all()
        ):
            existing_pairs.add((int(from_id), int(to_id)))

    for raw_link in raw_links:
        if not isinstance(raw_link, dict):
            continue
        try:
            from_old = int(raw_link.get("from_id") or raw_link.get("from") or 0)
            to_old = int(raw_link.get("to_id") or raw_link.get("to") or 0)
        except Exception:
            skipped_links += 1
            continue
        if from_old <= 0 or to_old <= 0:
            skipped_links += 1
            continue
        from_new = id_map.get(from_old)
        to_new = id_map.get(to_old)
        if not from_new or not to_new:
            skipped_links += 1
            continue
        if from_new == to_new:
            skipped_links += 1
            continue
        pair = (int(from_new), int(to_new))
        if pair in existing_pairs:
            skipped_links += 1
            continue
        existing_pairs.add(pair)
        db.session.add(
            WhiteboardLink(
                board_date=date_str,
                from_card_id=from_new,
                to_card_id=to_new,
                created_by_id=user.id if user else None,
            )
        )
        created_links += 1

    _emit_event(date_str, "reset", 0, user.id, {"mode": mode, "cards": created_cards, "links": created_links})
    db.session.commit()
    _write_board_snapshot(date_str)

    deleted_failed = 0
    if mode == "replace" and keys_to_delete:
        for key in keys_to_delete:
            try:
                delete_object(key)
            except Exception:
                deleted_failed += 1
                current_app.logger.exception("Failed to delete replaced whiteboard object %s", key)

    return jsonify(
        {
            "imported": True,
            "date": date_str,
            "mode": mode,
            "created_cards": created_cards,
            "created_links": created_links,
            "skipped_links": skipped_links,
            "skipped_attachments": skipped_attachments,
            "deleted_failed": deleted_failed,
        }
    )
