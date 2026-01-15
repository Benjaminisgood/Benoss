from functools import wraps

from flask import current_app, g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..models import User


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def generate_token(user: User) -> str:
    serializer = _serializer()
    return serializer.dumps({"id": user.id, "role": user.role})


def verify_token(token: str, max_age: int = 86400) -> User:
    serializer = _serializer()
    data = serializer.loads(token, max_age=max_age)
    return User.query.get(data["id"])


def require_role(role: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = getattr(g, "user", None)
            if user and user.is_active:
                if not role or user.role == role:
                    return fn(*args, **kwargs)
                return jsonify({"error": "forbidden"}), 403

            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "missing token"}), 401
            token = auth.split(" ", 1)[1].strip()
            try:
                user = verify_token(token)
            except (BadSignature, SignatureExpired):
                return jsonify({"error": "invalid token"}), 401
            if user is None or not user.is_active:
                return jsonify({"error": "invalid user"}), 401
            if role and user.role != role:
                return jsonify({"error": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
