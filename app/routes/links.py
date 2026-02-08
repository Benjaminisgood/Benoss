from flask import Blueprint, jsonify, request

from ..extensions import db
from ..models import QuickLink
from ..utils.session_auth import login_required


links_bp = Blueprint("links", __name__)


@links_bp.route("/api/links/quick", methods=["GET"])
@login_required()
def list_quick_links():
    links = QuickLink.query.filter_by(is_active=True).order_by(QuickLink.sort_order.asc(), QuickLink.id.asc()).all()
    return jsonify(
        {
            "items": [
                {
                    "id": link.id,
                    "title": link.title,
                    "url": link.url,
                    "sort_order": int(link.sort_order or 0),
                }
                for link in links
            ]
        }
    )


@links_bp.route("/api/links/quick", methods=["POST"])
@login_required()
def create_quick_link():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    url = str(payload.get("url", "")).strip()
    sort_order = int(payload.get("sort_order", 0))
    if not title or not url:
        return jsonify({"error": "missing fields"}), 400
    if len(title) > 120 or len(url) > 512:
        return jsonify({"error": "fields too long"}), 400
    link = QuickLink(title=title, url=url, sort_order=sort_order, is_active=True)
    db.session.add(link)
    db.session.commit()
    return jsonify({"id": link.id})


@links_bp.route("/api/links/quick/<int:link_id>", methods=["PATCH"])
@login_required()
def update_quick_link(link_id: int):
    payload = request.get_json(silent=True) or {}
    link = QuickLink.query.get_or_404(link_id)
    if "title" in payload:
        title = str(payload.get("title", "")).strip()
        if not title or len(title) > 120:
            return jsonify({"error": "invalid title"}), 400
        link.title = title
    if "url" in payload:
        url = str(payload.get("url", "")).strip()
        if not url or len(url) > 512:
            return jsonify({"error": "invalid url"}), 400
        link.url = url
    if "sort_order" in payload:
        link.sort_order = int(payload.get("sort_order", 0))
    db.session.commit()
    return jsonify({"id": link.id})


@links_bp.route("/api/links/quick/<int:link_id>", methods=["DELETE"])
@login_required()
def delete_quick_link(link_id: int):
    link = QuickLink.query.get_or_404(link_id)
    db.session.delete(link)
    db.session.commit()
    return jsonify({"deleted": True})

