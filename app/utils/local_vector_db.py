from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import requests
from flask import current_app

from .local_archive import list_archive_files, load_archive
from .runtime_settings import get_setting_int, get_setting_str


_SCHEMA_VERSION = 2
_PROVIDER_ALIASES = {
    "chat_anywhere": "chatanywhere",
    "chat-anywhere": "chatanywhere",
    "dashscope": "aliyun",
}


def _vector_dir() -> Path:
    default_path = str(current_app.config.get("LOCAL_VECTOR_STORE_DIR") or "")
    configured = get_setting_str("LOCAL_VECTOR_STORE_DIR", default=default_path).strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        path = Path(current_app.root_path).parent / "data" / "vector-store"
    if not path.is_absolute():
        path = (Path(current_app.root_path).parent / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path() -> Path:
    return _vector_dir() / "index.json"


def _normalize_provider(value: str) -> str:
    raw = str(value or "").strip().lower()
    return _PROVIDER_ALIASES.get(raw, raw)


def _embedding_provider_settings() -> dict | None:
    provider = _normalize_provider(get_setting_str("AI_AUTOFILL_PROVIDER", default=""))
    if not provider:
        return None

    choices = {
        "chatanywhere": {
            "api_key": get_setting_str("CHAT_ANYWHERE_API_KEY", default=""),
            "base_url": get_setting_str("CHAT_ANYWHERE_API_BASE_URL", default="https://api.chatanywhere.tech/v1"),
        },
        "deepseek": {
            "api_key": get_setting_str("DEEPSEEK_API_KEY", default=""),
            "base_url": get_setting_str("DEEPSEEK_API_BASE_URL", default="https://api.deepseek.com/v1"),
        },
        "aliyun": {
            "api_key": get_setting_str("ALIYUN_AI_API_KEY", default=""),
            "base_url": get_setting_str(
                "ALIYUN_AI_API_BASE_URL",
                default="https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
        },
    }
    selected = choices.get(provider)
    if not selected:
        return None

    api_key = str(selected.get("api_key") or "").strip()
    base_url = str(selected.get("base_url") or "").strip().rstrip("/")
    model = get_setting_str("VECTOR_EMBEDDING_MODEL", default="text-embedding-3-small").strip()
    if not api_key or not base_url or not model:
        return None

    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }


def _truncate_embedding_input(text: str) -> str:
    limit = max(200, min(get_setting_int("VECTOR_EMBEDDING_MAX_INPUT_CHARS", default=4000), 20000))
    cleaned = str(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip()


def _embed_texts(texts: list[str]) -> tuple[list[list[float]], dict]:
    settings = _embedding_provider_settings()
    if not settings:
        raise RuntimeError("embedding provider not configured")

    cleaned_inputs = [_truncate_embedding_input(item) for item in texts]
    if not cleaned_inputs:
        return [], {"provider": settings["provider"], "model": settings["model"]}

    payload = {
        "model": settings["model"],
        "input": cleaned_inputs,
        "encoding_format": "float",
    }
    timeout = get_setting_int("AI_REQUEST_TIMEOUT_SECONDS", default=45)
    endpoint = settings["base_url"] + "/embeddings"
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"embedding request error: {exc}") from exc

    if not response.ok:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 320:
            detail = detail[:320] + "..."
        raise RuntimeError(f"embedding request failed ({response.status_code}): {detail}")

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("embedding response is not json") from exc

    rows = data.get("data") or []
    vectors: list[list[float]] = [None] * len(cleaned_inputs)  # type: ignore[list-item]
    for item in rows:
        if not isinstance(item, dict):
            continue
        idx = int(item.get("index") or 0)
        if idx < 0 or idx >= len(cleaned_inputs):
            continue
        emb = item.get("embedding")
        if not isinstance(emb, list) or not emb:
            continue
        vectors[idx] = [float(v) for v in emb]

    if any(vec is None for vec in vectors):
        raise RuntimeError("embedding response missing vectors")

    return vectors, {"provider": settings["provider"], "model": settings["model"]}


def _vector_norm(values: list[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(float(v) * float(v) for v in values))


def _cosine_similarity(query_vector: list[float], doc_vector: list[float], *, query_norm: float, doc_norm: float) -> float:
    if query_norm <= 0.0 or doc_norm <= 0.0:
        return 0.0
    size = min(len(query_vector), len(doc_vector))
    if size <= 0:
        return 0.0
    dot = 0.0
    for idx in range(size):
        dot += float(query_vector[idx]) * float(doc_vector[idx])
    if dot <= 0.0:
        return 0.0
    return dot / (query_norm * doc_norm)


def _document_text(item: dict) -> str:
    parts = [
        str(item.get("preview") or ""),
        str(item.get("text") or ""),
    ]
    tags = item.get("tags") or []
    if isinstance(tags, list) and tags:
        parts.append(" ".join(f"#{str(tag)}" for tag in tags))
    return "\n".join(part for part in parts if part).strip()


def _doc_content_hash(*, day: str, record_id: int, text: str, tags: list[str], username: str) -> str:
    digest = hashlib.sha256()
    digest.update(day.encode("utf-8"))
    digest.update(b"\n")
    digest.update(str(record_id).encode("utf-8"))
    digest.update(b"\n")
    digest.update(username.encode("utf-8"))
    digest.update(b"\n")
    digest.update(",".join(tags).encode("utf-8"))
    digest.update(b"\n")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def _archive_documents(max_docs: int) -> tuple[list[dict], int]:
    archives = list_archive_files()
    docs: dict[str, dict] = {}
    archive_count = 0

    for path in archives:
        archive = load_archive(path)
        scope = str(archive.get("scope") or "public").strip().lower()
        if scope != "public":
            continue
        rows = archive.get("records") or []
        if not isinstance(rows, list):
            continue
        archive_count += 1
        day = str(archive.get("day") or path.stem)
        for row in rows:
            if not isinstance(row, dict):
                continue
            record_id = int(row.get("id") or 0)
            if record_id <= 0:
                continue

            text = _document_text(row)
            if not text:
                continue

            doc_id = f"{day}:{record_id}"
            user = row.get("user") or {}
            tags = [str(tag) for tag in (row.get("tags") or []) if str(tag).strip()]
            username = str(user.get("username") or "")
            docs[doc_id] = {
                "id": doc_id,
                "day": day,
                "record_id": record_id,
                "user_id": int(user.get("id") or 0),
                "username": username,
                "tags": tags,
                "created_at": row.get("created_at"),
                "preview": str(row.get("preview") or ""),
                "text": text,
                "content_hash": _doc_content_hash(
                    day=day,
                    record_id=record_id,
                    text=text,
                    tags=tags,
                    username=username,
                ),
            }

    ordered = sorted(
        docs.values(),
        key=lambda item: (str(item.get("day") or ""), int(item.get("record_id") or 0)),
        reverse=True,
    )
    if max_docs > 0:
        ordered = ordered[:max_docs]
    return ordered, archive_count


def _load_index() -> dict:
    path = _index_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            schema_version = int(data.get("schema_version") or 0)
            if schema_version == _SCHEMA_VERSION:
                return data

            # Backward compatibility: accept schema-less payload if documents already carry dense vectors.
            docs = data.get("documents")
            if isinstance(docs, list) and docs:
                first = docs[0]
                if isinstance(first, dict) and isinstance(first.get("vector"), list) and first.get("vector"):
                    migrated = dict(data)
                    migrated["schema_version"] = _SCHEMA_VERSION
                    return migrated
            return {}
    except Exception:
        return {}
    return {}


def _write_index(payload: dict) -> None:
    _index_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _normalize_existing_documents(rows) -> list[dict]:
    docs: list[dict] = []
    if not isinstance(rows, list):
        return docs
    for item in rows:
        if not isinstance(item, dict):
            continue
        vector = item.get("vector")
        if not isinstance(vector, list) or not vector:
            continue
        clean_vec = [float(v) for v in vector]
        doc = dict(item)
        doc["vector"] = clean_vec
        doc["vector_norm"] = float(item.get("vector_norm") or _vector_norm(clean_vec))
        doc["vector_dim"] = int(item.get("vector_dim") or len(clean_vec))
        docs.append(doc)
    return docs


def _empty_index(*, archive_count: int, embedding_info: dict | None = None) -> dict:
    now = datetime.utcnow().isoformat() + "Z"
    embedding = embedding_info or {}
    return {
        "schema_version": _SCHEMA_VERSION,
        "built_at": now,
        "updated_at": now,
        "archive_count": int(archive_count),
        "doc_count": 0,
        "vector_dim": int(embedding.get("vector_dim") or 0),
        "embedding": {
            "provider": str(embedding.get("provider") or ""),
            "model": str(embedding.get("model") or ""),
        },
        "documents": [],
    }


def _index_summary(payload: dict, *, changes: dict | None = None) -> dict:
    summary = {
        "ok": True,
        "doc_count": int(payload.get("doc_count") or 0),
        "archive_count": int(payload.get("archive_count") or 0),
        "vector_dim": int(payload.get("vector_dim") or 0),
        "vocab_size": int(payload.get("vector_dim") or 0),
        "embedding_model": str((payload.get("embedding") or {}).get("model") or ""),
        "embedding_provider": str((payload.get("embedding") or {}).get("provider") or ""),
        "index_path": str(_index_path()),
        "built_at": str(payload.get("built_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
    }
    if changes:
        summary.update(changes)
    return summary


def build_index(*, max_docs: int | None = None, force: bool = False) -> dict:
    max_docs_default = get_setting_int(
        "VECTOR_MAX_DOCS",
        default=int(current_app.config.get("VECTOR_MAX_DOCS") or 4000),
    )
    max_docs_value = int(max_docs or max_docs_default or 4000)
    max_docs_value = max(200, min(max_docs_value, 30000))

    source_documents, archive_count = _archive_documents(max_docs=max_docs_value)
    existing = _load_index() if not force else {}
    existing_docs = _normalize_existing_documents(existing.get("documents"))
    existing_map = {str(item.get("id") or ""): item for item in existing_docs if str(item.get("id") or "")}

    expected_model = get_setting_str("VECTOR_EMBEDDING_MODEL", default="text-embedding-3-small").strip()
    existing_model = str((existing.get("embedding") or {}).get("model") or "").strip()
    model_changed = bool(existing_model and expected_model and existing_model != expected_model)
    if model_changed:
        existing_map = {}
        force = True

    if not source_documents:
        payload = _empty_index(
            archive_count=archive_count,
            embedding_info={
                "provider": (existing.get("embedding") or {}).get("provider", ""),
                "model": expected_model or existing_model,
                "vector_dim": int(existing.get("vector_dim") or 0),
            },
        )
        _write_index(payload)
        return _index_summary(
            payload,
            changes={
                "inserted_count": 0,
                "updated_count": 0,
                "unchanged_count": 0,
                "removed_count": len(existing_map),
                "force_rebuild": bool(force),
            },
        )

    kept_documents: list[dict] = []
    embed_tasks: list[tuple[dict, str, str]] = []
    unchanged_count = 0
    inserted_count = 0
    updated_count = 0
    source_ids: set[str] = set()

    for item in source_documents:
        doc_id = str(item.get("id") or "")
        if not doc_id:
            continue
        source_ids.add(doc_id)
        previous = existing_map.get(doc_id)

        if (
            previous
            and str(previous.get("content_hash") or "") == str(item.get("content_hash") or "")
            and isinstance(previous.get("vector"), list)
            and previous.get("vector")
        ):
            merged = dict(item)
            merged["vector"] = [float(v) for v in (previous.get("vector") or [])]
            merged["vector_dim"] = int(previous.get("vector_dim") or len(merged["vector"]))
            merged["vector_norm"] = float(previous.get("vector_norm") or _vector_norm(merged["vector"]))
            kept_documents.append(merged)
            unchanged_count += 1
            continue

        if previous:
            updated_count += 1
        else:
            inserted_count += 1
        embed_tasks.append((item, str(item.get("text") or ""), doc_id))

    removed_count = len([doc_id for doc_id in existing_map.keys() if doc_id not in source_ids])

    embedded_map: dict[str, list[float]] = {}
    embed_provider = str((existing.get("embedding") or {}).get("provider") or "")
    embed_model = expected_model or str((existing.get("embedding") or {}).get("model") or "")
    batch_size = max(1, min(get_setting_int("VECTOR_EMBEDDING_BATCH_SIZE", default=16), 128))

    if embed_tasks:
        if not _embedding_provider_settings():
            raise RuntimeError("embedding provider not configured: cannot upsert new/updated documents")

        for offset in range(0, len(embed_tasks), batch_size):
            chunk = embed_tasks[offset : offset + batch_size]
            texts = [task[1] for task in chunk]
            vectors, info = _embed_texts(texts)
            embed_provider = str(info.get("provider") or embed_provider)
            embed_model = str(info.get("model") or embed_model)
            for idx, (_, _, doc_id) in enumerate(chunk):
                embedded_map[doc_id] = vectors[idx]

    final_documents: list[dict] = []
    vector_dim = 0
    for item in source_documents:
        doc_id = str(item.get("id") or "")
        if not doc_id:
            continue

        vector = embedded_map.get(doc_id)
        if vector is None:
            previous = existing_map.get(doc_id)
            if previous:
                vector = [float(v) for v in (previous.get("vector") or [])]
        if not vector:
            continue

        norm = _vector_norm(vector)
        merged = dict(item)
        merged["vector"] = vector
        merged["vector_dim"] = len(vector)
        merged["vector_norm"] = round(norm, 10)
        final_documents.append(merged)
        if len(vector) > vector_dim:
            vector_dim = len(vector)

    now = datetime.utcnow().isoformat() + "Z"
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "built_at": str(existing.get("built_at") or now),
        "updated_at": now,
        "archive_count": int(archive_count),
        "doc_count": len(final_documents),
        "vector_dim": int(vector_dim),
        "embedding": {
            "provider": embed_provider,
            "model": embed_model,
        },
        "documents": final_documents,
    }
    if not existing:
        payload["built_at"] = now
    _write_index(payload)

    return _index_summary(
        payload,
        changes={
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "removed_count": removed_count,
            "force_rebuild": bool(force),
            "model_changed": bool(model_changed),
        },
    )


def ensure_index() -> dict:
    data = _load_index()
    if data.get("doc_count") is None:
        return build_index()
    return _index_summary(data)


def index_meta() -> dict:
    data = _load_index()
    meta = _index_summary(data) if data else {
        "ok": True,
        "doc_count": 0,
        "archive_count": 0,
        "vector_dim": 0,
        "vocab_size": 0,
        "embedding_model": "",
        "embedding_provider": "",
        "index_path": str(_index_path()),
        "built_at": "",
        "updated_at": "",
    }
    meta["ready"] = bool(data)
    return meta


def search(query: str, *, top_k: int = 6) -> dict:
    query_text = str(query or "").strip()
    if not query_text:
        return {"query": "", "hits": [], "meta": index_meta()}

    data = _load_index()
    if not data:
        build_index()
        data = _load_index()
    if not data:
        return {"query": query_text, "hits": [], "meta": index_meta()}

    documents = _normalize_existing_documents(data.get("documents"))
    if not documents:
        return {"query": query_text, "hits": [], "meta": index_meta()}

    query_vectors, _ = _embed_texts([query_text])
    if not query_vectors or not query_vectors[0]:
        return {"query": query_text, "hits": [], "meta": index_meta()}
    query_vector = query_vectors[0]
    query_norm = _vector_norm(query_vector)
    if query_norm <= 0:
        return {"query": query_text, "hits": [], "meta": index_meta()}

    scored: list[dict] = []
    for doc in documents:
        doc_vector = doc.get("vector") or []
        if not isinstance(doc_vector, list) or not doc_vector:
            continue
        score = _cosine_similarity(
            query_vector,
            doc_vector,
            query_norm=query_norm,
            doc_norm=float(doc.get("vector_norm") or _vector_norm(doc_vector)),
        )
        if score <= 0:
            continue

        snippet = str(doc.get("text") or "").strip()
        if len(snippet) > 240:
            snippet = snippet[:239].rstrip() + "…"
        scored.append(
            {
                "id": str(doc.get("id") or ""),
                "day": str(doc.get("day") or ""),
                "record_id": int(doc.get("record_id") or 0),
                "username": str(doc.get("username") or ""),
                "tags": doc.get("tags") or [],
                "score": round(float(score), 6),
                "snippet": snippet,
                "created_at": doc.get("created_at"),
                "text": str(doc.get("text") or ""),
            }
        )

    scored.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    limit = max(1, min(int(top_k or 6), 20))
    hits = scored[:limit]
    return {"query": query_text, "hits": hits, "meta": index_meta()}


def build_chat_context(hits: list[dict], *, max_chars: int = 5000) -> str:
    chunks: list[str] = []
    total = 0
    for idx, hit in enumerate(hits, start=1):
        lines = [
            f"[Hit {idx}] score={hit.get('score', 0.0)} day={hit.get('day', '')} record={hit.get('record_id', 0)} user={hit.get('username', '')}",
            str(hit.get("text") or ""),
        ]
        block = "\n".join(lines).strip()
        if not block:
            continue
        plus = len(block) + 2
        if chunks and total + plus > max_chars:
            break
        chunks.append(block)
        total += plus
    return "\n\n".join(chunks)
