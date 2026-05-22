"""
Pure-function geographic inference from free-text location strings.

derive_country(location) -> "CA" | "US" | None
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canadian provinces and territories — full name (lowercase) and abbreviation
# ---------------------------------------------------------------------------

_CA_PROVINCE_NAMES: frozenset[str] = frozenset({
    "ontario", "quebec", "québec",
    "british columbia",
    "alberta", "manitoba", "saskatchewan",
    "nova scotia", "new brunswick",
    "newfoundland and labrador", "newfoundland", "labrador",
    "prince edward island",
    "northwest territories", "yukon", "nunavut",
})

_CA_PROVINCE_ABBREVS: frozenset[str] = frozenset({
    "ON", "QC", "BC", "AB", "MB", "SK",
    "NS", "NB", "NL", "PE", "NT", "YT", "NU",
})

# ---------------------------------------------------------------------------
# US states (all 50 + DC) — full name (lowercase) and abbreviation
# ---------------------------------------------------------------------------

_US_STATE_NAMES: frozenset[str] = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california",
    "colorado", "connecticut", "delaware", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts",
    "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma",
    "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
})

_US_STATE_ABBREVS: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})

# ---------------------------------------------------------------------------
# Unambiguous city names
# Excluded: London (London UK), Victoria (Victoria TX / Victoria BC),
#           Windsor (Windsor UK), etc. — too common outside North America.
# ---------------------------------------------------------------------------

_CA_CITIES: frozenset[str] = frozenset({
    "toronto", "montreal", "montréal", "vancouver", "calgary", "edmonton",
    "winnipeg", "ottawa", "brampton", "mississauga", "burnaby", "surrey",
    "laval", "markham", "vaughan", "gatineau", "saskatoon", "kelowna",
    "abbotsford", "barrie", "sudbury", "hamilton", "kitchener", "guelph",
    "kingston", "thunder bay", "sherbrooke", "longueuil", "saguenay",
    "trois-rivières", "trois-rivieres", "richmond hill", "oakville",
    "oshawa", "whitby", "pickering", "ajax",
})

_US_CITIES: frozenset[str] = frozenset({
    "new york", "new york city", "nyc", "los angeles", "chicago",
    "houston", "phoenix", "philadelphia", "san antonio", "san diego",
    "dallas", "san jose", "austin", "jacksonville", "fort worth",
    "columbus", "charlotte", "indianapolis", "san francisco", "seattle",
    "denver", "nashville", "las vegas", "boston", "memphis", "louisville",
    "baltimore", "milwaukee", "albuquerque", "tucson", "fresno",
    "sacramento", "mesa", "atlanta", "omaha", "colorado springs",
    "raleigh", "miami", "cleveland", "tulsa", "arlington", "new orleans",
    "wichita", "bakersfield", "tampa", "aurora", "anaheim", "santa ana",
    "corpus christi", "riverside", "st. louis", "lexington", "pittsburgh",
    "stockton", "anchorage", "cincinnati", "st. paul", "greensboro",
    "toledo", "newark", "plano", "henderson", "lincoln", "buffalo",
    "jersey city", "chandler", "laredo", "chula vista", "scottsdale",
    "norfolk", "madison", "orlando",
    # common shorthand used in job postings
    "bay area", "san francisco bay area", "silicon valley",
    "washington dc", "washington, dc",
})

# ---------------------------------------------------------------------------
# Pattern: one 2-letter uppercase token preceded by a separator (comma,
# space, pipe, slash) and followed by end-of-string or another separator.
# This avoids matching mid-word letter pairs like "BC" in "BATCH".
# ---------------------------------------------------------------------------
_ABBREV_RE = re.compile(r"(?:^|[,\s|/])([A-Z]{2})(?=\s*(?:[,\s|/]|$))")


def derive_country(location: str) -> str | None:
    """
    Infer "CA" (Canada) or "US" (United States) from a free-text location
    string, or return None if the country cannot be determined confidently.

    Errs conservative: ambiguous names like bare "London" or "Remote" → None.
    """
    if not location or not location.strip():
        return None

    loc = location.strip()
    loc_lower = loc.lower()

    # 1. Country-level keywords (strongest signal, checked first).
    if re.search(r"\bcanada\b", loc_lower):
        return "CA"
    if re.search(r"\b(united states|u\.s\.a\.)\b", loc_lower):
        return "US"
    if re.search(r"\busa\b", loc_lower):
        return "US"

    # 2. Province / state abbreviations in context.
    #    Check Canadian provinces before US states so a CA province abbrev
    #    is never shadowed by a US state of the same letters (there is no
    #    overlap between the two sets, but ordering makes intent clear).
    for abbrev in _ABBREV_RE.findall(loc):
        if abbrev in _CA_PROVINCE_ABBREVS:
            return "CA"
        if abbrev in _US_STATE_ABBREVS:
            return "US"

    # 3. Province / state full names.
    for name in _CA_PROVINCE_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", loc_lower):
            return "CA"
    for name in _US_STATE_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", loc_lower):
            return "US"

    # 4. Unambiguous city names (lower-confidence than state signals).
    for city in _CA_CITIES:
        if re.search(r"\b" + re.escape(city) + r"\b", loc_lower):
            return "CA"
    for city in _US_CITIES:
        if re.search(r"\b" + re.escape(city) + r"\b", loc_lower):
            return "US"

    return None
