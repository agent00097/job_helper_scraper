"""
URL parsing for auto-discovery of company + source from a job posting URL.

parse_company_from_url(url) -> {"source", "company_endpoint", "company_name"} | None

This module owns the canonical hostname → source routing logic.
scrape_request_service.try_enrich_by_url imports source_name_for_url() from here
so the routing table lives in exactly one place.
"""
from __future__ import annotations

from urllib.parse import urlparse


def source_name_for_url(url: str) -> str | None:
    """Return the source name for a job URL, or None if unrecognised.

    Canonical routing table — the single source of truth shared by
    try_enrich_by_url and parse_company_from_url.
    """
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


def parse_company_from_url(url: str) -> dict | None:
    """Parse a job posting URL into source metadata.

    Returns a dict with keys:
      "source"           — one of "ashby", "lever", "greenhouse", "workday"
      "company_endpoint" — the value stored in source_companies.company_endpoint
      "company_name"     — human-readable name derived from the URL slug

    Returns None for unrecognised or malformed URLs.

    Supported patterns
    ------------------
    Ashby:
      https://jobs.ashbyhq.com/{company}/...
        → company_endpoint = "{company}"

    Lever:
      https://jobs.lever.co/{company}/...
        → company_endpoint = "{company}"

    Greenhouse:
      https://boards.greenhouse.io/{company}/jobs/{id}
      https://job-boards.greenhouse.io/{company}/jobs/{id}
        → company_endpoint = "{company}"
      https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{id}
        → company_endpoint = "{company}"

    Workday:
      https://{tenant}.{dc}.myworkdayjobs.com/{site}/job/...
        → company_endpoint = "https://{tenant}.{dc}.myworkdayjobs.com/{site}"
        → company_name     = "{tenant}" title-cased
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    source = source_name_for_url(url)
    if source is None:
        return None

    hostname = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")

    if source == "ashby":
        company = path.split("/")[0] if path else ""
        if not company:
            return None
        return {
            "source": "ashby",
            "company_endpoint": company,
            "company_name": company.replace("-", " ").title(),
        }

    if source == "lever":
        company = path.split("/")[0] if path else ""
        if not company:
            return None
        return {
            "source": "lever",
            "company_endpoint": company,
            "company_name": company.replace("-", " ").title(),
        }

    if source == "greenhouse":
        if hostname == "boards-api.greenhouse.io":
            # Path: v1/boards/{company}/jobs/{id}
            parts = path.split("/")
            company = parts[2] if len(parts) > 2 else ""
        else:
            # Path: {company}/jobs/{id}
            company = path.split("/")[0] if path else ""
        if not company:
            return None
        return {
            "source": "greenhouse",
            "company_endpoint": company,
            "company_name": company.replace("-", " ").title(),
        }

    if source == "workday":
        return _parse_workday(parsed)

    return None  # unreachable, but satisfies type checkers


def _parse_workday(parsed) -> dict | None:
    """Extract Workday board URL and tenant name from a job page URL."""
    hostname = parsed.hostname or ""
    parts = hostname.split(".")
    # Expected: {tenant}.{dc}.myworkdayjobs.com  (4 dot-separated parts)
    if len(parts) < 4 or parts[2] != "myworkdayjobs":
        return None
    tenant = parts[0]
    dc = parts[1]

    # Isolate the site path (everything before /job/ if present).
    full_path = parsed.path
    job_marker = full_path.find("/job/")
    site = (full_path[:job_marker] if job_marker != -1 else full_path).strip("/")
    if not site:
        return None

    company_endpoint = f"https://{tenant}.{dc}.myworkdayjobs.com/{site}"
    return {
        "source": "workday",
        "company_endpoint": company_endpoint,
        "company_name": tenant.replace("-", " ").title(),
    }
