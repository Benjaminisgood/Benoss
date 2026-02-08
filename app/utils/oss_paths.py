from pathlib import PurePosixPath

from flask import current_app


def join(*parts: str) -> str:
    cleaned = [str(p).strip("/") for p in parts if p]
    return "/".join(cleaned)


def ensure_relative_key(rel_key: str) -> str:
    rel_key = str(rel_key or "").strip().lstrip("/")
    if not rel_key or ".." in PurePosixPath(rel_key).parts:
        raise ValueError("invalid key")
    return rel_key


def projects_prefix() -> str:
    base = current_app.config.get("OSS_PREFIX", "benoss")
    return join(base, "projects")


def project_prefix(project_uuid: str) -> str:
    project_uuid = ensure_relative_key(project_uuid)
    return join(projects_prefix(), project_uuid)


def project_object_key(project_uuid: str, object_name: str) -> str:
    object_name = ensure_relative_key(object_name)
    return join(project_prefix(project_uuid), object_name)


def whiteboard_prefix() -> str:
    base = current_app.config.get("OSS_PREFIX", "benoss")
    return join(base, "whiteboard")


def whiteboard_date_prefix(board_date: str) -> str:
    # board_date is validated elsewhere (YYYY-MM-DD). Keep it as a path segment for bucketing.
    board_date = ensure_relative_key(board_date)
    return join(whiteboard_prefix(), board_date)


def whiteboard_object_key(board_date: str, object_name: str) -> str:
    object_name = ensure_relative_key(object_name)
    return join(whiteboard_date_prefix(board_date), object_name)
