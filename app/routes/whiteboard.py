import json
from datetime import datetime

from flask import Blueprint, g, jsonify, request
from sqlalchemy import func

from ..extensions import db
from ..models import WhiteboardCard, WhiteboardEvent
from ..utils.session_auth import login_required


whiteboard_bp = Blueprint("whiteboard", __name__)


def _validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("invalid date") from exc
    return date_str


def _card_payload(card: WhiteboardCard) -> dict:
    return {
        "id": card.id,
        "date": card.board_date,
        "x": float(card.x or 0),
        "y": float(card.y or 0),
        "text": card.text or "",
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

    cards = WhiteboardCard.query.filter_by(board_date=date_str).order_by(WhiteboardCard.updated_at.asc()).all()
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
    db.session.delete(card)
    _emit_event(date_str, "delete", card_id, user.id, {})
    db.session.commit()
    return jsonify({"deleted": True, "id": card_id})

