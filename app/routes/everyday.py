import os
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

from ..oss import object_exists, public_url, put_object_from_file, sign_put_url
from ..services.album_service import media_type_for_path, upsert_everyday_attachment
from ..services.everyday_service import (
    add_attachment,
    get_day_entry,
    list_month,
    upsert_day_text,
)
from ..utils.ids import new_uuid
from ..utils.oss_paths import everyday_media_key, everyday_prefix, join
from ..utils.session_auth import login_required


everyday_bp = Blueprint("everyday", __name__)


def _validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("invalid date") from exc
    return date_str


def _enrich_entry(entry: dict) -> dict:
    if not entry:
        return entry
    attachments = []
    for item in entry.get("attachments", []):
        enriched = dict(item)
        if item.get("oss_key") and not item.get("url"):
            enriched["url"] = public_url(item["oss_key"])
        attachments.append(enriched)
    enriched_entry = dict(entry)
    enriched_entry["attachments"] = attachments
    enriched_entry["reel"] = _build_reel_payload(enriched_entry)
    return enriched_entry


def _build_reel_payload(entry: dict) -> dict:
    attachments = entry.get("attachments", []) if entry else []
    media = []
    audio = None
    captions = []
    for item in attachments:
        media_type = item.get("media_type")
        if media_type == "audio" and not audio and item.get("url"):
            audio = dict(item)
        if media_type in {"image", "video"} and item.get("url"):
            media_item = dict(item)
            media.append(media_item)
            caption = item.get("caption")
            if caption:
                captions.append(caption)
    return {
        "text": entry.get("text", "") if entry else "",
        "media": media,
        "audio": audio,
        "captions": captions,
    }


@everyday_bp.route("/api/everyday/day", methods=["GET"])
@login_required()
def get_day():
    date_str = request.args.get("date", "").strip()
    if not date_str:
        return jsonify({"error": "missing date"}), 400
    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    entry = _enrich_entry(get_day_entry(date_str))
    return jsonify({"date": date_str, "entry": entry})


@everyday_bp.route("/api/everyday/month", methods=["GET"])
@login_required()
def get_month():
    month = request.args.get("month", "").strip()
    if not month:
        return jsonify({"error": "missing month"}), 400
    data = list_month(month)
    return jsonify(data)


@everyday_bp.route("/api/everyday/text", methods=["POST"])
@login_required(role="admin")
def update_text():
    payload = request.get_json(silent=True) or {}
    date_str = str(payload.get("date", "")).strip()
    text = payload.get("text", "")

    if not date_str:
        return jsonify({"error": "missing date"}), 400

    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    upsert_day_text(date_str, text)
    entry = _enrich_entry(get_day_entry(date_str))
    return jsonify({"date": date_str, "entry": entry})


@everyday_bp.route("/api/everyday/upload", methods=["POST"])
@login_required(role="admin")
def upload_media():
    date_str = request.form.get("date", "").strip()
    caption = request.form.get("caption", "")
    if not date_str:
        return jsonify({"error": "missing date"}), 400

    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400

    file_obj = request.files["file"]
    if not file_obj or not file_obj.filename:
        return jsonify({"error": "missing filename"}), 400

    original_name = secure_filename(file_obj.filename)
    ext = os.path.splitext(original_name)[1].lower()
    uuid = new_uuid()
    oss_key = everyday_media_key(date_str, uuid, ext)

    tmp_dir = Path(current_app.config["UPLOAD_TMP_DIR"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid}{ext}"
    file_obj.save(tmp_path)

    put_object_from_file(oss_key, str(tmp_path), content_type=file_obj.mimetype or None)
    tmp_path.unlink(missing_ok=True)

    media_type = media_type_for_path(oss_key)
    attachment = {
        "uuid": uuid,
        "oss_key": oss_key,
        "media_type": media_type,
        "caption": caption,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

    add_attachment(date_str, attachment)
    entry = _enrich_entry(get_day_entry(date_str))

    upsert_everyday_attachment(uuid, media_type, oss_key, date_str)

    return jsonify(
        {
            "uuid": uuid,
            "oss_key": oss_key,
            "url": public_url(oss_key),
            "media_type": media_type,
            "entry": entry,
        }
    )


@everyday_bp.route("/api/everyday/upload/presign", methods=["POST"])
@login_required(role="admin")
def presign_upload():
    payload = request.get_json(silent=True) or {}
    date_str = str(payload.get("date", "")).strip()
    filename = str(payload.get("filename", "")).strip()
    content_type = str(payload.get("content_type", "")).strip() or "application/octet-stream"

    if not date_str or not filename:
        return jsonify({"error": "missing fields"}), 400

    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    ext = os.path.splitext(filename)[1].lower()
    uuid = new_uuid()
    oss_key = everyday_media_key(date_str, uuid, ext)
    headers = {"Content-Type": content_type}
    upload_url = sign_put_url(oss_key, expires=900, headers=headers)
    media_type = media_type_for_path(oss_key)

    return jsonify(
        {
            "uuid": uuid,
            "oss_key": oss_key,
            "upload_url": upload_url,
            "headers": headers,
            "expires_in": 900,
            "media_type": media_type,
        }
    )


@everyday_bp.route("/api/everyday/upload/commit", methods=["POST"])
@login_required(role="admin")
def commit_upload():
    payload = request.get_json(silent=True) or {}
    date_str = str(payload.get("date", "")).strip()
    uuid = str(payload.get("uuid", "")).strip()
    oss_key = str(payload.get("oss_key", "")).strip()
    caption = str(payload.get("caption", "")).strip()
    media_type = str(payload.get("media_type", "")).strip()

    if not date_str or not uuid or not oss_key:
        return jsonify({"error": "missing fields"}), 400

    try:
        date_str = _validate_date(date_str)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    year, month, _ = date_str.split("-")
    expected_prefix = join(everyday_prefix(), year, month)
    if not oss_key.startswith(f"{expected_prefix}/"):
        return jsonify({"error": "invalid oss key"}), 400

    if not os.path.splitext(oss_key)[0].endswith(f"/{uuid}"):
        return jsonify({"error": "uuid mismatch"}), 400

    if not object_exists(oss_key):
        return jsonify({"error": "object missing"}), 400

    if not media_type:
        media_type = media_type_for_path(oss_key)

    attachment = {
        "uuid": uuid,
        "oss_key": oss_key,
        "media_type": media_type,
        "caption": caption,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

    add_attachment(date_str, attachment)
    entry = _enrich_entry(get_day_entry(date_str))

    upsert_everyday_attachment(uuid, media_type, oss_key, date_str)

    return jsonify(
        {
            "uuid": uuid,
            "oss_key": oss_key,
            "url": public_url(oss_key),
            "media_type": media_type,
            "entry": entry,
        }
    )
