import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env(key: str, default=None):
    value = os.environ.get(key, default)
    if isinstance(value, str):
        return value.strip()
    return value


class Config:
    SECRET_KEY = _env("SECRET_KEY", "dev-secret-key")

    SQLALCHEMY_DATABASE_URI = _env(
        "DATABASE_URL",
        f"sqlite:///{DATA_DIR / 'benoss.sqlite'}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    PERMANENT_SESSION_LIFETIME = timedelta(days=int(_env("REMEMBER_DAYS", 30)))
    SESSION_COOKIE_SAMESITE = _env("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _env("SESSION_COOKIE_SECURE", "0") == "1"

    OSS_ENDPOINT = _env("ALIYUN_OSS_ENDPOINT")
    OSS_ACCESS_KEY_ID = _env("ALIYUN_OSS_ACCESS_KEY_ID")
    OSS_ACCESS_KEY_SECRET = _env("ALIYUN_OSS_ACCESS_KEY_SECRET")
    OSS_BUCKET = _env("ALIYUN_OSS_BUCKET")
    OSS_PREFIX = _env("ALIYUN_OSS_PREFIX", "benoss")
    OSS_PUBLIC_BASE_URL = _env("ALIYUN_OSS_PUBLIC_BASE_URL")
    OSS_ASSUME_PUBLIC = _env("ALIYUN_OSS_ASSUME_PUBLIC", "0")

    OSS_LOCAL_DIR = str(DATA_DIR / "oss-local")
    UPLOAD_TMP_DIR = str(DATA_DIR / "uploads")

    MAX_CONTENT_LENGTH = int(_env("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))

    BOARD_DEFAULT_DAYS = int(_env("BOARD_DEFAULT_DAYS", 7))

    AI_AUTOFILL_PROVIDER = _env("AI_AUTOFILL_PROVIDER", "")
    AI_REQUEST_TIMEOUT_SECONDS = int(_env("AI_REQUEST_TIMEOUT_SECONDS", 45))
    AI_MAX_NOTICE_RECORDS = int(_env("AI_MAX_NOTICE_RECORDS", 180))
    AI_IMAGE_MODEL = _env("AI_IMAGE_MODEL", "gpt-image-1")
    AI_TTS_MODEL = _env("AI_TTS_MODEL", "gpt-4o-mini-tts")
    AI_TTS_VOICE = _env("AI_TTS_VOICE", "alloy")
    AI_TTS_RESPONSE_FORMAT = _env("AI_TTS_RESPONSE_FORMAT", "mp3")
    AI_TTS_MAX_INPUT_CHARS = int(_env("AI_TTS_MAX_INPUT_CHARS", 3600))
    PODCAST_DEFAULT_STYLE = _env("PODCAST_DEFAULT_STYLE", "dialogue")

    LOCAL_DAILY_ARCHIVE_DIR = _env("LOCAL_DAILY_ARCHIVE_DIR", str(DATA_DIR / "daily-archive"))
    LOCAL_VECTOR_STORE_DIR = _env("LOCAL_VECTOR_STORE_DIR", str(DATA_DIR / "vector-store"))
    VECTOR_AUTO_REBUILD = _env("VECTOR_AUTO_REBUILD", "1")
    VECTOR_TOP_K = int(_env("VECTOR_TOP_K", 6))
    VECTOR_MAX_DOCS = int(_env("VECTOR_MAX_DOCS", 4000))
    HOME_AUTO_BUILD_DAILY_ASSETS = _env("HOME_AUTO_BUILD_DAILY_ASSETS", "1")
    HOME_DIGEST_RETRY_MINUTES = int(_env("HOME_DIGEST_RETRY_MINUTES", 30))
    DIGEST_TIMEZONE = _env("DIGEST_TIMEZONE", "Asia/Shanghai")

    CHAT_ANYWHERE_API_KEY = _env("CHAT_ANYWHERE_API_KEY")
    CHAT_ANYWHERE_API_BASE_URL = _env("CHAT_ANYWHERE_API_BASE_URL", "https://api.chatanywhere.tech/v1")
    CHAT_ANYWHERE_MODEL = _env("CHAT_ANYWHERE_MODEL", "gpt-4o-mini")

    DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
    DEEPSEEK_API_BASE_URL = _env("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")

    ALIYUN_AI_API_KEY = _env("ALIYUN_AI_API_KEY")
    ALIYUN_AI_API_BASE_URL = _env("ALIYUN_AI_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    ALIYUN_AI_MODEL = _env("ALIYUN_AI_MODEL", "qwen-plus")
