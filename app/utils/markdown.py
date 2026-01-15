import os
import re
from typing import List


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_TITLE_RE = re.compile(r"^#\s+(.+)")
_OBSIDIAN_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


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


def find_attachment_refs(content: str) -> List[str]:
    refs = []
    for match in _OBSIDIAN_EMBED_RE.finditer(content):
        ref = _normalize_ref(match.group(1))
        if ref and _is_local_ref(ref):
            refs.append(ref)

    for match in _MD_IMAGE_RE.finditer(content):
        ref = _normalize_ref(match.group(1).split()[0])
        if ref and _is_local_ref(ref):
            refs.append(ref)

    return list(dict.fromkeys(refs))
