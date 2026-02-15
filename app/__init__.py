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


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    register_blueprints(app)

    with app.app_context():
        db.create_all()
        _ensure_schema_shape()
        _seed_admin()

    @app.before_request
    def _attach_user():
        load_current_user()

    @app.context_processor
    def _inject_user():
        return {"current_user": getattr(g, "user", None)}

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.cli.command("init-db")
    def init_db_command():
        with app.app_context():
            db.drop_all()
            db.create_all()
            _seed_admin()

    return app


def _seed_admin() -> None:
    username = (os.environ.get("ADMIN_USERNAME") or "").strip()
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        return
    existing = User.query.filter_by(username=username).first()
    if existing:
        if existing.role != "admin":
            existing.role = "admin"
            db.session.commit()
        return

    admin = User(username=username, role="admin", is_active=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()


def _ensure_schema_shape() -> None:
    """Reset old incompatible schemas from pre-refactor versions.

    This project intentionally rebuilt table shapes. If legacy tables exist,
    SQLAlchemy `create_all` will not alter them. We detect the mismatch and
    rebuild once so the new app can run immediately.
    """

    required: dict[str, set[str]] = {
        "user": {"id", "username", "password_hash", "role", "is_active"},
        "content": {"id", "kind", "text_content", "oss_key"},
        "record": {"id", "user_id", "content_id", "visibility", "tags_json"},
        "comment": {"id", "record_id", "user_id", "body"},
        "generated_asset": {"id", "user_id", "kind", "content_type", "oss_key"},
    }
    forbidden_tables = {"project"}
    forbidden_columns: dict[str, set[str]] = {
        "record": {"project_id"},
    }

    inspector = inspect(db.engine)
    all_tables = set(inspector.get_table_names())
    legacy_tables = all_tables.intersection(forbidden_tables)
    if legacy_tables:
        # Legacy tables are no longer in SQLAlchemy metadata; drop them manually.
        with db.engine.begin() as conn:
            for table in legacy_tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
        inspector = inspect(db.engine)

    def _has_shape() -> bool:
        all_tables_local = set(inspector.get_table_names())

        for table, columns in required.items():
            if table not in all_tables_local:
                return False
            existing = {col["name"] for col in inspector.get_columns(table)}
            if not columns.issubset(existing):
                return False
            blocked = forbidden_columns.get(table) or set()
            if existing.intersection(blocked):
                return False
        return True

    if _has_shape():
        return

    db.drop_all()
    db.create_all()
