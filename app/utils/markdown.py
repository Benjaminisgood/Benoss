import os
import re
from typing import List


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_TITLE_RE = re.compile(r"^#\s+(.+)")
_OBSIDIAN_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
_OBSIDIAN_LINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")


def extract_title(content: str, fallback: str) -> str:
    if content.startswith("---"):
        match = _FRONTMATTER_RE.match(content)
        if match:
            for line in match.group(1).splitlines():
                if line.lower().startswith("title:"):
                    return line.split(":", 1)[1].strip().strip("\"")

    for line in content.splitlines():
        match = _TITLE_RE.match(line.strip())
        if match:
            return match.group(1).strip()

    return fallback


def _normalize_ref(ref: str) -> str:
    ref = ref.strip().strip("\"").strip("'")
    ref = ref.split("?")[0].split("#")[0]
    ref = ref.split("|")[0]
    return ref


def _is_local_ref(ref: str) -> bool:
    lower = ref.lower()
    if lower.startswith("http://") or lower.startswith("https://") or lower.startswith("data:"):
        return False
    return True


def _split_md_target(target: str) -> str:
    if not target:
        return ""
    return target.strip().split()[0]


def _has_extension(ref: str) -> bool:
    return bool(os.path.splitext(ref)[1])


def _is_markdown_ref(ref: str) -> bool:
    return os.path.splitext(ref)[1].lower() == ".md"


def _is_preferred_label(ref: str) -> bool:
    return bool(ref) and _is_local_ref(ref) and _has_extension(ref) and not _is_markdown_ref(ref)


def _is_valid_target(ref: str) -> bool:
    if not ref or not _is_local_ref(ref):
        return False
    if _is_markdown_ref(ref):
        return False
    return True


def _md_link_candidates(label: str, target: str) -> List[str]:
    candidates = []
    label_ref = _normalize_ref(label)
    if _is_preferred_label(label_ref):
        candidates.append(label_ref)
    target_ref = _normalize_ref(_split_md_target(target))
    if _is_valid_target(target_ref) and target_ref not in candidates:
        candidates.append(target_ref)
    return candidates


def find_attachment_refs(content: str) -> List[List[str]]:
    refs: List[List[str]] = []
    for match in _OBSIDIAN_EMBED_RE.finditer(content):
        ref = _normalize_ref(match.group(1))
        if _is_preferred_label(ref):
            refs.append([ref])

    for match in _OBSIDIAN_LINK_RE.finditer(content):
        ref = _normalize_ref(match.group(1))
        if _is_preferred_label(ref):
            refs.append([ref])

    for match in _MD_IMAGE_RE.finditer(content):
        candidates = _md_link_candidates(match.group(1), match.group(2))
        if candidates:
            refs.append(candidates)

    for match in _MD_LINK_RE.finditer(content):
        candidates = _md_link_candidates(match.group(1), match.group(2))
        if candidates:
            refs.append(candidates)

    return refs
