import os

from dotenv import load_dotenv
from flask import Flask, g, jsonify

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
            _seed_admin()

    return app


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

