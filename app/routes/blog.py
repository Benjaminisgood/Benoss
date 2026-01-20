import os

from flask import Blueprint, jsonify, request

from ..oss import get_object_text, list_objects, public_url
from ..services.album_service import media_type_for_path, preview_url_for_key
from ..utils.markdown import extract_title, find_attachment_refs
from ..utils.oss_paths import blog_prefix, ensure_relative_key, resolve_attachment_key, resolve_module_key
from ..utils.session_auth import login_required


blog_bp = Blueprint("blog", __name__)


@blog_bp.route("/api/blog", methods=["GET"])
@login_required()
def list_blog():
    prefix = blog_prefix()
    items = []
    for key in list_objects(prefix, suffix=".md"):
        rel = key[len(prefix) + 1 :]
        title = os.path.splitext(os.path.basename(rel))[0]
        items.append({"key": rel, "title": title})

    items.sort(key=lambda item: item["key"])
    return jsonify({"items": items})


@blog_bp.route("/api/blog/item", methods=["GET"])
@login_required()
def get_blog_item():
    rel_key = request.args.get("key", "").strip()
    if not rel_key:
        return jsonify({"error": "missing key"}), 400

    try:
        rel_key = ensure_relative_key(rel_key)
    except ValueError:
        return jsonify({"error": "invalid key"}), 400

    key = resolve_module_key("blog", rel_key)
    content = get_object_text(key)
    title = extract_title(content, os.path.splitext(os.path.basename(rel_key))[0])

    attachments = []
    seen_refs = set()
    for candidates in find_attachment_refs(content):
        for ref in candidates:
            if ref in seen_refs:
                break
            try:
                att_key = resolve_attachment_key("blog", rel_key, ref, check_exists=True)
            except ValueError:
                continue
            media_type = media_type_for_path(att_key)
            attachments.append(
                {
                    "ref": ref,
                    "oss_key": att_key,
                    "url": public_url(att_key),
                    "media_type": media_type,
                    "preview_url": preview_url_for_key(att_key, media_type),
                }
            )
            seen_refs.add(ref)
            break

    return jsonify(
        {
            "key": rel_key,
            "title": title,
            "content": content,
            "attachments": attachments,
        }
    )
