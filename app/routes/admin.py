from flask import Blueprint, jsonify, request

from ..extensions import db
from ..models import FriendLink, QuickLink, User
from ..utils.auth import generate_token, require_role
from ..utils.session_auth import login_required


admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/api/admin/login", methods=["POST"])
def admin_login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username", "").strip()
    password = payload.get("password", "")
    if not username or not password:
        return jsonify({"error": "missing credentials"}), 400

    user = User.query.filter_by(username=username).first()
    if user is None or not user.check_password(password):
        return jsonify({"error": "invalid credentials"}), 401

    token = generate_token(user)
    return jsonify({"token": token, "role": user.role})


@admin_bp.route("/api/admin/users", methods=["GET"])
@require_role("admin")
def list_users():
    users = User.query.order_by(User.created_at.desc()).all()
    items = [
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() + "Z",
        }
        for user in users
    ]
    return jsonify({"items": items})


@admin_bp.route("/api/admin/users", methods=["POST"])
@require_role("admin")
def create_user():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username", "").strip()
    password = payload.get("password", "")
    role = payload.get("role", "user")

    if not username or not password:
        return jsonify({"error": "missing fields"}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({"error": "user exists"}), 409

    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    return jsonify({"id": user.id, "username": user.username, "role": user.role})


@admin_bp.route("/api/admin/links/quick", methods=["GET"])
@require_role("admin")
def list_quick_links():
    links = QuickLink.query.order_by(QuickLink.sort_order.asc()).all()
    items = [
        {
            "id": link.id,
            "title": link.title,
            "url": link.url,
            "sort_order": link.sort_order,
            "is_active": link.is_active,
        }
        for link in links
    ]
    return jsonify({"items": items})


@admin_bp.route("/api/admin/links/quick", methods=["POST"])
@require_role("admin")
def create_quick_link():
    payload = request.get_json(silent=True) or {}
    title = payload.get("title", "").strip()
    url = payload.get("url", "").strip()
    sort_order = int(payload.get("sort_order", 0))
    is_active = bool(payload.get("is_active", True))

    if not title or not url:
        return jsonify({"error": "missing fields"}), 400

    link = QuickLink(title=title, url=url, sort_order=sort_order, is_active=is_active)
    db.session.add(link)
    db.session.commit()

    return jsonify({"id": link.id})


@admin_bp.route("/api/admin/links/quick/<int:link_id>", methods=["PATCH"])
@require_role("admin")
def update_quick_link(link_id: int):
    payload = request.get_json(silent=True) or {}
    link = QuickLink.query.get_or_404(link_id)
    if "title" in payload:
        link.title = payload["title"]
    if "url" in payload:
        link.url = payload["url"]
    if "sort_order" in payload:
        link.sort_order = int(payload["sort_order"])
    if "is_active" in payload:
        link.is_active = bool(payload["is_active"])
    db.session.commit()
    return jsonify({"id": link.id})


@admin_bp.route("/api/admin/links/quick/<int:link_id>", methods=["DELETE"])
@require_role("admin")
def delete_quick_link(link_id: int):
    link = QuickLink.query.get_or_404(link_id)
    db.session.delete(link)
    db.session.commit()
    return jsonify({"deleted": True})


@admin_bp.route("/api/admin/links/friend", methods=["GET"])
@require_role("admin")
def list_friend_links():
    links = FriendLink.query.order_by(FriendLink.sort_order.asc()).all()
    items = [
        {
            "id": link.id,
            "title": link.title,
            "url": link.url,
            "avatar_url": link.avatar_url,
            "description": link.description,
            "sort_order": link.sort_order,
            "is_active": link.is_active,
        }
        for link in links
    ]
    return jsonify({"items": items})


@admin_bp.route("/api/admin/links/friend", methods=["POST"])
@require_role("admin")
def create_friend_link():
    payload = request.get_json(silent=True) or {}
    title = payload.get("title", "").strip()
    url = payload.get("url", "").strip()
    avatar_url = payload.get("avatar_url", "").strip() or None
    description = payload.get("description", "").strip() or None
    sort_order = int(payload.get("sort_order", 0))
    is_active = bool(payload.get("is_active", True))

    if not title or not url:
        return jsonify({"error": "missing fields"}), 400

    link = FriendLink(
        title=title,
        url=url,
        avatar_url=avatar_url,
        description=description,
        sort_order=sort_order,
        is_active=is_active,
    )
    db.session.add(link)
    db.session.commit()

    return jsonify({"id": link.id})


@admin_bp.route("/api/admin/links/friend/<int:link_id>", methods=["PATCH"])
@require_role("admin")
def update_friend_link(link_id: int):
    payload = request.get_json(silent=True) or {}
    link = FriendLink.query.get_or_404(link_id)
    if "title" in payload:
        link.title = payload["title"]
    if "url" in payload:
        link.url = payload["url"]
    if "avatar_url" in payload:
        link.avatar_url = payload["avatar_url"]
    if "description" in payload:
        link.description = payload["description"]
    if "sort_order" in payload:
        link.sort_order = int(payload["sort_order"])
    if "is_active" in payload:
        link.is_active = bool(payload["is_active"])
    db.session.commit()
    return jsonify({"id": link.id})


@admin_bp.route("/api/admin/links/friend/<int:link_id>", methods=["DELETE"])
@require_role("admin")
def delete_friend_link(link_id: int):
    link = FriendLink.query.get_or_404(link_id)
    db.session.delete(link)
    db.session.commit()
    return jsonify({"deleted": True})


@admin_bp.route("/api/site/links", methods=["GET"])
@login_required()
def public_links():
    quick_links = QuickLink.query.filter_by(is_active=True).order_by(QuickLink.sort_order.asc()).all()
    friend_links = FriendLink.query.filter_by(is_active=True).order_by(FriendLink.sort_order.asc()).all()

    return jsonify(
        {
            "quick": [
                {
                    "title": link.title,
                    "url": link.url,
                    "sort_order": link.sort_order,
                }
                for link in quick_links
            ],
            "friends": [
                {
                    "title": link.title,
                    "url": link.url,
                    "avatar_url": link.avatar_url,
                    "description": link.description,
                    "sort_order": link.sort_order,
                }
                for link in friend_links
            ],
        }
    )
