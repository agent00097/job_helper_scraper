"""Locate high-signal sections in a job description."""
from __future__ import annotations

import re
from dataclasses import dataclass

_REQ_PATTERNS = [
    r"requirements?",
    r"qualifications?",
    r"what you(?:'ll| will) need",
    r"what you(?:'ll| will) bring",
    r"you(?:'ll| will) bring",
    r"must haves?",
    r"nice to haves?",
    r"basic qualifications?",
    r"minimum qualifications?",
    r"preferred qualifications?",
    r"tech stack",
    r"our stack",
    r"skills\s+required",
    r"skills\s+needed",
    r"required\s+skills",
    r"experience\s+required",
    r"experience\s+needed",
    r"required\s+experience",
    r"preferred\s+experience",
]

_RESP_PATTERNS = [
    r"what you(?:'ll| will) do",
    r"what you do",
    r"responsibilities",
    r"accountabilities",
    r"the role",
    r"about the (?:role|job|position)",
    r"key (?:responsibilities|accountabilities|duties)",
    r"your (?:responsibilities|duties)",
    r"day to day",
    r"in this role",
]

_END_PATTERNS = _REQ_PATTERNS + _RESP_PATTERNS + [
    r"what we offer",
    r"benefits?",
    r"about (?:us|the company)",
    r"who we are",
    r"equal opportunity",
    r"compensation",
    r"salary",
]


def _heading_regex(patterns: list[str]) -> re.Pattern[str]:
    body = "|".join(f"(?:{p})" for p in patterns)
    return re.compile(
        rf"(?is)(?:^|\n)\s*(?:[#>*\-•]+\s*)?(?:(?:\d+[\.\)]\s*)?)({body})\b\s*:?\s*",
    )


_REQ_RE = _heading_regex(_REQ_PATTERNS)
_RESP_RE = _heading_regex(_RESP_PATTERNS)
_ANY_SECTION_RE = _heading_regex(_END_PATTERNS)


@dataclass(frozen=True)
class JobSections:
    requirements: str = ""
    responsibilities: str = ""
    structured: bool = False

    @property
    def high_signal_text(self) -> str:
        parts = [p for p in (self.requirements, self.responsibilities) if p]
        return "\n\n".join(parts)


def _slice_section(text: str, match: re.Match[str]) -> str:
    start = match.end()
    nxt = _ANY_SECTION_RE.search(text, start)
    end = nxt.start() if nxt else len(text)
    return text[start:end].strip()


def extract_sections(description: str) -> JobSections:
    """
    Pull Requirements/Qualifications and What-you'll-do style sections.

    If none found, returns empty strings and structured=False (caller should
    fall back to the full description).
    """
    text = description or ""
    if not text.strip():
        return JobSections()

    req = ""
    resp = ""

    req_matches = list(_REQ_RE.finditer(text))
    if req_matches:
        candidates = [_slice_section(text, m) for m in req_matches]
        req = max(candidates, key=len)

    resp_matches = list(_RESP_RE.finditer(text))
    if resp_matches:
        candidates = [_slice_section(text, m) for m in resp_matches]
        resp = max(candidates, key=len)

    structured = bool(req or resp)
    return JobSections(
        requirements=req,
        responsibilities=resp,
        structured=structured,
    )
