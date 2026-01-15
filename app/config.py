import os
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

    OSS_ENDPOINT = _get_env("ALIYUN_OSS_ENDPOINT")
    OSS_ACCESS_KEY_ID = _get_env("ALIYUN_OSS_ACCESS_KEY_ID")
    OSS_ACCESS_KEY_SECRET = _get_env("ALIYUN_OSS_ACCESS_KEY_SECRET")
    OSS_BUCKET = _get_env("ALIYUN_OSS_BUCKET")
    OSS_PREFIX = _get_env("ALIYUN_OSS_PREFIX", "benoss")
    OSS_PUBLIC_BASE_URL = _get_env("ALIYUN_OSS_PUBLIC_BASE_URL")
    OSS_ASSUME_PUBLIC = _get_env("ALIYUN_OSS_ASSUME_PUBLIC", "0")

    UPLOAD_TMP_DIR = str(DATA_DIR / "uploads")
    MAX_CONTENT_LENGTH = int(_get_env("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))

    REEL_RENDERER = _get_env("REEL_RENDERER", "manifest")
    REEL_DEFAULT_IMAGE_DURATION = float(_get_env("REEL_DEFAULT_IMAGE_DURATION", 2.5))

    EVERYDAY_REINDEX_STATE_FILE = _get_env(
        "EVERYDAY_REINDEX_STATE_FILE",
        str(DATA_DIR / "everyday_reindex_state.json"),
    )
