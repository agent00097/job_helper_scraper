"""
Heuristic occupation category helpers.

Two pure functions: from_noc (uses Canada's NOC code) and from_title
(keyword-based, ~80% accuracy for ATS sources without NOC data).
Admin can override categories via the admin portal.

Canonical return values:
  tech, business, healthcare, service, trades, education, creative, science, other
"""
import re
from typing import Optional

# NOC first-digit → category
_NOC_MAP: dict[str, str] = {
    "0": "business",
    "1": "business",
    "2": "tech",
    "3": "healthcare",
    "4": "education",
    "5": "creative",
    "6": "service",
    "7": "trades",
    "8": "trades",
    "9": "trades",
}

# Tech: multi-word phrases checked before single words to avoid false prefix matches
_TECH_PHRASES = [
    "data scientist",
    "ml engineer",
    "machine learning",
    "full stack",
    "full-stack",
    "data engineer",
    "platform engineer",
    "security engineer",
    "software engineer",
    "devops engineer",
    "cloud engineer",
    "network engineer",
    "systems engineer",
    "reliability engineer",
]

# Single-word tech signals (no ambiguous short tokens like "it")
_TECH_WORDS = [
    "software",
    "developer",
    "programmer",
    "devops",
    "sre",
    "infrastructure",
    "security",
    "frontend",
    "backend",
    "mobile",
    "android",
    "ios",
    "qa",
    "sdet",
]

# Non-tech "engineer" phrases — excluded from the generic "engineer" → tech rule
_NON_TECH_ENGINEER = re.compile(
    r"\b(?:sales|service|field|support|account|application|process|"
    r"manufacturing|chemical|civil|mechanical|structural|electrical(?! engineer))\s+engineer\b",
    re.IGNORECASE,
)


def from_noc(noc_code: Optional[str]) -> str:
    """Map a NOC code (4 or 5 digits) to an occupation category.

    Takes the first digit and looks it up in the NOC classification table.
    Returns 'other' if the code is absent or unparseable.
    """
    if not noc_code or not isinstance(noc_code, str):
        return "other"
    digit = noc_code.strip()[:1]
    return _NOC_MAP.get(digit, "other")


def from_title(title: str) -> str:
    """Heuristic occupation category derived from a job title.

    Uses case-insensitive substring matching with priority order:
    tech > healthcare > business > education > creative > science > service > trades.
    Accuracy is ~80%; admin can override via the admin portal.
    """
    if not title:
        return "other"
    t = title.lower()

    # --- Tech ---
    for phrase in _TECH_PHRASES:
        if phrase in t:
            return "tech"

    # "engineer" → tech unless it's a non-tech discipline
    if "engineer" in t and not _NON_TECH_ENGINEER.search(t):
        return "tech"

    for word in _TECH_WORDS:
        if word in t:
            return "tech"

    # --- Healthcare ---
    for kw in ["nurse", "physician", "doctor", "medical", "clinical",
                "pharmacist", "therapist", "dentist"]:
        if kw in t:
            return "healthcare"

    # --- Business ---
    for kw in ["sales", "marketing", "finance", "accounting",
                "human resources", " hr ", "recruiter", "recruiting",
                "operations", "product manager", "business analyst",
                "consultant", "account executive", "customer success",
                "account manager"]:
        if kw in t:
            return "business"
    # Standalone "hr" word boundary (handles "HR Manager" etc.)
    if re.search(r"\bhr\b", t):
        return "business"

    # --- Education ---
    for kw in ["teacher", "professor", "instructor", "tutor",
                "principal", "educator"]:
        if kw in t:
            return "education"

    # --- Creative ---
    for kw in ["designer", "design", "ux", "ui", "copywriter",
                "content writer", "brand", "illustrator",
                "photographer", "video editor"]:
        if kw in t:
            return "creative"

    # --- Science --- (after creative to keep "researcher" out of creative)
    for kw in ["scientist", "researcher", "biologist", "chemist",
                "physicist", "lab tech"]:
        if kw in t:
            return "science"

    # --- Service ---
    for kw in ["bartender", "cashier", "retail", "waiter", "waitress",
                "hospitality", "cleaner", "cook", "chef", "food service",
                "housekeep"]:
        if kw in t:
            return "service"
    # "server" alone without any tech context
    if "server" in t and not any(w in t for w in ["sql server", "web server", "server admin"]):
        return "service"

    # --- Trades ---
    for kw in ["carpenter", "electrician", "plumber", "mason", "welder",
                "truck driver", "mechanic", "technician", "construction",
                "roofer", "driver"]:
        if kw in t:
            return "trades"

    return "other"
