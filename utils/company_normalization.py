"""
Pure-function helpers for normalizing company names to a canonical form.
"""
import re

# Ordered longest-to-shortest so alternation matches the most-specific form first.
_SUFFIX_ALTS = "|".join([
    "incorporated", "corporation", "limited", "company",
    r"l\.l\.c\.?", r"s\.a\.?",
    r"inc\.?", r"ltd\.?", r"corp\.?",
    "llc", "gmbh", "plc", "pty",
    r"co\.?",
    "ag", "sa",
])

# Require a real word-boundary before the suffix: preceded by whitespace or comma.
# Using lookbehind keeps the separator in the string so we can strip it separately.
_SUFFIX_RE = re.compile(
    r"(?:(?<=\s)|(?<=,))(?:" + _SUFFIX_ALTS + r")\s*$",
    re.IGNORECASE,
)

_LEADING_TRAILING_NON_WORD = re.compile(r"^[^\w]+|[^\w]+$")


def normalize_company_name(name: str) -> str:
    """Return a canonical lowercase form of a company name.

    Steps: lowercase, collapse whitespace, strip common legal suffixes
    (iterating up to 5 times to handle stacked forms like "Co., Inc."),
    then strip any residual leading/trailing punctuation.
    """
    if not name:
        return ""

    s = name.strip().lower()
    s = re.sub(r"\s+", " ", s)

    for _ in range(5):
        prev = s
        # Strip trailing punctuation/separators first so the lookbehind in
        # _SUFFIX_RE sees the suffix cleanly (e.g. "Stripe Inc," → "Stripe Inc").
        s = re.sub(r"[,.\s]+$", "", s)
        s = _SUFFIX_RE.sub("", s)
        # Strip again in case the suffix had a trailing comma ("Stripe, Inc,").
        s = re.sub(r"[,.\s]+$", "", s)
        if s == prev:
            break

    s = _LEADING_TRAILING_NON_WORD.sub("", s)
    return s.strip()
