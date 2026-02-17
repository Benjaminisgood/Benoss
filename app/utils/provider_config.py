from __future__ import annotations

import re

from .runtime_settings import get_setting_str


_PROVIDER_ALIASES = {
    "open_ai": "openai",
    "open-ai": "openai",
    "chat_anywhere": "chatanywhere",
    "chat-anywhere": "chatanywhere",
    "dashscope": "aliyun",
}
_PROVIDER_KEY_PREFIXES = {
    "openai": "OPENAI",
    "chatanywhere": "CHAT_ANYWHERE",
    "deepseek": "DEEPSEEK",
    "aliyun": "ALIYUN_AI",
}
_PROVIDER_BASE_URL_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "chatanywhere": "https://api.chatanywhere.tech/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "aliyun": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
_MODEL_KEY_SUFFIX = {
    "chat": "CHAT_MODEL",
    "embedding": "EMBEDDING_MODEL",
    "tts": "TTS_MODEL",
    "image": "IMAGE_MODEL",
    "transcribe": "TRANSCRIBE_MODEL",
}
_MODEL_DEFAULTS = {
    "chat": {
        "openai": "gpt-4o-mini",
        "chatanywhere": "gpt-4o-mini",
        "deepseek": "deepseek-chat",
        "aliyun": "qwen-plus",
    },
    "embedding": {
        "openai": "text-embedding-3-small",
        "chatanywhere": "text-embedding-3-small",
        "deepseek": "unsupported",
        "aliyun": "text-embedding-v3",
    },
    "tts": {
        "openai": "gpt-4o-mini-tts",
        "chatanywhere": "gpt-4o-mini-tts",
        "deepseek": "unsupported",
        "aliyun": "qwen3-tts-instruct-flash",
    },
    "image": {
        "openai": "gpt-image-1",
        "chatanywhere": "gpt-image-1",
        "deepseek": "unsupported",
        "aliyun": "qwen-image-max",
    },
    "transcribe": {
        "openai": "whisper-1",
        "chatanywhere": "whisper-1",
        "deepseek": "unsupported",
        "aliyun": "whisper-1",
    },
}
_NON_ALNUM_UPPER = re.compile(r"[^A-Z0-9]+")


def normalize_provider(value: str) -> str:
    raw = str(value or "").strip().lower()
    return _PROVIDER_ALIASES.get(raw, raw)


def provider_key_prefix(provider: str) -> str:
    normalized = normalize_provider(provider)
    if not normalized:
        return ""
    mapped = _PROVIDER_KEY_PREFIXES.get(normalized)
    if mapped:
        return mapped
    return _NON_ALNUM_UPPER.sub("_", normalized.upper()).strip("_")


def provider_api_setting_key(provider: str, key: str) -> str:
    prefix = provider_key_prefix(provider)
    suffix = {
        "api_key": "API_KEY",
        "api_base_url": "API_BASE_URL",
    }.get(str(key or "").strip().lower(), "")
    if not prefix or not suffix:
        return ""
    return f"{prefix}_{suffix}"


def provider_model_setting(provider: str, capability: str) -> tuple[str, str]:
    prefix = provider_key_prefix(provider)
    kind = str(capability or "").strip().lower()
    suffix = _MODEL_KEY_SUFFIX.get(kind, "")
    if not prefix or not suffix:
        return "", ""
    key = f"{prefix}_{suffix}"
    default = str((_MODEL_DEFAULTS.get(kind) or {}).get(normalize_provider(provider), ""))
    return key, default


def provider_model(provider: str, capability: str) -> str:
    key, default = provider_model_setting(provider, capability)
    if not key:
        return ""
    return get_setting_str(key, default=default).strip()


def provider_connection_settings(provider: str) -> dict | None:
    normalized = normalize_provider(provider)
    if not normalized:
        return None

    api_key_key = provider_api_setting_key(normalized, "api_key")
    base_url_key = provider_api_setting_key(normalized, "api_base_url")
    if not api_key_key or not base_url_key:
        return None

    default_base_url = str(_PROVIDER_BASE_URL_DEFAULTS.get(normalized) or "")
    api_key = get_setting_str(api_key_key, default="").strip()
    base_url = get_setting_str(base_url_key, default=default_base_url).strip().rstrip("/")
    if not api_key or not base_url:
        return None
    return {
        "provider": normalized,
        "api_key": api_key,
        "base_url": base_url,
        "api_key_setting_key": api_key_key,
        "base_url_setting_key": base_url_key,
    }
