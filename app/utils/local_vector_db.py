from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from flask import current_app

from .local_archive import list_archive_files, load_archive
from .runtime_settings import get_setting_int, get_setting_str


_LATIN_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_CHUNK_RE = re.compile(r"[\u4e00-\u9fff]+")
_STOPWORDS = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "this",
    "from",
    "have",
    "were",
    "been",
    "into",
    "about",
    "what",
    "when",
    "where",
    "which",
    "will",
    "would",
    "could",
    "your",
    "you",
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "因为",
    "所以",
    "以及",
    "进行",
    "一个",
    "可以",
    "今天",
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


def _tokenize(text: str) -> list[str]:
    raw = str(text or "").strip().lower()
    if not raw:
        return []

    tokens: list[str] = []
    for item in _LATIN_RE.findall(raw):
        if item in _STOPWORDS:
            continue
        tokens.append(item)

    for chunk in _CJK_CHUNK_RE.findall(raw):
        if not chunk:
            continue
        if chunk in _STOPWORDS:
            continue
        if len(chunk) <= 2:
            tokens.append(chunk)
            continue
        tokens.append(chunk)
        for idx in range(len(chunk) - 1):
            tokens.append(chunk[idx : idx + 2])

    return tokens[:3000]


def _document_text(item: dict) -> str:
    parts = [
        str(item.get("preview") or ""),
        str(item.get("text") or ""),
    ]
    tags = item.get("tags") or []
    if isinstance(tags, list) and tags:
        parts.append(" ".join(f"#{str(tag)}" for tag in tags))
    return "\n".join(part for part in parts if part).strip()


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
            docs[doc_id] = {
                "id": doc_id,
                "day": day,
                "record_id": record_id,
                "user_id": int(user.get("id") or 0),
                "username": str(user.get("username") or ""),
                "tags": [str(tag) for tag in (row.get("tags") or []) if str(tag).strip()],
                "created_at": row.get("created_at"),
                "preview": str(row.get("preview") or ""),
                "text": text,
            }

    ordered = sorted(
        docs.values(),
        key=lambda item: (str(item.get("day") or ""), int(item.get("record_id") or 0)),
        reverse=True,
    )
    if max_docs > 0:
        ordered = ordered[:max_docs]
    return ordered, archive_count


def build_index(*, max_docs: int | None = None) -> dict:
    max_docs_default = get_setting_int(
        "VECTOR_MAX_DOCS",
        default=int(current_app.config.get("VECTOR_MAX_DOCS") or 4000),
    )
    max_docs_value = int(max_docs or max_docs_default or 4000)
    max_docs_value = max(200, min(max_docs_value, 30000))

    documents, archive_count = _archive_documents(max_docs=max_docs_value)
    if not documents:
        payload = {
            "built_at": datetime.utcnow().isoformat() + "Z",
            "doc_count": 0,
            "archive_count": archive_count,
            "vocab_size": 0,
            "documents": [],
            "idf": {},
            "vectors": [],
            "norms": [],
        }
        _index_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {
            "ok": True,
            "doc_count": 0,
            "archive_count": archive_count,
            "vocab_size": 0,
            "index_path": str(_index_path()),
            "built_at": payload["built_at"],
        }

    token_counts: list[Counter[str]] = []
    doc_freq: Counter[str] = Counter()
    for doc in documents:
        counter = Counter(_tokenize(doc.get("text") or ""))
        token_counts.append(counter)
        doc_freq.update(counter.keys())

    doc_total = len(documents)
    idf: dict[str, float] = {}
    for token, df in doc_freq.items():
        idf[token] = round(math.log((doc_total + 1) / (int(df) + 1)) + 1.0, 6)

    vectors: list[dict[str, float]] = []
    norms: list[float] = []
    for counter in token_counts:
        total = float(sum(counter.values()) or 1.0)
        weights: dict[str, float] = {}
        for token, count in counter.items():
            tf = float(count) / total
            weights[token] = tf * idf.get(token, 1.0)
        top_weights = sorted(weights.items(), key=lambda item: item[1], reverse=True)[:120]
        compact = {token: round(weight, 6) for token, weight in top_weights}
        vectors.append(compact)
        norm = math.sqrt(sum(weight * weight for weight in compact.values()))
        norms.append(round(norm, 8))

    payload = {
        "built_at": datetime.utcnow().isoformat() + "Z",
        "doc_count": doc_total,
        "archive_count": archive_count,
        "vocab_size": len(idf),
        "documents": documents,
        "idf": idf,
        "vectors": vectors,
        "norms": norms,
    }
    _index_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {
        "ok": True,
        "doc_count": payload["doc_count"],
        "archive_count": payload["archive_count"],
        "vocab_size": payload["vocab_size"],
        "index_path": str(_index_path()),
        "built_at": payload["built_at"],
    }


def _load_index() -> dict:
    path = _index_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def ensure_index() -> dict:
    data = _load_index()
    if data.get("doc_count") is None:
        return build_index()
    return {
        "ok": True,
        "doc_count": int(data.get("doc_count") or 0),
        "archive_count": int(data.get("archive_count") or 0),
        "vocab_size": int(data.get("vocab_size") or 0),
        "index_path": str(_index_path()),
        "built_at": str(data.get("built_at") or ""),
    }


def index_meta() -> dict:
    data = _load_index()
    return {
        "ready": bool(data),
        "doc_count": int(data.get("doc_count") or 0),
        "archive_count": int(data.get("archive_count") or 0),
        "vocab_size": int(data.get("vocab_size") or 0),
        "built_at": str(data.get("built_at") or ""),
        "index_path": str(_index_path()),
    }


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

    query_counter = Counter(_tokenize(query_text))
    total = float(sum(query_counter.values()) or 1.0)
    idf = data.get("idf") or {}
    query_weights: dict[str, float] = {}
    for token, count in query_counter.items():
        token_idf = float(idf.get(token, 0.0))
        if token_idf <= 0:
            continue
        query_weights[token] = (float(count) / total) * token_idf
    query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values()))
    if query_norm <= 0:
        return {"query": query_text, "hits": [], "meta": index_meta()}

    documents = data.get("documents") or []
    vectors = data.get("vectors") or []
    norms = data.get("norms") or []
    size = min(len(documents), len(vectors), len(norms))

    scored: list[dict] = []
    for idx in range(size):
        doc = documents[idx]
        doc_weights = vectors[idx]
        doc_norm = float(norms[idx] or 0.0)
        if doc_norm <= 0:
            continue

        dot = 0.0
        for token, query_weight in query_weights.items():
            doc_weight = float(doc_weights.get(token, 0.0))
            if doc_weight:
                dot += query_weight * doc_weight
        if dot <= 0:
            continue
        score = dot / (query_norm * doc_norm)
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
