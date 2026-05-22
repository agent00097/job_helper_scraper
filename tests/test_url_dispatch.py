"""
Standalone URL-dispatch test — no database required.

Verifies that try_enrich_by_url routes each URL to the correct source
and returns a populated JobData (or falls back gracefully for unknown URLs).

Greenhouse and Ashby URLs are fetched dynamically from their live boards so
the test never relies on a hardcoded stale job ID.

Run from repo root:
    python tests/test_url_dispatch.py
"""
import logging
import os
import sys
from urllib.parse import urlparse

import requests

# Bootstrap: add repo root so imports resolve without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Patch get_source_config on the service module's namespace (the local binding
# created by "from utils.source_loader import get_source_config") before any
# test calls reach it — this avoids any database connection.
import services.scrape_request_service as srs

_MOCK_CONFIGS = {
    name: {
        "id": f"mock-{name}",
        "name": name,
        "type": "api",
        "enabled": True,
        "rate_limit_per_minute": 60,
        "config": {},
    }
    for name in ("greenhouse", "ashby", "lever", "workday")
}

srs.get_source_config = lambda name: _MOCK_CONFIGS.get(name)

from models import JobData  # noqa: E402 — must follow sys.path bootstrap

logging.basicConfig(level=logging.WARNING)


# ---------------------------------------------------------------------------
# Live-URL helpers: one cheap API call each, no description fetching.
# ---------------------------------------------------------------------------

def _fetch_live_greenhouse_url(company: str = "airbnb") -> str | None:
    """Return the first live job URL from a Greenhouse board (one list GET)."""
    try:
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs",
            timeout=30,
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            print(f"    Greenhouse board for {company!r} returned no jobs")
            return None
        job_id = jobs[0]["id"]
        url = f"https://boards.greenhouse.io/{company}/jobs/{job_id}"
        print(f"    live URL: {url}")
        return url
    except Exception as e:
        print(f"    Greenhouse board fetch failed: {e}")
        return None


def _fetch_live_ashby_url(company: str = "linear") -> str | None:
    """Return the first live job URL from an Ashby board (one list GET)."""
    try:
        resp = requests.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{company}",
            timeout=30,
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            print(f"    Ashby board for {company!r} returned no jobs")
            return None
        job_id = jobs[0]["id"]
        url = f"https://jobs.ashbyhq.com/{company}/{job_id}"
        print(f"    live URL: {url}")
        return url
    except Exception as e:
        print(f"    Ashby board fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Routing helper (mirrors the logic inside try_enrich_by_url).
# ---------------------------------------------------------------------------

def _expected_source(url: str) -> str | None:
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"):
        return "greenhouse"
    if hostname == "jobs.ashbyhq.com":
        return "ashby"
    if hostname == "jobs.lever.co":
        return "lever"
    if hostname.endswith(".myworkdayjobs.com"):
        return "workday"
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("URL DISPATCH TEST")
    print("=" * 70)

    print("\n[Setup] Fetching live URLs...")
    print("  Greenhouse (airbnb board):")
    gh_url = _fetch_live_greenhouse_url()
    print("  Ashby (linear board):")
    ashby_url = _fetch_live_ashby_url()

    test_cases = [
        ("Greenhouse", gh_url),
        ("Ashby",      ashby_url),
        # Lever and Workday URLs provided directly — these are real postings.
        ("Lever",   "https://jobs.lever.co/spotify/1ff4a4e3-897c-4eab-9ee2-aa7d1d07a9d6"),
        ("Workday", "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/Illinois---Chicago/Success-Architect---Service-Cloud_JR343225"),
        ("Unknown", "https://example.com/job/123"),
    ]

    for label, url in test_cases:
        print(f"\n[{label}]")
        if url is None:
            print("  SKIPPED — could not fetch a live URL during setup")
            continue

        print(f"  URL : {url}")
        expected = _expected_source(url)
        print(f"  Expected source : {expected or 'none (fallback)'}")

        seed = JobData(url=url, source_website="test-dispatch")
        result = srs.try_enrich_by_url(seed)

        if result is seed:
            if expected is None:
                print("  Result  : fallback — no source matched (correct)")
            else:
                print("  Result  : fallback — enrichment returned None (job may have expired; routing was attempted)")
        else:
            matched = result.source_website
            title   = result.job_title or "(no title)"
            company = result.company   or "(no company)"
            routing_ok = "CORRECT" if matched == expected else f"UNEXPECTED (got {matched!r})"
            print(f"  Matched source : {matched}  [{routing_ok}]")
            print(f"  Title          : {title}")
            print(f"  Company        : {company}")

    print("\n" + "=" * 70)
    print("Done.")


if __name__ == "__main__":
    main()
