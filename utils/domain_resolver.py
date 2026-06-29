"""
Resolve a real company domain from an ATS slug.

resolve_domain(company_name, slug) -> str | None
is_junk_slug(slug, company_name) -> bool
"""
from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_FETCH_TIMEOUT = 4  # seconds

_TRAILING_SUFFIXES = [
    "jobs", "careers", "inc", "llc", "corp", "global", "hq",
    "rebuild", "external", "bio", "pbc", "group", "labs",
    "team", "talent",
]

_ALT_TLDS = [".io", ".ai", ".co", ".org", ".net"]

_STOPWORDS = {
    "the", "a", "an", "of", "and", "for", "inc", "llc", "corp", "co", "ltd",
    "group", "company", "careers", "jobs", "global", "holdings", "international",
    "technologies", "technology", "solutions", "services", "systems", "partners",
    "capital", "management", "health", "labs", "studio", "studios", "ventures",
    "corporation", "limited", "us", "usa",
}

_JUNK_NAME_RE = re.compile(
    r"\bnot on website\b|\bsandbox\b|\btest\b|\binternal employees only\b",
    re.IGNORECASE,
)


def is_junk_slug(slug: str, company_name: str) -> bool:
    """Return True for slugs that clearly don't belong to real companies."""
    if not slug:
        return True
    # Hex-hash / UUID-like: > 30 chars and only hex digits + hyphens
    if len(slug) > 30 and re.match(r"^[0-9a-f-]+$", slug):
        return True
    if _JUNK_NAME_RE.search(company_name or ""):
        return True
    return False


def _normalize_slug(slug: str) -> str:
    """Lowercase and keep only [a-z0-9-]."""
    return re.sub(r"[^a-z0-9-]", "", slug.lower())


def _slug_variants(base: str) -> list[str]:
    """Return base slug + suffix-stripped variants."""
    variants = [base]
    for suffix in _TRAILING_SUFFIXES:
        candidate = f"-{suffix}"
        if base.endswith(candidate) and len(base) > len(suffix) + 2:
            stripped = base[: -len(candidate)]
            if stripped and stripped not in variants:
                variants.append(stripped)
    return variants


def _distinctive_words(company_name: str, slug: str) -> set[str]:
    """Tokens from company_name + slug that are longer than 2 chars and not stopwords."""
    tokens: set[str] = set()
    for text in (company_name, slug):
        for token in re.split(r"[\s\-_]+", text.lower()):
            token = re.sub(r"[^a-z0-9]", "", token)
            if len(token) > 2 and token not in _STOPWORDS:
                tokens.add(token)
    return tokens


def _extract_signals(html: str) -> tuple[str, str]:
    """Extract page signals; returns (strong, weak).

    strong = <title> + og:site_name + og:title   — ownership signals
    weak   = application-name + description       — mention signals only
    """
    strong_parts: list[str] = []
    weak_parts: list[str] = []

    m = re.search(r"<title[^>]*>([^<]{0,300})</title>", html, re.IGNORECASE)
    if m:
        strong_parts.append(m.group(1))

    for meta_tag in re.finditer(r"<meta\b[^>]*>", html, re.IGNORECASE):
        tag = meta_tag.group()
        matched = False
        for attr, target, bucket in (
            ("property", "og:site_name", strong_parts),
            ("property", "og:title", strong_parts),
            ("name", "application-name", weak_parts),
            ("name", "description", weak_parts),
        ):
            if re.search(
                rf'\b{attr}\s*=\s*["\']?{re.escape(target)}["\']?',
                tag,
                re.IGNORECASE,
            ):
                cm = re.search(
                    r'\bcontent\s*=\s*["\']([^"\']{0,300})["\']',
                    tag,
                    re.IGNORECASE,
                )
                if cm:
                    bucket.append(cm.group(1))
                matched = True
                break
            if matched:
                break

    return " ".join(strong_parts).lower(), " ".join(weak_parts).lower()


def _fetch_and_verify(url: str, distinctive: set[str]) -> bool:
    """Fetch *url* and return True iff it looks like the target company's page.

    A distinctive word must appear in the STRONG signals (title / og:site_name /
    og:title).  A match only in weak signals (description / application-name) is
    insufficient — that pattern leaks on wrong-domain pages that merely mention
    the target company name in their description.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _CHROME_UA},
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True,
        )
    except Exception:
        return False

    if resp.status_code >= 400:
        return False
    if "text/html" not in resp.headers.get("Content-Type", ""):
        return False

    strong, _ = _extract_signals(resp.text)
    return any(word in strong for word in distinctive)


def resolve_domain(company_name: str, slug: str) -> str | None:
    """Resolve an ATS slug to a verified company domain.

    Tries .com first across all slug variants; falls back to alt TLDs only if
    no .com verifies.  Returns None when nothing verifies.
    """
    base = _normalize_slug(slug)
    if not base:
        return None

    variants = _slug_variants(base)
    distinctive = _distinctive_words(company_name, base)
    if not distinctive:
        return None

    # Pass 1: .com variants (preferred)
    for variant in variants:
        if _fetch_and_verify(f"https://{variant}.com", distinctive):
            return f"{variant}.com"

    # Pass 2: alt TLDs — only reached if no .com verified
    for variant in variants:
        for tld in _ALT_TLDS:
            if _fetch_and_verify(f"https://{variant}{tld}", distinctive):
                return f"{variant}{tld}"

    return None
