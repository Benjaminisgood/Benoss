from pathlib import Path
from typing import List, Optional

from ..extensions import db
from ..models import EverydayAttachmentIndex
from ..oss import public_url


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
DOC_EXTS = {".pdf"}


def media_type_for_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in DOC_EXTS:
        return "pdf"
    return "file"


def preview_url_for_key(key: str, media_type: str) -> Optional[str]:
    if media_type == "image":
        return public_url(key, params={"x-oss-process": "image/resize,w_480/quality,q_70"})
    if media_type == "video":
        return public_url(key, params={"x-oss-process": "video/snapshot,t_1000,f_jpg,w_480"})
    if media_type == "pdf":
        return public_url(key, params={"x-oss-process": "doc/preview,format=jpg,page=1"})
    return None


def upsert_everyday_attachment(
    uuid: str,
    media_type: str,
    oss_key: str,
    source_id: str,
    commit: bool = True,
) -> EverydayAttachmentIndex:
    record = EverydayAttachmentIndex.query.filter_by(uuid=uuid).first()
    if record is None:
        record = EverydayAttachmentIndex(
            uuid=uuid,
            media_type=media_type,
            oss_key=oss_key,
            source_id=source_id,
        )
        db.session.add(record)
    else:
        record.media_type = media_type
        record.oss_key = oss_key
        record.source_id = source_id
    if commit:
        db.session.commit()
    return record


def delete_everyday_attachment(uuid: str, commit: bool = True) -> bool:
    record = EverydayAttachmentIndex.query.filter_by(uuid=uuid).first()
    if record is None:
        return False
    db.session.delete(record)
    if commit:
        db.session.commit()
    return True


def list_everyday_attachments(
    media_type: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> List[dict]:
    query = EverydayAttachmentIndex.query
    if media_type:
        query = query.filter_by(media_type=media_type)
    records = query.order_by(EverydayAttachmentIndex.created_at.desc()).offset(offset).limit(limit).all()
    items = []
    for record in records:
        resolved_type = record.media_type
        if record.oss_key:
            derived_type = media_type_for_path(record.oss_key)
            if derived_type != "file" or resolved_type == "file":
                resolved_type = derived_type
        items.append(
            {
                "uuid": record.uuid,
                "media_type": resolved_type,
                "oss_key": record.oss_key,
                "url": public_url(record.oss_key),
                "preview_url": preview_url_for_key(record.oss_key, resolved_type),
                "source_module": "everyday",
                "source_id": record.source_id,
            }
        )
    return items
