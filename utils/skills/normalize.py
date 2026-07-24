"""Text normalization helpers for skill matching."""
from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")
# Keep + # . for C++, C#, Node.js, etc.
_NON_SKILL_CHAR_RE = re.compile(r"[^a-z0-9+#./\s-]+")


def normalize_text(text: str) -> str:
    """Lowercase, strip noisy punctuation, collapse whitespace."""
    if not text:
        return ""
    t = text.lower().replace("\x00", " ")
    t = _NON_SKILL_CHAR_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def normalize_alias(text: str) -> str:
    return normalize_text(text)
