from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import User
from ..utils.session_auth import login_required


admin_bp = Blueprint("admin", __name__)


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": bool(user.is_active),
        "description": user.description or "",
        "created_at": user.created_at.isoformat() + "Z" if user.created_at else None,
    }


@admin_bp.route("/api/admin/users", methods=["GET"])
@login_required(role="admin")
def list_users():
    users = User.query.order_by(User.created_at.desc(), User.id.desc()).all()
    return jsonify({"items": [_user_payload(user) for user in users]})


@admin_bp.route("/api/admin/users", methods=["POST"])
@login_required(role="admin")
def create_user():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    if not username or not password:
        return jsonify({"error": "missing fields"}), 400
    # Locked: only the seeded ADMIN_USERNAME account is admin.
    if "role" in payload and str(payload.get("role") or "").strip() not in {"", "user"}:
        return jsonify({"error": "role locked"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "user exists"}), 409

    user = User(username=username, role="user", is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({"user": _user_payload(user)})


@admin_bp.route("/api/admin/users/<int:user_id>", methods=["PATCH"])
@login_required(role="admin")
def update_user(user_id: int):
    payload = request.get_json(silent=True) or {}
    user = User.query.get_or_404(user_id)
    current = g.get("user")

    if "role" in payload:
        return jsonify({"error": "role locked"}), 400

    if "is_active" in payload:
        # Prevent locking yourself out.
        is_active = bool(payload.get("is_active"))
        if current and user.id == current.id and not is_active:
            return jsonify({"error": "cannot deactivate yourself"}), 400
        user.is_active = is_active

    if "password" in payload:
        password = str(payload.get("password") or "")
        if not password:
            return jsonify({"error": "missing password"}), 400
        user.set_password(password)

    db.session.commit()
    return jsonify({"user": _user_payload(user)})
