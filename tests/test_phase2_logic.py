"""
Standalone tests for Phase 2 pure-logic components — no database required.

Covers:
  1. utils.geo.derive_country
  2. utils.company_discovery.parse_company_from_url
  3. WorkdaySource._resolve_location

Run from repo root:
    python tests/test_phase2_logic.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.geo import derive_country, parse_location
from utils.company_discovery import parse_company_from_url
from sources.api.workday_source import WorkdaySource

# ---------------------------------------------------------------------------
# Minimal test harness
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0


def check(label: str, got, expected) -> None:
    global _passed, _failed
    if got == expected:
        print(f"  PASS  {label}")
        _passed += 1
    else:
        print(f"  FAIL  {label}")
        print(f"        expected : {expected!r}")
        print(f"        got      : {got!r}")
        _failed += 1


# ---------------------------------------------------------------------------
# 1. derive_country
# ---------------------------------------------------------------------------

print("\n── 1. derive_country ──────────────────────────────────────────────")

COUNTRY_CASES = [
    # (input,             expected)
    ("Toronto, ON",       "CA"),
    ("New York, NY",      "US"),
    ("Vancouver, BC",     "CA"),
    ("Remote",            None),
    ("London",            None),   # ambiguous — London UK is more common
    ("London, ON",        "CA"),
    ("Austin, Texas",     "US"),
    ("Montreal, QC",      "CA"),
    ("6 Locations",       None),
    ("Europe",            None),
    ("China (Remote)",    "CN"),
    ("Remote - India",    "IN"),
    ("London, UK",        "GB"),
    ("Bengaluru, India",  "IN"),
    ("Georgia",           "US"),   # US state wins over the country
    ("Lebanon, PA",       "US"),   # US city wins over the country
]

for location, expected in COUNTRY_CASES:
    check(f"derive_country({location!r})", derive_country(location), expected)

# ---------------------------------------------------------------------------
# 2. parse_company_from_url
# ---------------------------------------------------------------------------

print("\n── 2. parse_company_from_url ──────────────────────────────────────")

URL_CASES = [
    (
        "Ashby — linear",
        "https://jobs.ashbyhq.com/linear/d3bc1ced-3ce4-4086-a050-555055dbb1ff",
        {"source": "ashby", "company_endpoint": "linear", "company_name": "Linear"},
    ),
    (
        "Lever — spotify",
        "https://jobs.lever.co/spotify/1ff4a4e3-897c-4eab-9ee2-aa7d1d07a9d6",
        {"source": "lever", "company_endpoint": "spotify", "company_name": "Spotify"},
    ),
    (
        "Greenhouse boards.greenhouse.io — airbnb",
        "https://boards.greenhouse.io/airbnb/jobs/4688232003",
        {"source": "greenhouse", "company_endpoint": "airbnb", "company_name": "Airbnb"},
    ),
    (
        "Greenhouse boards-api.greenhouse.io — airbnb",
        "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs/4688232003",
        {"source": "greenhouse", "company_endpoint": "airbnb", "company_name": "Airbnb"},
    ),
    (
        "Workday — salesforce",
        "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/Illinois---Chicago/Success-Architect---Service-Cloud_JR343225",
        {
            "source": "workday",
            "company_endpoint": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
            "company_name": "Salesforce",
        },
    ),
    (
        "Unknown → None",
        "https://example.com/job/123",
        None,
    ),
]

for label, url, expected in URL_CASES:
    check(label, parse_company_from_url(url), expected)

# ---------------------------------------------------------------------------
# 3. WorkdaySource._resolve_location
# ---------------------------------------------------------------------------

print("\n── 3. _resolve_location ───────────────────────────────────────────")

_source = WorkdaySource(name="workday", source_id="test", config={}, rate_limit_per_minute=60)

# Shared mock detail with primary + two additional locations.
_multi_detail = {
    "jobPostingInfo": {
        "jobRequisitionLocation": {"descriptor": "Chicago, IL"},
        "additionalLocations": [
            {"descriptor": "New York, NY"},
            {"descriptor": "San Francisco, CA"},
        ],
    }
}

# Mock detail with only a primary location.
_single_detail = {
    "jobPostingInfo": {
        "jobRequisitionLocation": {"descriptor": "Austin, TX"},
        "additionalLocations": [],
    }
}

# Mock detail used only as the fallback resolver when locations_text is None.
_fallback_detail = {
    "jobPostingInfo": {
        "jobRequisitionLocation": {"descriptor": "Seattle, WA"},
        "additionalLocations": [],
    }
}

check(
    '"6 Locations" + multi-location detail → joined real locations',
    _source._resolve_location("6 Locations", _multi_detail),
    "Chicago, IL; New York, NY; San Francisco, CA",
)

check(
    '"1 Location" (singular) + single-location detail → one place',
    _source._resolve_location("1 Location", _single_detail),
    "Austin, TX",
)

check(
    'real place name → returned unchanged (detail is ignored)',
    _source._resolve_location("New York, NY", _multi_detail),
    "New York, NY",
)

check(
    'None + detail → primary location from jobRequisitionLocation',
    _source._resolve_location(None, _fallback_detail),
    "Seattle, WA",
)

check(
    # Count string kept as last-resort fallback when detail unavailable.
    '"6 Locations" + no detail → count string preserved',
    _source._resolve_location("6 Locations", None),
    "6 Locations",
)

check(
    'None + no detail → None',
    _source._resolve_location(None, None),
    None,
)

# ---------------------------------------------------------------------------
# 4. parse_location
# ---------------------------------------------------------------------------

print("\n── 4. parse_location ──────────────────────────────────────────────")

PARSE_CASES = [
    ("Sacramento, CA", ("US", "CA", "Sacramento", "locality")),
    ("California", ("US", "CA", None, "admin1")),
    ("Toronto, ON", ("CA", "ON", "Toronto", "locality")),
    ("New York, NY", ("US", "NY", "New York", "locality")),
    ("Remote", (None, None, None, "unknown")),
    ("Remote - United States", ("US", None, None, "country")),
    ("Austin, Texas", ("US", "TX", "Austin", "locality")),
    ("British Columbia", ("CA", "BC", None, "admin1")),
    ("San Francisco Bay Area", ("US", "CA", "San Francisco Bay Area", "locality")),
    ("China (Remote)", ("CN", None, None, "country")),
    ("Shanghai, China", ("CN", None, "Shanghai", "locality")),
    ("Remote - United Kingdom", ("GB", None, None, "country")),
]

for location, (country, admin1, locality, precision) in PARSE_CASES:
    got = parse_location(location)
    check(
        f"parse_location({location!r}).country",
        got.country_code,
        country,
    )
    check(
        f"parse_location({location!r}).admin1",
        got.admin1_code,
        admin1,
    )
    check(
        f"parse_location({location!r}).locality",
        got.locality,
        locality,
    )
    check(
        f"parse_location({location!r}).precision",
        got.geo_precision,
        precision,
    )

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

total = _passed + _failed
print(f"\n{'=' * 50}")
status = "ALL PASSED" if _failed == 0 else f"{_failed} FAILED"
print(f"Results: {_passed}/{total} passed  —  {status}")
print("=" * 50)

if _failed:
    sys.exit(1)
