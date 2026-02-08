import hashlib
import json
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import WhiteboardAttachment, WhiteboardCard, WhiteboardEvent
from ..oss import copy_object, delete_object, public_url, put_object_from_file
from ..utils.ids import new_uuid
from ..utils.oss_paths import whiteboard_object_key, whiteboard_prefix
from ..utils.session_auth import login_required


whiteboard_bp = Blueprint("whiteboard", __name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_DOC_EXTS = {".pdf"}


def _validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("invalid date") from exc
    return date_str


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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
    return {
        "id": card.id,
        "date": card.board_date,
        "x": float(card.x or 0),
        "y": float(card.y or 0),
        "text": card.text or "",
        "attachments": [_attachment_payload(a) for a in (card.attachments or [])],
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
        "attachments": [_attachment_export_payload(a) for a in (card.attachments or [])],
        "updated_at": card.updated_at.isoformat() + "Z" if card.updated_at else None,
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
        .order_by(WhiteboardCard.updated_at.asc())
        .all()
    )
    last_event_id = (
        db.session.query(func.max(WhiteboardEvent.id)).filter(WhiteboardEvent.board_date == date_str).scalar()
    ) or 0
    return jsonify(
        {
            "date": date_str,
            "cards": [_card_payload(card) for card in cards],
            "last_event_id": int(last_event_id),
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

    text = str(payload.get("text", "")).strip()
    x = float(payload.get("x", 20))
    y = float(payload.get("y", 20))

    card = WhiteboardCard(board_date=date_str, text=text, x=x, y=y, created_by_id=user.id if user else None)
    db.session.add(card)
    db.session.flush()
    _emit_event(date_str, "create", card.id, user.id, {"card": _card_payload(card)})
    db.session.commit()
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

    text = str(request.form.get("text", "")).strip()
    try:
        x = float(request.form.get("x", 20))
        y = float(request.form.get("y", 20))
    except Exception:
        return jsonify({"error": "invalid position"}), 400

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

    card = WhiteboardCard(board_date=date_str, text=text, x=x, y=y, created_by_id=user.id if user else None)
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
    return jsonify({"card": _card_payload(card)})


@whiteboard_bp.route("/api/whiteboard/cards/<int:card_id>", methods=["PATCH"])
@login_required()
def update_card(card_id: int):
    user = g.get("user")
    card = WhiteboardCard.query.get_or_404(card_id)
    payload = request.get_json(silent=True) or {}
    changed = {}
    if "text" in payload:
        text = str(payload.get("text", ""))
        if len(text) > 5000:
            return jsonify({"error": "text too long"}), 400
        card.text = text
        changed["text"] = text
    if "x" in payload:
        card.x = float(payload.get("x", card.x or 0))
        changed["x"] = float(card.x or 0)
    if "y" in payload:
        card.y = float(payload.get("y", card.y or 0))
        changed["y"] = float(card.y or 0)

    if not changed:
        return jsonify({"card": _card_payload(card)})

    db.session.add(card)
    db.session.flush()
    _emit_event(card.board_date, "update", card.id, user.id, {"changes": changed})
    db.session.commit()
    return jsonify({"card": _card_payload(card)})


@whiteboard_bp.route("/api/whiteboard/cards/<int:card_id>", methods=["DELETE"])
@login_required()
def delete_card(card_id: int):
    user = g.get("user")
    card = WhiteboardCard.query.get_or_404(card_id)
    date_str = card.board_date
    keys = [a.oss_key for a in (card.attachments or []) if a.oss_key]
    db.session.delete(card)
    _emit_event(date_str, "delete", card_id, user.id, {})
    db.session.commit()
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

    cards = (
        WhiteboardCard.query.options(joinedload(WhiteboardCard.attachments))
        .filter_by(board_date=date_str)
        .order_by(WhiteboardCard.updated_at.asc())
        .all()
    )
    return jsonify(
        {
            "schema_version": 1,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "date": date_str,
            "cards": [_card_export_payload(card) for card in cards],
        }
    )


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
    if int(board.get("schema_version") or 0) != 1:
        return jsonify({"error": "unsupported schema"}), 400

    raw_cards = board.get("cards") or []
    if not isinstance(raw_cards, list):
        return jsonify({"error": "invalid cards"}), 400

    keys_to_delete: list[str] = []
    if mode == "replace":
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
    skipped_attachments = 0
    src_prefix = whiteboard_prefix().rstrip("/") + "/"

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

        card = WhiteboardCard(board_date=date_str, x=x, y=y, text=text, created_by_id=user.id if user else None)
        db.session.add(card)
        db.session.flush()
        created_cards += 1

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

    _emit_event(date_str, "reset", 0, user.id, {"mode": mode, "cards": created_cards})
    db.session.commit()

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
            "skipped_attachments": skipped_attachments,
            "deleted_failed": deleted_failed,
        }
    )
