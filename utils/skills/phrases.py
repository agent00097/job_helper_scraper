"""Extract skill-like candidate phrases from JD section text."""
from __future__ import annotations

import re
from dataclasses import dataclass

_SPLIT_RE = re.compile(
    r"(?:,|;|/|\||\n|•|\u2022|\t|\band\b|\bor\b|\bwith\b|\bincluding\b)",
    re.IGNORECASE,
)

# Product-ish / acronym-ish spans inside longer prose.
_PROPER_SPAN_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z0-9+#.]{1,}(?:\s+[A-Z][a-zA-Z0-9+#.\/-]{1,}){0,5}"
    r"|[A-Z]{2,6}(?:\s*[A-Z]{2,6}){0,2}"
    r"|[A-Za-z][A-Za-z0-9+#.]*[+#])\b"
)

_TECHISH_RE = re.compile(
    r"\b(?:"
    r"rest\s*apis?|graphql|grpc|soap|"
    r"event[- ]driven(?:\s+architectures?)?|"
    r"google\s+cloud|amazon\s+web\s+services|"
    r"ci/?cd|machine\s+learning|data\s+science|"
    r"warehouse\s+management|order\s+management|"
    r"incident\s+management|change\s+management|"
    r"root\s+cause\s+analysis|"
    r"c\+\+|c#|node\.?js|react\.?js|"
    r"[a-z][a-z0-9+#.]{1,20}"
    r")\b",
    re.IGNORECASE,
)

_STOP = frozenset(
    {
        "and",
        "or",
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "as",
        "by",
        "at",
        "is",
        "are",
        "be",
        "this",
        "that",
        "such",
        "including",
        "including",
        "experience",
        "years",
        "year",
        "strong",
        "proven",
        "ability",
        "excellent",
        "solid",
        "knowledge",
        "understanding",
        "using",
        "use",
        "used",
        "etc",
        "etc.",
        "other",
        "across",
        "within",
        "large",
        "scale",
        "based",
        "role",
        "team",
        "teams",
        "work",
        "working",
        "support",
        "supporting",
        "manage",
        "managing",
        "management",  # alone too vague; keep in multiword via other extractors
        "project",
        "projects",
        "business",
        "technical",
        "customer",
        "customers",
        "international",
        "global",
        "successful",
        "related",
        "similar",
        "required",
        "preferred",
        "plus",
    }
)

_MAX_PHRASE_LEN = 60
_MAX_PHRASES = 50


@dataclass(frozen=True)
class Phrase:
    text: str
    source: str  # requirements | responsibilities | fallback


def _clean_phrase(raw: str) -> str:
    t = re.sub(r"\s+", " ", (raw or "").strip(" \t\r\n-•*|.,:;()[]{}\"'"))
    return t


_BAD_PREFIXES = (
    "experience ",
    "experiences ",
    "solid knowledge",
    "strong knowledge",
    "proven experience",
    "ability to",
    "excellent ",
    "hands-on ",
    "hands on ",
    "knowledge of",
    "understanding of",
    "including ",
    "such as ",
    "years of",
    "at least ",
)


def _acceptable(phrase: str) -> bool:
    if not phrase or len(phrase) < 2 or len(phrase) > _MAX_PHRASE_LEN:
        return False
    low = phrase.lower()
    if low in _STOP:
        return False
    if any(low.startswith(p) for p in _BAD_PREFIXES):
        return False
    # Drop pure numbers / seniority fluff.
    if re.fullmatch(r"[\d\W]+", phrase):
        return False
    if re.fullmatch(r"\d+\s*[\-–]\s*\d+\+?\s*years?", low):
        return False
    tokens = [t for t in re.split(r"\s+", low) if t]
    if not tokens:
        return False
    if len(tokens) == 1 and tokens[0] in _STOP:
        return False
    # Drop long prose clauses (keep short skill-like phrases).
    if len(tokens) > 6:
        return False
    # Prefer phrases with some alnum content.
    if not re.search(r"[a-z0-9]", low):
        return False
    return True


def extract_phrases(text: str, *, source: str) -> list[Phrase]:
    """Pull candidate skill phrases from a section."""
    if not text or not text.strip():
        return []

    found: list[str] = []

    # 1) Split list-like clauses.
    for part in _SPLIT_RE.split(text):
        cleaned = _clean_phrase(part)
        if _acceptable(cleaned):
            # Also keep shorter heads of long clauses (first 1–4 tokens).
            found.append(cleaned)
            toks = cleaned.split()
            if 4 < len(toks) <= 12:
                found.append(" ".join(toks[:3]))
                found.append(" ".join(toks[:4]))

    # 2) Proper / acronym spans from original casing.
    for m in _PROPER_SPAN_RE.finditer(text):
        cleaned = _clean_phrase(m.group(0))
        if _acceptable(cleaned):
            found.append(cleaned)

    # 3) Techish lowercase patterns.
    for m in _TECHISH_RE.finditer(text):
        cleaned = _clean_phrase(m.group(0))
        if _acceptable(cleaned):
            found.append(cleaned)

    # 4) Standalone ALLCAPS / Camel tokens (JIRA, SQL, MAWM, Xray).
    for m in re.finditer(r"\b([A-Z]{2,8}|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b", text):
        cleaned = _clean_phrase(m.group(1))
        if _acceptable(cleaned):
            found.append(cleaned)

    # Dedupe case-insensitively, preserve order.
    seen: set[str] = set()
    out: list[Phrase] = []
    for p in found:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(Phrase(text=p, source=source))
        if len(out) >= _MAX_PHRASES:
            break
    return out


def phrases_from_sections(
    requirements: str,
    responsibilities: str,
    fallback_text: str = "",
) -> list[Phrase]:
    phrases: list[Phrase] = []
    phrases.extend(extract_phrases(requirements, source="requirements"))
    phrases.extend(extract_phrases(responsibilities, source="responsibilities"))
    if not phrases and fallback_text:
        phrases.extend(extract_phrases(fallback_text, source="fallback"))
    # Final dedupe across sources (keep first / higher-priority source).
    seen: set[str] = set()
    out: list[Phrase] = []
    for p in phrases:
        key = p.text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= _MAX_PHRASES:
            break
    return out
