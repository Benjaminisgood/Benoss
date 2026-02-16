import json
from datetime import datetime

from sqlalchemy import Index
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class User(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(32), nullable=False, default="user")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    description = db.Column(db.Text, nullable=False, default="")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class AppSetting(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(128), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=False, default="")


class Content(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)

    kind = db.Column(db.String(16), nullable=False)  # text | file
    text_content = db.Column(db.Text, nullable=False, default="")

    oss_key = db.Column(db.String(512), nullable=False, default="")
    filename = db.Column(db.String(255), nullable=False, default="")
    content_type = db.Column(db.String(255), nullable=False, default="")
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    sha256 = db.Column(db.String(64), nullable=False, default="")


class Record(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user = db.relationship("User", backref=db.backref("records", lazy=True))

    content_id = db.Column(db.Integer, db.ForeignKey("content.id"), nullable=False, unique=True, index=True)
    content = db.relationship("Content", backref=db.backref("record", uselist=False))

    format = db.Column(db.String(32), nullable=False, default="text")
    visibility = db.Column(db.String(16), nullable=False, default="private")  # public | private
    tags_json = db.Column(db.Text, nullable=False, default="[]")
    preview = db.Column(db.Text, nullable=False, default="")

    def get_tags(self) -> list[str]:
        try:
            raw = json.loads(self.tags_json or "[]")
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        output: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if text:
                output.append(text)
        return output

    def set_tags(self, tags: list[str]) -> None:
        cleaned: list[str] = []
        seen = set()
        for item in tags:
            text = str(item or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            cleaned.append(text)
        self.tags_json = json.dumps(cleaned, ensure_ascii=False)


class Comment(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)

    record_id = db.Column(db.Integer, db.ForeignKey("record.id"), nullable=False, index=True)
    record = db.relationship("Record", backref=db.backref("comments", lazy=True, cascade="all, delete-orphan"))

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user = db.relationship("User", backref=db.backref("comments", lazy=True))

    body = db.Column(db.Text, nullable=False)


class GeneratedAsset(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user = db.relationship("User", backref=db.backref("generated_assets", lazy=True))

    kind = db.Column(db.String(32), nullable=False)  # blog_html | podcast_audio | poster_image
    title = db.Column(db.String(255), nullable=False, default="")
    provider = db.Column(db.String(64), nullable=False, default="")
    model = db.Column(db.String(128), nullable=False, default="")
    visibility = db.Column(db.String(16), nullable=False, default="private")  # public | private
    status = db.Column(db.String(16), nullable=False, default="ready")
    is_daily_digest = db.Column(db.Boolean, nullable=False, default=False)
    source_day = db.Column(db.Date, nullable=True, index=True)

    content_type = db.Column(db.String(255), nullable=False, default="")
    ext = db.Column(db.String(16), nullable=False, default="")
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    oss_key = db.Column(db.String(512), nullable=False, default="")
    sha256 = db.Column(db.String(64), nullable=False, default="")

    source_filters_json = db.Column(db.Text, nullable=False, default="{}")


class DailyDigestJob(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)

    day = db.Column(db.Date, nullable=False, index=True)
    timezone = db.Column(db.String(64), nullable=False, default="Asia/Shanghai")
    status = db.Column(db.String(16), nullable=False, default="running")  # running | ready | partial | failed

    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)
    error = db.Column(db.Text, nullable=False, default="")

    blog_asset_id = db.Column(db.Integer, db.ForeignKey("generated_asset.id"), nullable=True)
    podcast_asset_id = db.Column(db.Integer, db.ForeignKey("generated_asset.id"), nullable=True)
    poster_asset_id = db.Column(db.Integer, db.ForeignKey("generated_asset.id"), nullable=True)

    blog_asset = db.relationship("GeneratedAsset", foreign_keys=[blog_asset_id], lazy="joined")
    podcast_asset = db.relationship("GeneratedAsset", foreign_keys=[podcast_asset_id], lazy="joined")
    poster_asset = db.relationship("GeneratedAsset", foreign_keys=[poster_asset_id], lazy="joined")


Index("ix_record_user_created", Record.user_id, Record.created_at)
Index("ix_record_visibility_created", Record.visibility, Record.created_at)
Index("ix_comment_record_created", Comment.record_id, Comment.created_at)
Index("ix_generated_asset_user_created", GeneratedAsset.user_id, GeneratedAsset.created_at)
Index("ix_generated_asset_public_day_created", GeneratedAsset.visibility, GeneratedAsset.source_day, GeneratedAsset.created_at)
Index("ux_daily_digest_day_tz", DailyDigestJob.day, DailyDigestJob.timezone, unique=True)
