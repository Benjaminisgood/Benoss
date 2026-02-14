import os

from dotenv import load_dotenv
from flask import Flask, g, jsonify
from sqlalchemy import inspect, text

from .config import Config
from .extensions import db
from .models import User
from .routes import register_blueprints
from .utils.session_auth import load_current_user


load_dotenv()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    register_blueprints(app)

    with app.app_context():
        db.create_all()
        _ensure_schema_compat(app)
        _seed_admin()

    @app.before_request
    def _attach_user():
        load_current_user()

    @app.context_processor
    def inject_user():
        return {"current_user": getattr(g, "user", None)}

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.cli.command("init-db")
    def init_db():
        with app.app_context():
            db.create_all()
            _ensure_schema_compat(app)
            _seed_admin()

    return app


def _ensure_schema_compat(app: Flask) -> None:
    # This project keeps schema migration lightweight; add missing columns/indexes in-place.
    insp = inspect(db.engine)
    try:
        columns = {c["name"] for c in insp.get_columns("whiteboard_card")}
    except Exception:
        return

    statements: list[str] = []
    if "version" not in columns:
        statements.append("ALTER TABLE whiteboard_card ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    if "idempotency_key" not in columns:
        statements.append("ALTER TABLE whiteboard_card ADD COLUMN idempotency_key VARCHAR(96)")
    if "entry_date" not in columns:
        statements.append("ALTER TABLE whiteboard_card ADD COLUMN entry_date VARCHAR(16) NOT NULL DEFAULT ''")
    if "entry_tags_json" not in columns:
        statements.append("ALTER TABLE whiteboard_card ADD COLUMN entry_tags_json TEXT NOT NULL DEFAULT '[]'")
    if "entry_mood" not in columns:
        statements.append("ALTER TABLE whiteboard_card ADD COLUMN entry_mood VARCHAR(24) NOT NULL DEFAULT ''")
    if "entry_type" not in columns:
        statements.append("ALTER TABLE whiteboard_card ADD COLUMN entry_type VARCHAR(24) NOT NULL DEFAULT 'note'")

    index_statements = [
        "CREATE INDEX IF NOT EXISTS idx_wb_card_board_updated ON whiteboard_card (board_date, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_wb_card_board_user_idem ON whiteboard_card (board_date, created_by_id, idempotency_key)",
        "CREATE INDEX IF NOT EXISTS idx_wb_event_board_created ON whiteboard_event (board_date, created_at)",
    ]

    try:
        with db.engine.begin() as conn:
            for sql in statements:
                conn.execute(text(sql))
            conn.execute(
                text(
                    "UPDATE whiteboard_card "
                    "SET version = CASE WHEN version IS NULL OR version < 1 THEN 1 ELSE version END"
                )
            )
            conn.execute(
                text(
                    "UPDATE whiteboard_card "
                    "SET entry_date = board_date "
                    "WHERE entry_date IS NULL OR entry_date = ''"
                )
            )
            conn.execute(
                text(
                    "UPDATE whiteboard_card "
                    "SET entry_tags_json = '[]' "
                    "WHERE entry_tags_json IS NULL OR TRIM(entry_tags_json) = ''"
                )
            )
            conn.execute(
                text(
                    "UPDATE whiteboard_card "
                    "SET entry_type = 'note' "
                    "WHERE entry_type IS NULL OR TRIM(entry_type) = ''"
                )
            )
            for sql in index_statements:
                conn.execute(text(sql))
    except Exception:
        app.logger.exception("Failed to run schema compatibility checks")


def _seed_admin():
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        return
    username = username.strip()
    if not username:
        return
    if User.query.filter_by(username=username).first():
        return
    user = User(username=username, role="admin", is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
