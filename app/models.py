from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .utils.ids import new_uuid


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class User(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(32), default="user")
    is_active = db.Column(db.Boolean, default=True)
    description = db.Column(db.Text, default="")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class QuickLink(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    url = db.Column(db.String(512), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)


class Project(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(64), unique=True, nullable=False, default=new_uuid)
    module = db.Column(db.String(16), nullable=False)  # blog | note

    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    owner = db.relationship("User", backref=db.backref("projects", lazy=True))

    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    visibility = db.Column(db.String(16), default="private")  # public | private
    is_archived = db.Column(db.Boolean, default=False)

    cloned_from_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    cloned_from = db.relationship("Project", remote_side=[id], backref=db.backref("clones", lazy=True))


class ProjectFile(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    project = db.relationship("Project", backref=db.backref("files", lazy=True, cascade="all, delete-orphan"))

    path = db.Column(db.String(512), nullable=False)
    oss_key = db.Column(db.String(512), nullable=False)
    content_type = db.Column(db.String(255), default="")
    media_type = db.Column(db.String(16), default="file")  # image | video | audio | pdf | text | file
    size_bytes = db.Column(db.Integer, default=0)
    sha256 = db.Column(db.String(64), default="")

    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    __table_args__ = (db.UniqueConstraint("project_id", "path", name="uq_project_file_path"),)


class ProjectActivity(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(32), nullable=False)  # git | clone | push | push_request | push_approve | ...
    module = db.Column(db.String(16), nullable=False)  # blog | note

    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    project = db.relationship("Project", backref=db.backref("activity", lazy=True, cascade="all, delete-orphan"))

    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    actor_user = db.relationship("User", foreign_keys=[actor_user_id])

    initiator_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    initiator_user = db.relationship("User", foreign_keys=[initiator_user_id])

    meta_json = db.Column(db.Text, default="{}")


class PushRequest(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)
    project = db.relationship("Project", backref=db.backref("push_requests", lazy=True, cascade="all, delete-orphan"))

    proposer_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    proposer_user = db.relationship("User", foreign_keys=[proposer_user_id])

    status = db.Column(db.String(16), default="pending")  # pending | approved | rejected | cancelled
    message = db.Column(db.Text, default="")

    decided_at = db.Column(db.DateTime, nullable=True)
    decided_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    decided_by_user = db.relationship("User", foreign_keys=[decided_by_user_id])


class PushRequestFile(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    push_request_id = db.Column(db.Integer, db.ForeignKey("push_request.id"), nullable=False, index=True)
    push_request = db.relationship(
        "PushRequest",
        backref=db.backref("files", lazy=True, cascade="all, delete-orphan"),
    )

    path = db.Column(db.String(512), nullable=False)
    oss_key = db.Column(db.String(512), nullable=False)
    content_type = db.Column(db.String(255), default="")
    media_type = db.Column(db.String(16), default="file")
    size_bytes = db.Column(db.Integer, default=0)
    sha256 = db.Column(db.String(64), default="")


class WhiteboardCard(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    board_date = db.Column(db.String(16), nullable=False, index=True)  # YYYY-MM-DD
    x = db.Column(db.Float, default=20.0)
    y = db.Column(db.Float, default=20.0)
    text = db.Column(db.Text, default="")
    version = db.Column(db.Integer, nullable=False, default=1)
    idempotency_key = db.Column(db.String(96), nullable=True, index=True)
    entry_date = db.Column(db.String(16), nullable=False, default="")
    entry_tags_json = db.Column(db.Text, default="[]")
    entry_mood = db.Column(db.String(24), default="")
    entry_type = db.Column(db.String(24), default="note")
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    attachments = db.relationship(
        "WhiteboardAttachment",
        backref=db.backref("card", lazy=True),
        lazy=True,
        cascade="all, delete-orphan",
    )


class WhiteboardAttachment(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("whiteboard_card.id"), nullable=False, index=True)
    oss_key = db.Column(db.String(512), nullable=False)
    filename = db.Column(db.String(255), default="")
    content_type = db.Column(db.String(255), default="")
    media_type = db.Column(db.String(16), default="file")  # image | video | audio | pdf | file
    size_bytes = db.Column(db.Integer, default=0)
    sha256 = db.Column(db.String(64), default="")


class WhiteboardEvent(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    board_date = db.Column(db.String(16), nullable=False, index=True)
    event_type = db.Column(db.String(16), nullable=False)  # create | update | delete | reset
    card_id = db.Column(db.Integer, nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    actor_user = db.relationship("User", foreign_keys=[actor_user_id])
    payload_json = db.Column(db.Text, default="{}")


class WhiteboardLink(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    board_date = db.Column(db.String(16), nullable=False, index=True)  # YYYY-MM-DD
    from_card_id = db.Column(db.Integer, db.ForeignKey("whiteboard_card.id"), nullable=False, index=True)
    to_card_id = db.Column(db.Integer, db.ForeignKey("whiteboard_card.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (db.UniqueConstraint("board_date", "from_card_id", "to_card_id", name="uq_whiteboard_link"),)
