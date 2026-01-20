from flask import Blueprint, g, jsonify, request

from ..extensions import db
from ..models import User
from ..utils.session_auth import login_required


account_bp = Blueprint("account", __name__)


@account_bp.route("/api/account", methods=["GET"])
@login_required()
def get_account():
    current = g.get("user")
    if current is None:
        return jsonify({"error": "login required"}), 401
    users = User.query.filter_by(is_active=True).order_by(User.created_at.desc()).all()
    others = [
        {
            "id": user.id,
            "username": user.username,
            "description": user.description or "",
        }
        for user in users
        if user.id != current.id
    ]
    return jsonify(
        {
            "current_user": {
                "id": current.id,
                "username": current.username,
                "role": current.role,
                "description": current.description or "",
            },
            "accounts": others,
        }
    )


@account_bp.route("/api/account/description", methods=["PATCH"])
@login_required()
def update_description():
    current = g.get("user")
    if current is None:
        return jsonify({"error": "login required"}), 401
    payload = request.get_json(silent=True) or {}
    description = str(payload.get("description", "")).strip()
    if len(description) > 500:
        return jsonify({"error": "description too long"}), 400
    current.description = description
    db.session.commit()
    return jsonify({"description": current.description or ""})
