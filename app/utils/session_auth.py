from functools import wraps
from urllib.parse import urlparse

from flask import g, jsonify, redirect, request, session, url_for

from ..models import User


def load_current_user():
    user_id = session.get("user_id")
    if not user_id:
        g.user = None
        return None
    user = User.query.get(user_id)
    g.user = user
    return user


def login_user(user: User) -> None:
    session["user_id"] = user.id
    session["role"] = user.role
    session["username"] = user.username


def logout_user() -> None:
    session.clear()


def safe_next_url(value: str) -> str:
    if not value:
        return url_for("site.home")
    parsed = urlparse(value)
    if parsed.netloc or parsed.scheme:
        return url_for("site.home")
    return value


def login_required(role: str = None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = g.get("user") if hasattr(g, "user") else load_current_user()
            if not user:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "login required"}), 401
                next_url = safe_next_url(request.full_path.rstrip("?"))
                return redirect(url_for("site.login", next=next_url))
            if role and user.role != role:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "forbidden"}), 403
                return redirect(url_for("site.login"))
            return fn(*args, **kwargs)

        return wrapper

    return decorator
