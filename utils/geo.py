"""
Pure-function geographic inference from free-text location strings (US + CA).

derive_country(location) -> "CA" | "US" | None
parse_location(location) -> GeoParts (country, admin1, locality, precision)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional


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

_CA_NAME_TO_CODE: dict[str, str] = {
    "ontario": "ON",
    "quebec": "QC",
    "québec": "QC",
    "british columbia": "BC",
    "alberta": "AB",
    "manitoba": "MB",
    "saskatchewan": "SK",
    "nova scotia": "NS",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "newfoundland": "NL",
    "labrador": "NL",
    "prince edward island": "PE",
    "northwest territories": "NT",
    "yukon": "YT",
    "nunavut": "NU",
}

_CA_CODE_TO_NAME: dict[str, str] = {
    "ON": "Ontario",
    "QC": "Quebec",
    "BC": "British Columbia",
    "AB": "Alberta",
    "MB": "Manitoba",
    "SK": "Saskatchewan",
    "NS": "Nova Scotia",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "PE": "Prince Edward Island",
    "NT": "Northwest Territories",
    "YT": "Yukon",
    "NU": "Nunavut",
}

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
    "district of columbia",
})

_US_STATE_ABBREVS: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})

_US_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

_US_CODE_TO_NAME: dict[str, str] = {
    code: name.title() if name != "district of columbia" else "District of Columbia"
    for name, code in _US_NAME_TO_CODE.items()
}
_US_CODE_TO_NAME["DC"] = "District of Columbia"

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

_CITY_COUNTRY: dict[str, str] = {c: "CA" for c in _CA_CITIES}
_CITY_COUNTRY.update({c: "US" for c in _US_CITIES})

# Cities that imply a default admin1 when bare.
_CITY_ADMIN1: dict[str, tuple[str, str]] = {
    "toronto": ("CA", "ON"),
    "montreal": ("CA", "QC"),
    "montréal": ("CA", "QC"),
    "vancouver": ("CA", "BC"),
    "calgary": ("CA", "AB"),
    "edmonton": ("CA", "AB"),
    "ottawa": ("CA", "ON"),
    "winnipeg": ("CA", "MB"),
    "new york": ("US", "NY"),
    "new york city": ("US", "NY"),
    "nyc": ("US", "NY"),
    "los angeles": ("US", "CA"),
    "san francisco": ("US", "CA"),
    "san jose": ("US", "CA"),
    "sacramento": ("US", "CA"),
    "san diego": ("US", "CA"),
    "seattle": ("US", "WA"),
    "chicago": ("US", "IL"),
    "boston": ("US", "MA"),
    "austin": ("US", "TX"),
    "dallas": ("US", "TX"),
    "houston": ("US", "TX"),
    "denver": ("US", "CO"),
    "miami": ("US", "FL"),
    "atlanta": ("US", "GA"),
    "washington dc": ("US", "DC"),
    "washington, dc": ("US", "DC"),
    "bay area": ("US", "CA"),
    "san francisco bay area": ("US", "CA"),
    "silicon valley": ("US", "CA"),
}

# ---------------------------------------------------------------------------
# Countries outside the US/CA market.
#
# Only consulted after every US/CA rule has failed, so names that collide with
# North American places ("Georgia", "Lebanon, PA") resolve to US/CA first.
# Two-letter aliases are limited to codes that are not US state or Canadian
# province abbreviations (e.g. no "IN" for India — that is Indiana).
# ---------------------------------------------------------------------------

_FOREIGN_COUNTRIES: dict[str, str] = {
    # Europe
    "united kingdom": "GB", "uk": "GB", "u.k.": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "ireland": "IE", "france": "FR", "germany": "DE", "deutschland": "DE",
    "spain": "ES", "portugal": "PT", "italy": "IT", "netherlands": "NL",
    "belgium": "BE", "luxembourg": "LU", "switzerland": "CH", "austria": "AT",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "iceland": "IS", "poland": "PL", "czechia": "CZ", "czech republic": "CZ",
    "slovakia": "SK", "hungary": "HU", "romania": "RO", "bulgaria": "BG",
    "greece": "GR", "croatia": "HR", "serbia": "RS", "slovenia": "SI",
    "estonia": "EE", "latvia": "LV", "lithuania": "LT", "ukraine": "UA",
    "russia": "RU", "belarus": "BY", "cyprus": "CY", "malta": "MT",
    # Asia-Pacific
    "china": "CN", "hong kong": "HK", "taiwan": "TW", "japan": "JP",
    "south korea": "KR", "korea": "KR", "singapore": "SG", "malaysia": "MY",
    "indonesia": "ID", "thailand": "TH", "vietnam": "VN", "viet nam": "VN",
    "philippines": "PH", "india": "IN", "pakistan": "PK", "bangladesh": "BD",
    "sri lanka": "LK", "nepal": "NP", "australia": "AU", "new zealand": "NZ",
    # Middle East / Africa
    "israel": "IL", "turkey": "TR", "türkiye": "TR",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA",
    "qatar": "QA", "kuwait": "KW", "bahrain": "BH", "oman": "OM",
    "jordan": "JO", "lebanon": "LB", "egypt": "EG", "morocco": "MA",
    "tunisia": "TN", "nigeria": "NG", "ghana": "GH", "kenya": "KE",
    "south africa": "ZA", "ethiopia": "ET",
    # Americas (excluding US/CA)
    "mexico": "MX", "brazil": "BR", "brasil": "BR", "argentina": "AR",
    "chile": "CL", "colombia": "CO", "peru": "PE", "uruguay": "UY",
    "paraguay": "PY", "bolivia": "BO", "ecuador": "EC", "venezuela": "VE",
    "costa rica": "CR", "panama": "PA", "guatemala": "GT", "honduras": "HN",
    "el salvador": "SV", "nicaragua": "NI", "dominican republic": "DO",
    "jamaica": "JM", "puerto rico": "PR",
}

_FOREIGN_COUNTRY_RE = re.compile(
    r"\b(" + "|".join(
        re.escape(name)
        for name in sorted(_FOREIGN_COUNTRIES, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)

_ABBREV_RE = re.compile(r"(?:^|[,\s|/])([A-Z]{2})(?=\s*(?:[,\s|/]|$))")
_MULTI_LOC_RE = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)
_REMOTE_RE = re.compile(
    r"\b(remote|work\s*from\s*home|wfh|anywhere|fully\s*remote)\b",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"\b(hybrid|hq|headquarters|metro\s*area|greater|region|"
    r"united\s*states|u\.s\.a\.|usa|canada|office)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GeoParts:
    country_code: Optional[str] = None
    admin1_code: Optional[str] = None
    admin1_name: Optional[str] = None
    locality: Optional[str] = None
    geo_precision: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_country(location: str) -> str | None:
    """
    Infer an ISO country code from a free-text location string, or return None
    if the country cannot be determined confidently.

    US/CA signals win over any other country, so "Georgia" is the state and
    "Lebanon, PA" is Pennsylvania. Errs conservative: ambiguous names like bare
    "London" or "Remote" → None.
    """
    return parse_location(location).country_code


def parse_location(location: Optional[str]) -> GeoParts:
    """
    Parse free-text ATS location into structured geography.

    admin1/locality are only resolved for the US/CA market; other countries get
    a country code so they can be filtered out of US/CA matching.

    Examples:
      "Sacramento, CA" -> locality=Sacramento, admin1=CA, country=US
      "California"     -> admin1=CA, country=US, precision=admin1
      "Toronto, ON"    -> locality=Toronto, admin1=ON, country=CA
      "Remote - US"    -> country=US, precision=country
      "China (Remote)" -> country=CN, precision=country
    """
    if not location or not str(location).strip():
        return GeoParts()

    raw = str(location).strip()
    if _MULTI_LOC_RE.match(raw):
        return GeoParts()

    raw_lower = raw.lower()

    # Remote / anywhere with optional country (check full string before splitting).
    if _REMOTE_RE.search(raw_lower):
        country = _country_keyword(raw_lower)
        # If there is also a concrete place ("Remote - New York, NY"), keep parsing.
        remainder = _REMOTE_RE.sub(" ", raw)
        remainder = re.sub(r"[\-–—|/]+", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip(" ,")
        if not remainder or not _looks_like_place(remainder):
            if country:
                return GeoParts(country_code=country, geo_precision="country")
            return GeoParts()
        primary = remainder
    else:
        # Multi-office strings: take the first segment.
        primary = re.split(r"[;|/]| - | – | — ", raw, maxsplit=1)[0].strip()
        if not primary:
            primary = raw

    loc_lower = primary.lower()

    # Bare country.
    if re.fullmatch(r"(united states|u\.s\.a\.|usa|us|canada)", loc_lower):
        country = "CA" if "canada" in loc_lower else "US"
        return GeoParts(country_code=country, geo_precision="country")

    # Bare state / province full name.
    for name, code in _US_NAME_TO_CODE.items():
        if re.fullmatch(re.escape(name), loc_lower):
            return GeoParts(
                country_code="US",
                admin1_code=code,
                admin1_name=_US_CODE_TO_NAME.get(code),
                geo_precision="admin1",
            )
    for name, code in _CA_NAME_TO_CODE.items():
        if re.fullmatch(re.escape(name), loc_lower):
            return GeoParts(
                country_code="CA",
                admin1_code=code,
                admin1_name=_CA_CODE_TO_NAME.get(code),
                geo_precision="admin1",
            )

    # Bare 2-letter admin1.
    if re.fullmatch(r"[A-Za-z]{2}", primary):
        code = primary.upper()
        if code in _US_STATE_ABBREVS:
            return GeoParts(
                country_code="US",
                admin1_code=code,
                admin1_name=_US_CODE_TO_NAME.get(code),
                geo_precision="admin1",
            )
        if code in _CA_PROVINCE_ABBREVS:
            return GeoParts(
                country_code="CA",
                admin1_code=code,
                admin1_name=_CA_CODE_TO_NAME.get(code),
                geo_precision="admin1",
            )

    # "City, ST" / "City, State" / "City, ST, Country"
    parts = [p.strip() for p in primary.split(",") if p.strip()]
    if len(parts) >= 2:
        locality = _clean_locality(parts[0])
        admin_raw = parts[1]
        admin_code, country, admin_name = _resolve_admin(admin_raw)
        if not country and len(parts) >= 3:
            country = _country_keyword(parts[2].lower()) or derive_country_fast(parts[2])
        if admin_code:
            if not country:
                country = "US" if admin_code in _US_STATE_ABBREVS else "CA"
            precision = "locality" if locality else "admin1"
            return GeoParts(
                country_code=country,
                admin1_code=admin_code,
                admin1_name=admin_name,
                locality=locality or None,
                geo_precision=precision,
            )
        # Second token wasn't admin1 — maybe "City, Country"
        country = (
            _country_keyword(admin_raw.lower())
            or derive_country_fast(admin_raw)
            or _foreign_country(admin_raw)
        )
        if locality and country:
            hint = _CITY_ADMIN1.get(locality.lower())
            if hint and hint[0] == country:
                return GeoParts(
                    country_code=country,
                    admin1_code=hint[1],
                    admin1_name=_admin_name(hint[0], hint[1]),
                    locality=locality,
                    geo_precision="locality",
                )
            return GeoParts(
                country_code=country,
                locality=locality,
                geo_precision="locality",
            )

    # Single token / phrase: known city or contains city+state.
    cleaned = _NOISE_RE.sub(" ", primary)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    cleaned_lower = cleaned.lower()

    # Abbrev in string without comma (e.g. "Sacramento CA")
    abbrevs = _ABBREV_RE.findall(cleaned)
    for abbrev in abbrevs:
        if abbrev in _CA_PROVINCE_ABBREVS or abbrev in _US_STATE_ABBREVS:
            country = "CA" if abbrev in _CA_PROVINCE_ABBREVS else "US"
            locality = _clean_locality(
                re.sub(rf"(?:^|[,\s|/]){abbrev}(?=\s*(?:[,\s|/]|$))", " ", cleaned)
            )
            return GeoParts(
                country_code=country,
                admin1_code=abbrev,
                admin1_name=_admin_name(country, abbrev),
                locality=locality or None,
                geo_precision="locality" if locality else "admin1",
            )

    # Known city names (longest first).
    for city in sorted(_CITY_COUNTRY.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(city) + r"\b", cleaned_lower):
            country = _CITY_COUNTRY[city]
            hint = _CITY_ADMIN1.get(city)
            admin1 = hint[1] if hint else None
            display_city = city.title() if city not in {"nyc", "bay area"} else {
                "nyc": "New York",
                "bay area": "Bay Area",
                "san francisco bay area": "San Francisco Bay Area",
                "silicon valley": "Silicon Valley",
                "washington dc": "Washington",
                "washington, dc": "Washington",
            }.get(city, city.title())
            return GeoParts(
                country_code=country,
                admin1_code=admin1,
                admin1_name=_admin_name(country, admin1) if admin1 else None,
                locality=display_city,
                geo_precision="locality",
            )

    # Full state/province name embedded.
    for name, code in sorted(_US_NAME_TO_CODE.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", cleaned_lower):
            return GeoParts(
                country_code="US",
                admin1_code=code,
                admin1_name=_US_CODE_TO_NAME.get(code),
                geo_precision="admin1",
            )
    for name, code in sorted(_CA_NAME_TO_CODE.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", cleaned_lower):
            return GeoParts(
                country_code="CA",
                admin1_code=code,
                admin1_name=_CA_CODE_TO_NAME.get(code),
                geo_precision="admin1",
            )

    country = (
        _country_keyword(cleaned_lower)
        or derive_country_fast(cleaned)
        or _foreign_country(cleaned)
    )
    if country:
        return GeoParts(country_code=country, geo_precision="country")
    return GeoParts()


def derive_country_fast(location: str) -> Optional[str]:
    """Lightweight country-only scan (used as helper inside parse_location)."""
    if not location or not location.strip():
        return None
    loc = location.strip()
    loc_lower = loc.lower()
    if re.search(r"\bcanada\b", loc_lower):
        return "CA"
    if re.search(r"\b(united states|u\.s\.a\.)\b", loc_lower):
        return "US"
    if re.search(r"\busa\b", loc_lower):
        return "US"
    for abbrev in _ABBREV_RE.findall(loc):
        if abbrev in _CA_PROVINCE_ABBREVS:
            return "CA"
        if abbrev in _US_STATE_ABBREVS:
            return "US"
    for name in _CA_PROVINCE_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", loc_lower):
            return "CA"
    for name in _US_STATE_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", loc_lower):
            return "US"
    for city in _CA_CITIES:
        if re.search(r"\b" + re.escape(city) + r"\b", loc_lower):
            return "CA"
    for city in _US_CITIES:
        if re.search(r"\b" + re.escape(city) + r"\b", loc_lower):
            return "US"
    return None


def _foreign_country(text: str) -> Optional[str]:
    """ISO code for a non-US/CA country named in `text`, else None."""
    if not text or not text.strip():
        return None
    match = _FOREIGN_COUNTRY_RE.search(text)
    return _FOREIGN_COUNTRIES[match.group(1).lower()] if match else None


def _country_keyword(loc_lower: str) -> Optional[str]:
    if re.search(r"\bcanada\b", loc_lower):
        return "CA"
    if re.search(r"\b(united states|u\.s\.a\.|usa|\bus\b)\b", loc_lower):
        return "US"
    return None


def _looks_like_place(text: str) -> bool:
    """True if text has more than just remote keywords (e.g. 'Remote - New York, NY')."""
    stripped = _REMOTE_RE.sub(" ", text)
    stripped = _NOISE_RE.sub(" ", stripped)
    stripped = re.sub(r"[^A-Za-z]", " ", stripped)
    return bool(stripped.strip())


def _clean_locality(text: str) -> str:
    cleaned = _REMOTE_RE.sub(" ", text)
    cleaned = _NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned


def _resolve_admin(token: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    t = token.strip()
    t_lower = t.lower()
    # Strip trailing country words: "CA USA", "ON Canada"
    t_lower = re.sub(r"\b(united states|u\.s\.a\.|usa|us|canada)\b", "", t_lower).strip()
    t_compact = re.sub(r"[^A-Za-z ]", "", t_lower).strip()

    if re.fullmatch(r"[A-Za-z]{2}", t_compact):
        code = t_compact.upper()
        if code in _US_STATE_ABBREVS:
            return code, "US", _US_CODE_TO_NAME.get(code)
        if code in _CA_PROVINCE_ABBREVS:
            return code, "CA", _CA_CODE_TO_NAME.get(code)

    if t_compact in _US_NAME_TO_CODE:
        code = _US_NAME_TO_CODE[t_compact]
        return code, "US", _US_CODE_TO_NAME.get(code)
    if t_compact in _CA_NAME_TO_CODE:
        code = _CA_NAME_TO_CODE[t_compact]
        return code, "CA", _CA_CODE_TO_NAME.get(code)

    # Abbrev embedded in token
    for abbrev in _ABBREV_RE.findall(t):
        if abbrev in _US_STATE_ABBREVS:
            return abbrev, "US", _US_CODE_TO_NAME.get(abbrev)
        if abbrev in _CA_PROVINCE_ABBREVS:
            return abbrev, "CA", _CA_CODE_TO_NAME.get(abbrev)
    return None, None, None


def _admin_name(country: str, code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    if country == "US":
        return _US_CODE_TO_NAME.get(code)
    if country == "CA":
        return _CA_CODE_TO_NAME.get(code)
    return None
