import os
import re
from datetime import datetime
import posixpath
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db
from ..models import FriendLink, QuickLink, User
from ..oss import delete_object, get_object_text, object_exists, put_object_from_file, put_object_text
from ..utils.auth import generate_token, require_role
from ..utils.ids import new_uuid
from ..utils.markdown import find_attachment_refs
from ..utils.oss_paths import (
    attachment_key_for_ref,
    ensure_relative_key,
    resolve_attachment_key,
    resolve_module_key,
)
from ..utils.session_auth import login_required


admin_bp = Blueprint("admin", __name__)


_MD_LINK_RE = re.compile(r'(!?\[[^\]]*\]\()([^)]+)(\))')


def _normalize_attachment_ref(ref: str) -> str:
    ref = ref.strip().strip("\"").strip("'")
    ref = ref.split("?")[0].split("#")[0]
    ref = ref.split("|")[0]
    return ref


def _clean_upload_name(filename: str) -> str:
    if not filename:
        return ""
    cleaned = filename.replace("\\", "/").strip()
    if not cleaned:
        return ""
    name = cleaned.split("/")[-1].strip()
    if name in {"", ".", ".."}:
        return ""
    return name


def _normalize_post_slug(value: str) -> str:
    name = _clean_upload_name(value)
    if not name:
        return ""
    if name.lower().endswith(".md"):
        name = name[: -len(".md")]
    return name.strip()


def _split_md_target(target: str) -> tuple[str, str, bool]:
    target = target.strip()
    if not target:
        return "", "", False
    if target[0] == "<" and ">" in target:
        end = target.find(">")
        url = target[1:end]
        rest = target[end + 1 :]
        return url, rest, True
    parts = target.split(maxsplit=1)
    url = parts[0]
    rest = target[len(url) :]
    return url, rest, False


def _rewrite_attachment_refs(content: str, mapping: dict) -> str:
    if not mapping:
        return content
    updated = content
    for old_ref, new_ref in mapping.items():
        pattern = re.compile(rf'(!?\[\[){re.escape(old_ref)}([^\]]*)\]\]')
        updated = pattern.sub(rf"\1{new_ref}\2]]", updated)

    def _replace_md_target(match: re.Match) -> str:
        target = match.group(2)
        url, rest, wrapped = _split_md_target(target)
        if not url:
            return match.group(0)
        normalized = _normalize_attachment_ref(url)
        new_ref = mapping.get(normalized)
        if not new_ref:
            return match.group(0)
        new_url = f"<{new_ref}>" if wrapped else new_ref
        return f"{match.group(1)}{new_url}{rest}{match.group(3)}"

    updated = _MD_LINK_RE.sub(_replace_md_target, updated)
    return updated


def _collect_attachment_keys(module: str, base_rel_key: str, content: str) -> list[str]:
    keys = []
    seen_refs = set()
    seen_keys = set()
    for candidates in find_attachment_refs(content):
        for ref in candidates:
            if ref in seen_refs:
                break
            try:
                att_key = resolve_attachment_key(module, base_rel_key, ref, check_exists=True)
            except ValueError:
                continue
            if att_key not in seen_keys:
                keys.append(att_key)
                seen_keys.add(att_key)
            seen_refs.add(ref)
            break
    return keys


def _build_upload_lookup(files) -> tuple[dict, dict]:
    by_name = {}
    by_stem = {}
    for file_obj in files:
        name = _clean_upload_name(file_obj.filename or "")
        if not name:
            continue
        if name in by_name:
            raise ValueError(f"duplicate attachment name: {name}")
        by_name[name] = file_obj
        stem = Path(name).stem
        by_stem.setdefault(stem, []).append(name)
    return by_name, by_stem


def _match_upload_ref(ref: str, by_name: dict, by_stem: dict) -> str:
    base = Path(ref).name
    if base in by_name:
        return base
    if not Path(base).suffix:
        stem = Path(base).stem
        names = by_stem.get(stem) or []
        if len(names) == 1:
            return names[0]
    return ""


@admin_bp.route("/api/admin/posts", methods=["POST"])
@require_role("admin")
def upload_post():
    module = request.form.get("module", "").strip()
    if module not in {"blog", "note"}:
        return jsonify({"error": "invalid module"}), 400

    md_file = request.files.get("markdown")
    if not md_file or not md_file.filename:
        return jsonify({"error": "missing markdown file"}), 400

    raw_key = request.form.get("key", "").strip()
    if raw_key:
        slug = _normalize_post_slug(raw_key.replace("\\", "/"))
    else:
        slug = _normalize_post_slug(md_file.filename or "")
    if not slug:
        return jsonify({"error": "missing post name"}), 400

    date_prefix = datetime.now().strftime("%Y-%m-%d")
    rel_key = f"{date_prefix}-{slug}"
    try:
        rel_key = ensure_relative_key(rel_key)
    except ValueError:
        return jsonify({"error": "invalid key"}), 400
    md_rel_key = posixpath.join(rel_key, "index.md")

    try:
        content = md_file.read().decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "markdown must be utf-8"}), 400

    attachments = request.files.getlist("attachments")
    if not attachments:
        attachments = request.files.getlist("attachments[]")
    attachments = [item for item in attachments if item and item.filename]

    try:
        by_name, by_stem = _build_upload_lookup(attachments)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    ref_to_file = {}
    missing_refs = []
    for candidates in find_attachment_refs(content):
        matched = False
        for ref in candidates:
            if ref in ref_to_file:
                matched = True
                break
            file_name = _match_upload_ref(ref, by_name, by_stem)
            if file_name:
                ref_to_file[ref] = file_name
                matched = True
                break
        if not matched and candidates:
            missing_refs.append(candidates[0])

    file_name_map = {}
    for file_name in set(ref_to_file.values()):
        ext = os.path.splitext(file_name)[1]
        file_name_map[file_name] = f"{new_uuid()}{ext}"

    ref_map = {}
    for ref, file_name in ref_to_file.items():
        new_name = file_name_map[file_name]
        ref_map[ref] = new_name

    updated_content = _rewrite_attachment_refs(content, ref_map)

    upload_targets = {}
    for ref, file_name in ref_to_file.items():
        new_ref = ref_map[ref]
        try:
            oss_key = attachment_key_for_ref(module, md_rel_key, new_ref)
        except ValueError:
            return jsonify({"error": "invalid attachment path"}), 400
        existing = upload_targets.get(oss_key)
        if existing and existing != file_name:
            return jsonify({"error": "conflicting attachment upload"}), 400
        upload_targets[oss_key] = file_name

    warnings = []
    if missing_refs:
        warnings.append("Missing attachments: " + ", ".join(sorted(set(missing_refs))))
    unused_files = [name for name in by_name if name not in file_name_map]
    if unused_files:
        warnings.append("Unused uploads: " + ", ".join(sorted(unused_files)))

    tmp_dir = Path(current_app.config["UPLOAD_TMP_DIR"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    uploaded_keys = []
    try:
        for oss_key, file_name in upload_targets.items():
            file_obj = by_name[file_name]
            ext = os.path.splitext(file_name)[1]
            tmp_path = tmp_dir / f"{new_uuid()}{ext}"
            try:
                file_obj.save(tmp_path)
                put_object_from_file(oss_key, str(tmp_path), content_type=file_obj.mimetype or None)
                uploaded_keys.append(oss_key)
            finally:
                tmp_path.unlink(missing_ok=True)
    except Exception:
        for key in uploaded_keys:
            try:
                delete_object(key)
            except Exception:
                current_app.logger.exception("Failed to rollback attachment %s", key)
        current_app.logger.exception("Failed to upload attachments")
        return jsonify({"error": "attachment upload failed"}), 500

    md_key = resolve_module_key(module, md_rel_key)
    try:
        put_object_text(md_key, updated_content, content_type="text/markdown; charset=utf-8")
    except Exception:
        for key in uploaded_keys:
            try:
                delete_object(key)
            except Exception:
                current_app.logger.exception("Failed to rollback attachment %s", key)
        current_app.logger.exception("Failed to upload markdown %s", md_key)
        return jsonify({"error": "markdown upload failed"}), 500

    return jsonify(
        {
            "uploaded": True,
            "key": rel_key,
            "attachments_uploaded": len(upload_targets),
            "warnings": warnings,
        }
    )


@admin_bp.route("/api/admin/posts", methods=["DELETE"])
@require_role("admin")
def delete_post():
    payload = request.get_json(silent=True) or {}
    module = str(payload.get("module", "")).strip()
    rel_key = str(payload.get("key", "")).strip()

    if module not in {"blog", "note"}:
        return jsonify({"error": "invalid module"}), 400
    if not rel_key:
        return jsonify({"error": "missing key"}), 400
    try:
        rel_key = ensure_relative_key(rel_key)
    except ValueError:
        return jsonify({"error": "invalid key"}), 400
    if rel_key.lower().endswith(".md"):
        return jsonify({"error": "invalid key"}), 400

    md_rel_key = posixpath.join(rel_key, "index.md")
    md_key = resolve_module_key(module, md_rel_key)
    if not object_exists(md_key):
        return jsonify({"error": "not found"}), 404
    try:
        content = get_object_text(md_key)
    except Exception:
        current_app.logger.exception("Failed to read markdown %s", md_key)
        return jsonify({"error": "failed to read markdown"}), 500

    attachment_keys = _collect_attachment_keys(module, md_rel_key, content)
    failed = []
    deleted_count = 0
    for key in attachment_keys:
        try:
            delete_object(key)
            deleted_count += 1
        except Exception:
            failed.append(key)
            current_app.logger.exception("Failed to delete attachment %s", key)

    try:
        delete_object(md_key)
    except Exception:
        current_app.logger.exception("Failed to delete markdown %s", md_key)
        return jsonify({"error": "failed to delete markdown"}), 500

    return jsonify(
        {
            "deleted": True,
            "key": rel_key,
            "attachments_deleted": deleted_count,
            "attachments_failed": failed,
        }
    )


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
