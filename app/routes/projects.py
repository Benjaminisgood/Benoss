import hashlib
import json
import posixpath
from datetime import datetime
from pathlib import Path, PurePosixPath

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Project, ProjectActivity, ProjectFile, PushRequest, PushRequestFile, User, WhiteboardAttachment, WhiteboardCard
from ..oss import copy_object, delete_object, get_object_bytes, public_url, put_object_from_file
from ..utils.ids import new_uuid
from ..utils.oss_paths import project_object_key
from ..utils.session_auth import login_required


projects_bp = Blueprint("projects", __name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_DOC_EXTS = {".pdf"}
_TEXT_EXTS = {
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".csv",
    ".tsv",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".xml",
    ".sh",
    ".zsh",
    ".bash",
    ".sql",
}
_VALID_MEDIA_TYPES = {"image", "video", "audio", "pdf", "text", "file"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _media_type_for(path: str, content_type: str = "") -> str:
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXTS or (content_type or "").startswith("image/"):
        return "image"
    if ext in _VIDEO_EXTS or (content_type or "").startswith("video/"):
        return "video"
    if ext in _AUDIO_EXTS or (content_type or "").startswith("audio/"):
        return "audio"
    if ext in _DOC_EXTS or (content_type or "").lower() == "application/pdf":
        return "pdf"
    if ext in _TEXT_EXTS or (content_type or "").startswith("text/"):
        return "text"
    return "file"


def _effective_media_type(stored: str | None, path: str, content_type: str = "") -> str:
    """Return a stable media_type even if older DB rows stored a generic 'file'."""

    derived = _media_type_for(path, content_type or "")
    raw = (stored or "").strip().lower()
    if not raw or raw == "file":
        return derived
    if raw not in _VALID_MEDIA_TYPES:
        return derived
    return raw


def _preview_params(media_type: str) -> dict | None:
    if media_type == "image":
        return {"x-oss-process": "image/resize,w_480/quality,q_70"}
    if media_type == "video":
        return {"x-oss-process": "video/snapshot,t_1000,f_jpg,w_480"}
    if media_type == "pdf":
        return {"x-oss-process": "doc/preview,format=jpg,page=1"}
    return None


def _safe_relpath(value: str, fallback: str = "file") -> str:
    raw = (value or "").replace("\\", "/").strip()
    if not raw:
        raw = fallback
    raw = raw.lstrip("/")
    raw = posixpath.normpath(raw)
    if raw in {"", ".", "/"}:
        raw = fallback
    # Reject traversal or weird segments.
    parts = PurePosixPath(raw).parts
    if not parts or any(p in {"", ".", ".."} for p in parts) or ".." in parts:
        raise ValueError("invalid path")
    if len(raw) > 500:
        raise ValueError("path too long")
    # Preserve case and unicode; key safety is handled by OSS key randomization.
    return raw


def _require_project_view(project: Project, user: User) -> None:
    if project.visibility == "public":
        return
    if user and project.owner_id == user.id:
        return
    raise PermissionError("forbidden")


def _require_project_edit(project: Project, user: User) -> None:
    if user and project.owner_id == user.id:
        return
    raise PermissionError("forbidden")


def _project_payload(project: Project, file_count: int | None = None) -> dict:
    return {
        "id": project.id,
        "uuid": project.uuid,
        "module": project.module,
        "title": project.title,
        "description": project.description or "",
        "visibility": project.visibility,
        "is_archived": bool(project.is_archived),
        "owner": {
            "id": project.owner.id if project.owner else project.owner_id,
            "username": project.owner.username if project.owner else "",
        },
        "cloned_from_id": project.cloned_from_id,
        "created_at": project.created_at.isoformat() + "Z" if project.created_at else None,
        "updated_at": project.updated_at.isoformat() + "Z" if project.updated_at else None,
        "file_count": file_count,
    }


def _file_payload(file: ProjectFile) -> dict:
    media_type = _effective_media_type(file.media_type, file.path or "", file.content_type or "")
    params = _preview_params(media_type)
    return {
        "id": file.id,
        "path": file.path,
        "media_type": media_type,
        "content_type": file.content_type or "",
        "size_bytes": int(file.size_bytes or 0),
        "sha256": file.sha256 or "",
        "url": public_url(file.oss_key, expires=3600),
        "preview_url": public_url(file.oss_key, expires=3600, params=params) if params else "",
        "created_at": file.created_at.isoformat() + "Z" if file.created_at else None,
        "updated_at": file.updated_at.isoformat() + "Z" if file.updated_at else None,
    }


def _whiteboard_attachment_payload(att: WhiteboardAttachment, card: WhiteboardCard) -> dict:
    media_type = _effective_media_type(att.media_type, att.filename or "", att.content_type or "")
    params = _preview_params(media_type)
    filename = (att.filename or "").strip() or "file"
    text = (card.text or "") if card else ""
    if len(text) > 500:
        text = text[:500]
    return {
        "id": att.id,
        "source": "whiteboard",
        "path": filename,
        "filename": filename,
        "media_type": media_type,
        "content_type": att.content_type or "",
        "size_bytes": int(att.size_bytes or 0),
        "sha256": att.sha256 or "",
        "url": public_url(att.oss_key, expires=3600),
        "preview_url": public_url(att.oss_key, expires=3600, params=params) if params else "",
        "created_at": att.created_at.isoformat() + "Z" if att.created_at else None,
        "updated_at": att.updated_at.isoformat() + "Z" if att.updated_at else None,
        "whiteboard": {
            "date": card.board_date if card else "",
            "card_id": card.id if card else None,
            "text": text,
        },
    }


def _activity_record(
    *,
    type_: str,
    project: Project,
    actor_user_id: int,
    initiator_user_id: int | None = None,
    meta: dict | None = None,
) -> None:
    record = ProjectActivity(
        type=type_,
        module=project.module,
        project_id=project.id,
        actor_user_id=actor_user_id,
        initiator_user_id=initiator_user_id,
        meta_json=json.dumps(meta or {}, ensure_ascii=True),
    )
    db.session.add(record)


@projects_bp.route("/api/projects", methods=["GET"])
@login_required()
def list_projects():
    user = g.get("user")
    module = (request.args.get("module") or "").strip()
    if module and module not in {"blog", "note"}:
        return jsonify({"error": "invalid module"}), 400

    query = Project.query.options(joinedload(Project.owner))
    if module:
        query = query.filter_by(module=module)

    query = query.filter(Project.is_archived.is_(False))
    query = query.filter(or_(Project.visibility == "public", Project.owner_id == user.id))
    query = query.order_by(Project.updated_at.desc(), Project.id.desc())
    projects = query.limit(500).all()

    ids = [p.id for p in projects]
    counts = {}
    if ids:
        for pid, cnt in (
            db.session.query(ProjectFile.project_id, func.count(ProjectFile.id))
            .filter(ProjectFile.project_id.in_(ids))
            .group_by(ProjectFile.project_id)
            .all()
        ):
            counts[int(pid)] = int(cnt or 0)

    items = [_project_payload(p, counts.get(p.id, 0)) for p in projects]
    return jsonify({"items": items})


@projects_bp.route("/api/projects", methods=["POST"])
@login_required()
def create_project():
    user = g.get("user")
    payload = request.get_json(silent=True) or {}
    module = str(payload.get("module", "")).strip()
    title = str(payload.get("title", "")).strip()
    visibility = str(payload.get("visibility", "private")).strip().lower() or "private"
    description = str(payload.get("description", "")).strip()

    if module not in {"blog", "note"}:
        return jsonify({"error": "invalid module"}), 400
    if not title:
        return jsonify({"error": "missing title"}), 400
    if len(title) > 160:
        return jsonify({"error": "title too long"}), 400
    if visibility not in {"public", "private"}:
        return jsonify({"error": "invalid visibility"}), 400
    if len(description) > 2000:
        return jsonify({"error": "description too long"}), 400

    project = Project(
        module=module,
        owner_id=user.id,
        title=title,
        description=description,
        visibility=visibility,
    )
    db.session.add(project)
    db.session.flush()

    _activity_record(type_="git", project=project, actor_user_id=user.id, meta={"title": title})
    db.session.commit()

    return jsonify({"project": _project_payload(project, 0)})


@projects_bp.route("/api/projects/<int:project_id>", methods=["GET"])
@login_required()
def get_project(project_id: int):
    user = g.get("user")
    project = Project.query.get_or_404(project_id)
    try:
        _require_project_view(project, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403

    files = ProjectFile.query.filter_by(project_id=project.id).order_by(ProjectFile.path.asc()).all()
    payload = _project_payload(project, len(files))
    payload["can_edit"] = bool(user and project.owner_id == user.id)
    return jsonify({"project": payload, "files": [_file_payload(f) for f in files]})


@projects_bp.route("/api/projects/<int:project_id>", methods=["PATCH"])
@login_required()
def update_project(project_id: int):
    user = g.get("user")
    project = Project.query.get_or_404(project_id)
    try:
        _require_project_edit(project, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    changed = False
    if "title" in payload:
        title = str(payload.get("title", "")).strip()
        if not title:
            return jsonify({"error": "missing title"}), 400
        if len(title) > 160:
            return jsonify({"error": "title too long"}), 400
        project.title = title
        changed = True
    if "description" in payload:
        description = str(payload.get("description", "")).strip()
        if len(description) > 2000:
            return jsonify({"error": "description too long"}), 400
        project.description = description
        changed = True
    if "visibility" in payload:
        visibility = str(payload.get("visibility", "")).strip().lower()
        if visibility not in {"public", "private"}:
            return jsonify({"error": "invalid visibility"}), 400
        project.visibility = visibility
        changed = True

    if changed:
        _activity_record(type_="project_update", project=project, actor_user_id=user.id, meta={"fields": list(payload.keys())})
        db.session.commit()

    files_count = ProjectFile.query.filter_by(project_id=project.id).count()
    return jsonify({"project": _project_payload(project, files_count)})


@projects_bp.route("/api/projects/<int:project_id>", methods=["DELETE"])
@login_required()
def delete_project(project_id: int):
    user = g.get("user")
    project = Project.query.get_or_404(project_id)
    try:
        _require_project_edit(project, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403

    files = ProjectFile.query.filter_by(project_id=project.id).all()
    keys = [f.oss_key for f in files if f.oss_key]
    db.session.delete(project)
    db.session.commit()

    failed = 0
    for key in keys:
        try:
            delete_object(key)
        except Exception:
            failed += 1
            current_app.logger.exception("Failed to delete project object %s", key)

    return jsonify({"deleted": True, "objects_failed": failed})


@projects_bp.route("/api/projects/<int:project_id>/files/upload", methods=["POST"])
@login_required()
def upload_project_file(project_id: int):
    user = g.get("user")
    project = Project.query.get_or_404(project_id)
    try:
        _require_project_edit(project, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403

    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400
    file_obj = request.files["file"]
    if not file_obj or not file_obj.filename:
        return jsonify({"error": "missing filename"}), 400

    # Keep nested paths from folder uploads if provided explicitly.
    requested_path = request.form.get("path") or request.form.get("relative_path") or ""
    fallback = (file_obj.filename or "").replace("\\", "/").split("/")[-1]
    fallback = fallback.strip() or "file"
    try:
        rel_path = _safe_relpath(requested_path, fallback=fallback)
    except ValueError:
        return jsonify({"error": "invalid path"}), 400

    ext = Path(rel_path).suffix.lower()
    # Use secure filename for extension fallback only (OSS key is randomized anyway).
    if not ext:
        cleaned = secure_filename(fallback)
        ext = Path(cleaned).suffix.lower()
    file_uuid = new_uuid()
    oss_key = project_object_key(project.uuid, f"objects/{file_uuid}{ext}")

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
    try:
        sha256 = _sha256_file(tmp_path)
    except Exception:
        sha256 = ""

    existing = ProjectFile.query.filter_by(project_id=project.id, path=rel_path).first()
    # Idempotency: if content is unchanged, don't churn OSS objects or DB.
    if (
        existing
        and sha256
        and existing.sha256
        and existing.sha256 == sha256
        and int(existing.size_bytes or 0) == int(size_bytes or 0)
    ):
        tmp_path.unlink(missing_ok=True)
        return jsonify({"uploaded": True, "skipped": True, "file": _file_payload(existing)})

    try:
        put_object_from_file(oss_key, str(tmp_path), content_type=file_obj.mimetype or None)
    finally:
        tmp_path.unlink(missing_ok=True)

    media_type = _media_type_for(rel_path, file_obj.mimetype or "")

    replaced_key = None
    if existing:
        replaced_key = existing.oss_key
        existing.oss_key = oss_key
        existing.content_type = file_obj.mimetype or ""
        existing.media_type = media_type
        existing.size_bytes = size_bytes
        if sha256:
            existing.sha256 = sha256
        existing.updated_by_id = user.id
    else:
        existing = ProjectFile(
            project_id=project.id,
            path=rel_path,
            oss_key=oss_key,
            content_type=file_obj.mimetype or "",
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha256,
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        db.session.add(existing)

    project.updated_at = datetime.utcnow()
    _activity_record(type_="push", project=project, actor_user_id=user.id, meta={"path": rel_path})
    db.session.commit()

    if replaced_key and replaced_key != oss_key:
        try:
            delete_object(replaced_key)
        except Exception:
            current_app.logger.exception("Failed to delete replaced object %s", replaced_key)

    return jsonify({"uploaded": True, "file": _file_payload(existing)})


@projects_bp.route("/api/projects/<int:project_id>/files", methods=["DELETE"])
@login_required()
def delete_project_file(project_id: int):
    user = g.get("user")
    project = Project.query.get_or_404(project_id)
    try:
        _require_project_edit(project, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    path = str(payload.get("path", "")).strip()
    if not path:
        return jsonify({"error": "missing path"}), 400
    try:
        rel_path = _safe_relpath(path, fallback="file")
    except ValueError:
        return jsonify({"error": "invalid path"}), 400

    record = ProjectFile.query.filter_by(project_id=project.id, path=rel_path).first()
    if not record:
        return jsonify({"error": "not found"}), 404
    oss_key = record.oss_key
    db.session.delete(record)
    project.updated_at = datetime.utcnow()
    _activity_record(type_="push", project=project, actor_user_id=user.id, meta={"delete": rel_path})
    db.session.commit()

    if oss_key:
        try:
            delete_object(oss_key)
        except Exception:
            current_app.logger.exception("Failed to delete object %s", oss_key)
    return jsonify({"deleted": True, "path": rel_path})


@projects_bp.route("/api/projects/<int:project_id>/file/text", methods=["GET", "PUT"])
@login_required()
def get_project_file_text(project_id: int):
    user = g.get("user")
    project = Project.query.get_or_404(project_id)
    if request.method == "GET":
        try:
            _require_project_view(project, user)
        except PermissionError:
            return jsonify({"error": "forbidden"}), 403

        path = (request.args.get("path") or "").strip()
        if not path:
            return jsonify({"error": "missing path"}), 400
        try:
            rel_path = _safe_relpath(path, fallback="file")
        except ValueError:
            return jsonify({"error": "invalid path"}), 400

        record = ProjectFile.query.filter_by(project_id=project.id, path=rel_path).first()
        if not record:
            return jsonify({"error": "not found"}), 404
        if _effective_media_type(record.media_type, rel_path, record.content_type or "") != "text":
            return jsonify({"error": "not a text file"}), 400
        if record.size_bytes and record.size_bytes > 500_000:
            return jsonify({"error": "file too large"}), 400
        try:
            data = get_object_bytes(record.oss_key).decode("utf-8")
        except UnicodeDecodeError:
            return jsonify({"error": "file not utf-8"}), 400
        return jsonify({"path": rel_path, "text": data})

    # PUT: save edited text back to OSS + update ProjectFile pointer.
    try:
        _require_project_edit(project, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    path = str(payload.get("path", "")).strip()
    if not path:
        return jsonify({"error": "missing path"}), 400
    if "text" not in payload:
        return jsonify({"error": "missing text"}), 400
    text = payload.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "invalid text"}), 400

    try:
        rel_path = _safe_relpath(path, fallback="file")
    except ValueError:
        return jsonify({"error": "invalid path"}), 400

    record = ProjectFile.query.filter_by(project_id=project.id, path=rel_path).first()
    if not record:
        return jsonify({"error": "not found"}), 404
    if _effective_media_type(record.media_type, rel_path, record.content_type or "") != "text":
        return jsonify({"error": "not a text file"}), 400

    data = text.encode("utf-8")
    if len(data) > 500_000:
        return jsonify({"error": "file too large"}), 400

    sha256 = hashlib.sha256(data).hexdigest()
    size_bytes = int(len(data))
    if sha256 and record.sha256 and record.sha256 == sha256 and int(record.size_bytes or 0) == int(size_bytes or 0):
        return jsonify({"saved": True, "skipped": True, "file": _file_payload(record)})

    ext = Path(rel_path).suffix.lower()
    file_uuid = new_uuid()
    oss_key = project_object_key(project.uuid, f"objects/{file_uuid}{ext}")

    tmp_dir = Path(current_app.config["UPLOAD_TMP_DIR"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{file_uuid}{ext}"
    tmp_path.write_bytes(data)

    # Preserve existing content_type if present, otherwise default to text/plain.
    content_type = str((record.content_type or "")).strip()
    if not content_type or content_type.lower() == "application/octet-stream":
        content_type = "text/plain"

    try:
        put_object_from_file(oss_key, str(tmp_path), content_type=content_type or None)
    finally:
        tmp_path.unlink(missing_ok=True)

    replaced_key = record.oss_key
    record.oss_key = oss_key
    record.content_type = content_type
    record.media_type = "text"
    record.size_bytes = size_bytes
    record.sha256 = sha256
    record.updated_by_id = user.id

    project.updated_at = datetime.utcnow()
    _activity_record(type_="push", project=project, actor_user_id=user.id, meta={"path": rel_path, "edit": True})
    db.session.commit()

    if replaced_key and replaced_key != oss_key:
        try:
            delete_object(replaced_key)
        except Exception:
            current_app.logger.exception("Failed to delete replaced object %s", replaced_key)

    return jsonify({"saved": True, "file": _file_payload(record)})


@projects_bp.route("/api/projects/<int:project_id>/clone", methods=["POST"])
@login_required()
def clone_project(project_id: int):
    user = g.get("user")
    source = Project.query.get_or_404(project_id)
    try:
        _require_project_view(source, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403

    files = ProjectFile.query.filter_by(project_id=source.id).all()
    title = str((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        title = f"{source.title} (clone)"
    if len(title) > 160:
        title = title[:160]

    cloned = Project(
        module=source.module,
        owner_id=user.id,
        title=title,
        description=source.description or "",
        visibility="private",
        cloned_from_id=source.id,
    )
    db.session.add(cloned)
    db.session.flush()

    copied_keys = []
    try:
        for f in files:
            ext = Path(f.path).suffix.lower()
            file_uuid = new_uuid()
            target_key = project_object_key(cloned.uuid, f"objects/{file_uuid}{ext}")
            copy_object(f.oss_key, target_key)
            copied_keys.append(target_key)
            db.session.add(
                ProjectFile(
                    project_id=cloned.id,
                    path=f.path,
                    oss_key=target_key,
                    content_type=f.content_type or "",
                    media_type=_effective_media_type(f.media_type, f.path, f.content_type or ""),
                    size_bytes=int(f.size_bytes or 0),
                    sha256=f.sha256 or "",
                    created_by_id=user.id,
                    updated_by_id=user.id,
                )
            )
        _activity_record(type_="clone", project=source, actor_user_id=user.id, meta={"new_project_id": cloned.id})
        db.session.commit()
    except Exception:
        db.session.rollback()
        for key in copied_keys:
            try:
                delete_object(key)
            except Exception:
                current_app.logger.exception("Failed to rollback cloned object %s", key)
        current_app.logger.exception("Clone failed")
        return jsonify({"error": "clone failed"}), 500

    return jsonify({"project": _project_payload(cloned, len(files))})


@projects_bp.route("/api/projects/public/files", methods=["GET"])
@login_required()
def list_public_files():
    module = (request.args.get("module") or "").strip()
    media_type = (request.args.get("media_type") or "").strip()
    limit = int(request.args.get("limit", 200))
    offset = int(request.args.get("offset", 0))
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    if module and module not in {"blog", "note", "whiteboard"}:
        return jsonify({"error": "invalid module"}), 400
    if media_type and media_type not in {"image", "video", "audio", "pdf", "text", "file"}:
        return jsonify({"error": "invalid media_type"}), 400

    include_projects = module in {"", "blog", "note"}
    include_whiteboard = module in {"", "whiteboard"}
    fetch_n = offset + limit

    rows: list[tuple[datetime, int, dict]] = []

    if include_projects:
        proj_query = (
            db.session.query(ProjectFile, Project, User)
            .join(Project, ProjectFile.project_id == Project.id)
            .join(User, Project.owner_id == User.id)
            .filter(Project.visibility == "public", Project.is_archived.is_(False))
        )
        if module in {"blog", "note"}:
            proj_query = proj_query.filter(Project.module == module)
        if media_type:
            proj_query = proj_query.filter(ProjectFile.media_type == media_type)
        proj_query = proj_query.order_by(ProjectFile.updated_at.desc(), ProjectFile.id.desc()).limit(fetch_n)
        for file, project, owner in proj_query.all():
            fp = _file_payload(file)
            fp["source"] = "project"
            fp["project"] = {
                "id": project.id,
                "module": project.module,
                "title": project.title,
                "owner_username": owner.username,
            }
            sort_dt = file.updated_at or file.created_at or datetime.min
            rows.append((sort_dt, int(file.id or 0), fp))

    if include_whiteboard:
        wb_query = (
            db.session.query(WhiteboardAttachment, WhiteboardCard)
            .join(WhiteboardCard, WhiteboardAttachment.card_id == WhiteboardCard.id)
            .order_by(WhiteboardAttachment.updated_at.desc(), WhiteboardAttachment.id.desc())
        )
        if media_type:
            wb_query = wb_query.filter(WhiteboardAttachment.media_type == media_type)
        wb_query = wb_query.limit(fetch_n)
        for att, card in wb_query.all():
            payload = _whiteboard_attachment_payload(att, card)
            sort_dt = att.updated_at or att.created_at or datetime.min
            rows.append((sort_dt, int(att.id or 0), payload))

    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    page = rows[offset : offset + limit]
    return jsonify({"items": [payload for _, __, payload in page]})


@projects_bp.route("/api/projects/<int:project_id>/push-requests", methods=["POST"])
@login_required()
def create_push_request(project_id: int):
    user = g.get("user")
    project = Project.query.get_or_404(project_id)
    try:
        _require_project_view(project, user)
    except PermissionError:
        return jsonify({"error": "forbidden"}), 403
    if project.owner_id == user.id:
        return jsonify({"error": "cannot push to your own project"}), 400

    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    if len(message) > 2000:
        return jsonify({"error": "message too long"}), 400

    pr = PushRequest(project_id=project.id, proposer_user_id=user.id, status="pending", message=message)
    db.session.add(pr)
    db.session.flush()
    _activity_record(type_="push_request", project=project, actor_user_id=user.id, meta={"push_request_id": pr.id})
    db.session.commit()
    return jsonify({"push_request": {"id": pr.id, "status": pr.status}})


@projects_bp.route("/api/push-requests/<int:push_request_id>/files/upload", methods=["POST"])
@login_required()
def upload_push_request_file(push_request_id: int):
    user = g.get("user")
    pr = PushRequest.query.get_or_404(push_request_id)
    if pr.proposer_user_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    if pr.status != "pending":
        return jsonify({"error": "push request closed"}), 400
    project = pr.project
    if not project:
        return jsonify({"error": "invalid project"}), 400

    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400
    file_obj = request.files["file"]
    if not file_obj or not file_obj.filename:
        return jsonify({"error": "missing filename"}), 400

    requested_path = request.form.get("path") or request.form.get("relative_path") or ""
    fallback = (file_obj.filename or "").replace("\\", "/").split("/")[-1]
    fallback = fallback.strip() or "file"
    try:
        rel_path = _safe_relpath(requested_path, fallback=fallback)
    except ValueError:
        return jsonify({"error": "invalid path"}), 400

    ext = Path(rel_path).suffix.lower()
    if not ext:
        cleaned = secure_filename(fallback)
        ext = Path(cleaned).suffix.lower()
    file_uuid = new_uuid()
    oss_key = project_object_key(project.uuid, f"push/{pr.id}/{file_uuid}{ext}")

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
    try:
        sha256 = _sha256_file(tmp_path)
    except Exception:
        sha256 = ""

    existing_remote = ProjectFile.query.filter_by(project_id=project.id, path=rel_path).first()
    # If the proposed content is identical to the current project file, treat as a no-op.
    if (
        existing_remote
        and sha256
        and existing_remote.sha256
        and existing_remote.sha256 == sha256
        and int(existing_remote.size_bytes or 0) == int(size_bytes or 0)
    ):
        tmp_path.unlink(missing_ok=True)
        return jsonify({"uploaded": True, "skipped": True, "reason": "unchanged"})

    existing_pr_file = (
        PushRequestFile.query.filter_by(push_request_id=pr.id, path=rel_path).order_by(PushRequestFile.id.desc()).first()
    )
    # If the same path already exists in this push request with identical content, skip OSS churn.
    if (
        existing_pr_file
        and sha256
        and existing_pr_file.sha256
        and existing_pr_file.sha256 == sha256
        and int(existing_pr_file.size_bytes or 0) == int(size_bytes or 0)
    ):
        tmp_path.unlink(missing_ok=True)
        return jsonify({"uploaded": True, "skipped": True, "reason": "duplicate"})

    try:
        put_object_from_file(oss_key, str(tmp_path), content_type=file_obj.mimetype or None)
    finally:
        tmp_path.unlink(missing_ok=True)

    media_type = _media_type_for(rel_path, file_obj.mimetype or "")

    replaced_key = None
    if existing_pr_file:
        replaced_key = existing_pr_file.oss_key
        existing_pr_file.oss_key = oss_key
        existing_pr_file.content_type = file_obj.mimetype or ""
        existing_pr_file.media_type = media_type
        existing_pr_file.size_bytes = size_bytes
        if sha256:
            existing_pr_file.sha256 = sha256
        record = existing_pr_file
    else:
        record = PushRequestFile(
            push_request_id=pr.id,
            path=rel_path,
            oss_key=oss_key,
            content_type=file_obj.mimetype or "",
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        db.session.add(record)

    db.session.commit()

    if replaced_key and replaced_key != oss_key:
        try:
            delete_object(replaced_key)
        except Exception:
            current_app.logger.exception("Failed to delete replaced push request object %s", replaced_key)

    return jsonify({"uploaded": True, "file": {"id": record.id, "path": record.path}})


@projects_bp.route("/api/collab/inbox", methods=["GET"])
@login_required()
def collab_inbox():
    user = g.get("user")
    query = (
        db.session.query(PushRequest, Project, User)
        .join(Project, PushRequest.project_id == Project.id)
        .join(User, PushRequest.proposer_user_id == User.id)
        .filter(Project.owner_id == user.id, PushRequest.status == "pending")
        .order_by(PushRequest.created_at.desc(), PushRequest.id.desc())
        .limit(200)
    )
    items = []
    for pr, project, proposer in query.all():
        items.append(
            {
                "id": pr.id,
                "status": pr.status,
                "message": pr.message or "",
                "created_at": pr.created_at.isoformat() + "Z" if pr.created_at else None,
                "project": {"id": project.id, "module": project.module, "title": project.title},
                "proposer": {"id": proposer.id, "username": proposer.username},
                "file_count": len(pr.files or []),
            }
        )
    return jsonify({"items": items})


@projects_bp.route("/api/collab/outbox", methods=["GET"])
@login_required()
def collab_outbox():
    user = g.get("user")
    query = (
        db.session.query(PushRequest, Project, User)
        .join(Project, PushRequest.project_id == Project.id)
        .join(User, Project.owner_id == User.id)
        .filter(PushRequest.proposer_user_id == user.id)
        .order_by(PushRequest.created_at.desc(), PushRequest.id.desc())
        .limit(200)
    )
    items = []
    for pr, project, owner in query.all():
        items.append(
            {
                "id": pr.id,
                "status": pr.status,
                "message": pr.message or "",
                "created_at": pr.created_at.isoformat() + "Z" if pr.created_at else None,
                "project": {
                    "id": project.id,
                    "module": project.module,
                    "title": project.title,
                    "owner_username": owner.username,
                },
                "file_count": len(pr.files or []),
            }
        )
    return jsonify({"items": items})


@projects_bp.route("/api/push-requests/<int:push_request_id>/cancel", methods=["POST"])
@login_required()
def cancel_push_request(push_request_id: int):
    user = g.get("user")
    pr = PushRequest.query.get_or_404(push_request_id)
    if pr.proposer_user_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    if pr.status != "pending":
        return jsonify({"error": "push request closed"}), 400
    pr.status = "cancelled"
    pr.decided_at = datetime.utcnow()
    pr.decided_by_user_id = user.id
    db.session.commit()
    return jsonify({"cancelled": True})


@projects_bp.route("/api/push-requests/<int:push_request_id>/reject", methods=["POST"])
@login_required()
def reject_push_request(push_request_id: int):
    user = g.get("user")
    pr = PushRequest.query.get_or_404(push_request_id)
    project = pr.project
    if not project or project.owner_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    if pr.status != "pending":
        return jsonify({"error": "push request closed"}), 400
    pr.status = "rejected"
    pr.decided_at = datetime.utcnow()
    pr.decided_by_user_id = user.id
    _activity_record(
        type_="push_reject",
        project=project,
        actor_user_id=user.id,
        initiator_user_id=pr.proposer_user_id,
        meta={"push_request_id": pr.id},
    )
    db.session.commit()
    return jsonify({"rejected": True})


@projects_bp.route("/api/push-requests/<int:push_request_id>/approve", methods=["POST"])
@login_required()
def approve_push_request(push_request_id: int):
    user = g.get("user")
    pr = PushRequest.query.get_or_404(push_request_id)
    project = pr.project
    if not project or project.owner_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    if pr.status != "pending":
        return jsonify({"error": "push request closed"}), 400

    request_files = sorted(list(pr.files or []), key=lambda f: int(f.id or 0))
    # If the proposer uploaded the same path multiple times, keep the latest one.
    latest_by_path: dict[str, PushRequestFile] = {}
    for rf in request_files:
        path = str(getattr(rf, "path", "") or "").strip()
        if not path:
            continue
        latest_by_path[path] = rf
    request_files = sorted(list(latest_by_path.values()), key=lambda f: int(f.id or 0))
    copied = []
    replaced = []
    try:
        for rf in request_files:
            existing = ProjectFile.query.filter_by(project_id=project.id, path=rf.path).first()
            if existing and rf.sha256 and existing.sha256 and rf.sha256 == existing.sha256:
                # No-op: don't churn OSS objects for identical content.
                continue

            ext = Path(rf.path).suffix.lower()
            file_uuid = new_uuid()
            target_key = project_object_key(project.uuid, f"objects/{file_uuid}{ext}")
            copy_object(rf.oss_key, target_key)
            copied.append(target_key)

            if existing:
                replaced.append(existing.oss_key)
                existing.oss_key = target_key
                existing.content_type = rf.content_type or ""
                existing.media_type = _effective_media_type(rf.media_type, rf.path, rf.content_type or "")
                existing.size_bytes = int(rf.size_bytes or 0)
                if rf.sha256:
                    existing.sha256 = rf.sha256
                existing.updated_by_id = user.id
            else:
                db.session.add(
                    ProjectFile(
                        project_id=project.id,
                        path=rf.path,
                        oss_key=target_key,
                        content_type=rf.content_type or "",
                        media_type=_effective_media_type(rf.media_type, rf.path, rf.content_type or ""),
                        size_bytes=int(rf.size_bytes or 0),
                        sha256=rf.sha256 or "",
                        created_by_id=user.id,
                        updated_by_id=user.id,
                    )
                )

        project.updated_at = datetime.utcnow()
        pr.status = "approved"
        pr.decided_at = datetime.utcnow()
        pr.decided_by_user_id = user.id

        # Credit the owner as if they pushed, but keep the proposer as initiator.
        _activity_record(
            type_="push_approve",
            project=project,
            actor_user_id=user.id,
            initiator_user_id=pr.proposer_user_id,
            meta={"push_request_id": pr.id, "files": [rf.path for rf in request_files]},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        for key in copied:
            try:
                delete_object(key)
            except Exception:
                current_app.logger.exception("Failed to rollback approved object %s", key)
        current_app.logger.exception("Failed to approve push request %s", pr.id)
        return jsonify({"error": "approve failed"}), 500

    # Cleanup replaced objects best-effort.
    for key in replaced:
        if not key:
            continue
        try:
            delete_object(key)
        except Exception:
            current_app.logger.exception("Failed to delete replaced object %s", key)
    return jsonify({"approved": True, "project_id": project.id})
