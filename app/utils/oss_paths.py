import posixpath
from pathlib import PurePosixPath

from flask import current_app

from ..oss import object_exists

ATTACHMENT_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".pdf",
)


def join(*parts: str) -> str:
    cleaned = [p.strip("/") for p in parts if p]
    return "/".join(cleaned)


def module_prefix(module: str) -> str:
    base = current_app.config.get("OSS_PREFIX", "benoss")
    return join(base, module)


def blog_prefix() -> str:
    return module_prefix("blog")


def note_prefix() -> str:
    return module_prefix("note")


def everyday_prefix() -> str:
    return module_prefix("everyday")


def ensure_relative_key(rel_key: str) -> str:
    rel_key = rel_key.strip().lstrip("/")
    if not rel_key or ".." in PurePosixPath(rel_key).parts:
        raise ValueError("invalid key")
    return rel_key


def resolve_module_key(module: str, rel_key: str) -> str:
    rel_key = ensure_relative_key(rel_key)
    return join(module_prefix(module), rel_key)


def resolve_attachment_key(module: str, base_rel_key: str, ref: str, check_exists: bool = False) -> str:
    ref = ref.strip().strip("\"").strip("'")
    ref = ref.split("?")[0].split("#")[0]
    ref = ref.split("|")[0]
    if not ref:
        raise ValueError("empty attachment ref")

    if ref.startswith("/"):
        rel_path = ref.lstrip("/")
    else:
        if "/" not in ref and "\\" not in ref:
            rel_path = ref
        else:
            base_dir = posixpath.dirname(base_rel_key)
            rel_path = posixpath.normpath(posixpath.join(base_dir, ref))

    rel_path = ensure_relative_key(rel_path)
    key = join(module_prefix(module), rel_path)
    if PurePosixPath(ref).suffix:
        if check_exists:
            try:
                exists = object_exists(key)
            except Exception:
                return key
            if not exists:
                raise ValueError("attachment not found")
        return key

    try:
        if object_exists(key):
            return key
    except Exception:
        return key

    for ext in ATTACHMENT_EXTS:
        candidate_key = join(module_prefix(module), f"{rel_path}{ext}")
        try:
            if object_exists(candidate_key):
                return candidate_key
        except Exception:
            return key
    raise ValueError("attachment not found")


def attachment_key_for_ref(module: str, base_rel_key: str, ref: str) -> str:
    ref = ref.strip()
    if not ref:
        raise ValueError("empty attachment ref")

    if ref.startswith("/"):
        rel_path = ref.lstrip("/")
    else:
        if "/" not in ref and "\\" not in ref:
            rel_path = ref
        else:
            base_dir = posixpath.dirname(base_rel_key)
            rel_path = posixpath.normpath(posixpath.join(base_dir, ref))

    rel_path = ensure_relative_key(rel_path)
    return join(module_prefix(module), rel_path)


def month_map_key(month: str) -> str:
    parts = month.split("-")
    year = parts[0]
    month_part = parts[1] if len(parts) > 1 else month
    return join(everyday_prefix(), year, month_part, "index.json")


def everyday_media_key(date_str: str, uuid: str, ext: str) -> str:
    year, month, _ = date_str.split("-")
    ext = ext if ext.startswith(".") else f".{ext}" if ext else ""
    filename = f"{uuid}{ext}"
    return join(everyday_prefix(), year, month, filename)
