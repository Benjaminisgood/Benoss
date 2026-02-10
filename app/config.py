import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get_env(key, default=None):
    value = os.environ.get(key, default)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    return value


class Config:
    SECRET_KEY = _get_env("SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = _get_env(
        "DATABASE_URL",
        f"sqlite:///{DATA_DIR / 'benoss.sqlite'}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(_get_env("REMEMBER_DAYS", 30)))
    SESSION_COOKIE_SAMESITE = _get_env("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _get_env("SESSION_COOKIE_SECURE", "0") == "1"

    OSS_ENDPOINT = _get_env("ALIYUN_OSS_ENDPOINT")
    OSS_ACCESS_KEY_ID = _get_env("ALIYUN_OSS_ACCESS_KEY_ID")
    OSS_ACCESS_KEY_SECRET = _get_env("ALIYUN_OSS_ACCESS_KEY_SECRET")
    OSS_BUCKET = _get_env("ALIYUN_OSS_BUCKET")
    OSS_PREFIX = _get_env("ALIYUN_OSS_PREFIX", "benoss")
    OSS_PUBLIC_BASE_URL = _get_env("ALIYUN_OSS_PUBLIC_BASE_URL")
    OSS_ASSUME_PUBLIC = _get_env("ALIYUN_OSS_ASSUME_PUBLIC", "0")

    UPLOAD_TMP_DIR = str(DATA_DIR / "uploads")
    MAX_CONTENT_LENGTH = int(_get_env("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))

    # Debounce whiteboard board.json snapshot writes to OSS (seconds).
    WHITEBOARD_SNAPSHOT_DEBOUNCE_SECONDS = float(_get_env("WHITEBOARD_SNAPSHOT_DEBOUNCE_SECONDS", 6.0))
