import os
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import click
from dotenv import load_dotenv
from flask import Flask, g, jsonify
from sqlalchemy import inspect, text

from .config import Config
from .extensions import db
from .models import User
from .routes.api import build_daily_public_digest
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

    @app.cli.command("digest-build")
    @click.option("--day", default="", help="Digest day in YYYY-MM-DD (default: yesterday in DIGEST_TIMEZONE).")
    @click.option("--timezone", "timezone_name", default="", help="IANA timezone, e.g. Asia/Shanghai.")
    @click.option("--force", is_flag=True, help="Regenerate assets even if same-day digest assets exist.")
    def digest_build_command(day: str, timezone_name: str, force: bool):
        with app.app_context():
            tz_name = str(timezone_name or app.config.get("DIGEST_TIMEZONE") or "Asia/Shanghai").strip() or "Asia/Shanghai"
            if day:
                try:
                    day_value = datetime.strptime(day, "%Y-%m-%d").date()
                except ValueError as exc:
                    raise click.ClickException("invalid --day format, expected YYYY-MM-DD") from exc
            else:
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = timezone.utc
                day_value = datetime.now(tz).date() - timedelta(days=1)

            try:
                result = build_daily_public_digest(day_value=day_value, force=bool(force), timezone_name=tz_name)
            except Exception as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(json.dumps(result, ensure_ascii=False))

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

    def _add_generated_asset_columns() -> None:
        local_inspector = inspect(db.engine)
        if "generated_asset" not in set(local_inspector.get_table_names()):
            return

        existing = {col["name"] for col in local_inspector.get_columns("generated_asset")}
        statements: list[str] = []
        if "visibility" not in existing:
            statements.append("ALTER TABLE generated_asset ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'private'")
        if "status" not in existing:
            statements.append("ALTER TABLE generated_asset ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'ready'")
        if "is_daily_digest" not in existing:
            statements.append("ALTER TABLE generated_asset ADD COLUMN is_daily_digest BOOLEAN NOT NULL DEFAULT 0")
        if "source_day" not in existing:
            statements.append("ALTER TABLE generated_asset ADD COLUMN source_day DATE")

        if not statements:
            return

        with db.engine.begin() as conn:
            for sql in statements:
                conn.execute(text(sql))

    def _add_daily_digest_columns() -> None:
        local_inspector = inspect(db.engine)
        if "daily_digest_job" not in set(local_inspector.get_table_names()):
            return

        existing = {col["name"] for col in local_inspector.get_columns("daily_digest_job")}
        with db.engine.begin() as conn:
            if "blog_asset_id" not in existing and "article_asset_id" in existing:
                try:
                    conn.execute(text("ALTER TABLE daily_digest_job RENAME COLUMN article_asset_id TO blog_asset_id"))
                except Exception:
                    conn.execute(text("ALTER TABLE daily_digest_job ADD COLUMN blog_asset_id INTEGER"))
                    conn.execute(text("UPDATE daily_digest_job SET blog_asset_id = article_asset_id WHERE blog_asset_id IS NULL"))

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

    _add_generated_asset_columns()
    _add_daily_digest_columns()

    if _has_shape():
        return

    db.drop_all()
    db.create_all()
