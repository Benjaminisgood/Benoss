from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import User
from ..utils.session_auth import login_required


account_bp = Blueprint("account", __name__)


@account_bp.route("/api/account", methods=["GET"])
@login_required()
def get_account():
    current = g.get("user")
    users = (
        User.query.filter_by(is_active=True)
        .order_by(User.username.asc(), User.id.asc())
        .all()
    )
    return jsonify(
        {
            "current_user": {
                "id": current.id,
                "username": current.username,
                "role": current.role,
                "description": current.description or "",
            },
            "users": [
                {
                    "id": user.id,
                    "username": user.username,
                    "description": user.description or "",
                }
                for user in users
            ],
        }
    )


@account_bp.route("/api/account/description", methods=["PATCH"])
@login_required()
def update_description():
    current = g.get("user")
    payload = request.get_json(silent=True) or {}
    description = str(payload.get("description") or "").strip()
    if len(description) > 500:
        return jsonify({"error": "description too long"}), 400
    current.description = description
    db.session.commit()
    return jsonify({"description": current.description})


@account_bp.route("/api/users", methods=["GET"])
@login_required()
def list_users():
    users = User.query.filter_by(is_active=True).order_by(User.username.asc()).all()
    return jsonify(
        {
            "items": [
                {
                    "id": user.id,
                    "username": user.username,
                }
                for user in users
            ]
        }
    )
