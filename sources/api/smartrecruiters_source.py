"""
SmartRecruiters public Posting API source.

List (paginated, no job ad body):
  GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings
Detail (description, apply URL):
  GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings/{postingId}

company_endpoint is the case-sensitive career-site identifier, e.g. "Visa"
from https://jobs.smartrecruiters.com/Visa
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from models import JobData
from sources.base_source import BaseSource
from utils.deduplication import urls_with_existing_description
from utils.html_text import html_to_text
from utils.occupation_category import from_title
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

SMARTRECRUITERS_API_BASE = "https://api.smartrecruiters.com/v1/companies"
PAGE_SIZE = 100
MAX_PAGES = 200
# List responses have no job-ad body. First-run boards can have 600+ postings;
# a detail GET per job at 30 rpm exceeds COMPANY_SCRAPE_TIMEOUT_SECONDS and
# saves nothing. Cap details per run; later scrapes fill via selective skip.
DEFAULT_MAX_DETAIL_FETCHES = 50

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_NUMERIC_ID_RE = re.compile(r"^(\d+)(?:-|$)")
_JOB_HOSTS = frozenset({"jobs.smartrecruiters.com", "careers.smartrecruiters.com"})
_NOISE_SEGMENTS = frozenset({"external-referrals", "oneclick", "widget", "api", "www"})
_SECTION_ORDER = ("jobDescription", "qualifications", "additionalInformation")


def _posting_id_from_token(token: str) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    if _UUID_RE.match(value):
        return value
    match = _NUMERIC_ID_RE.match(value)
    if match:
        return match.group(1)
    return value


def parse_smartrecruiters_job_url(url: str) -> Optional[Tuple[str, str]]:
    """Return (company_identifier, posting_id) from a public SR job URL."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return None

    company = ""
    job_token = ""

    if hostname == "api.smartrecruiters.com":
        try:
            idx = parts.index("companies")
            company = parts[idx + 1]
            if len(parts) > idx + 3 and parts[idx + 2] == "postings":
                job_token = parts[idx + 3]
        except (ValueError, IndexError):
            return None
    elif hostname in _JOB_HOSTS:
        if parts[0] == "external-referrals":
            try:
                idx = parts.index("company")
                company = parts[idx + 1]
                if "publication" in parts:
                    pub = parts.index("publication")
                    if pub + 1 < len(parts):
                        job_token = parts[pub + 1]
            except (ValueError, IndexError):
                return None
        elif parts[0] not in _NOISE_SEGMENTS:
            company = parts[0]
            if len(parts) >= 2:
                job_token = parts[1]
    else:
        return None

    posting_id = _posting_id_from_token(job_token)
    if not company or not posting_id:
        return None
    return company, posting_id


def _format_location(location: object) -> Optional[str]:
    if not isinstance(location, dict):
        return None
    parts = []
    for key in ("city", "region", "country"):
        value = location.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return ", ".join(parts) if parts else None


def _public_job_url(company_endpoint: str, posting: Dict) -> str:
    job_id = posting.get("id")
    return f"https://jobs.smartrecruiters.com/{company_endpoint}/{job_id}"


class SmartRecruitersSource(BaseSource):
    """SmartRecruiters public Posting API source."""

    def __init__(self, name: str, source_id: str, config: Dict, rate_limit_per_minute: int):
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self.base_url = config.get("base_url", SMARTRECRUITERS_API_BASE).rstrip("/")

    def _max_detail_fetches(self) -> int:
        raw = self.config.get("max_detail_fetches_per_run", DEFAULT_MAX_DETAIL_FETCHES)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_MAX_DETAIL_FETCHES

    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        logger.info(
            "Fetching jobs from SmartRecruiters for %s (%s)",
            company_name,
            company_endpoint,
        )
        postings = self._fetch_all_postings(company_endpoint, company_name)
        if not postings:
            return []

        list_parsed: List[Tuple[Dict, JobData]] = []
        for posting in postings:
            try:
                job = self._parse_job(posting, company_name, company_endpoint, detail=None)
                if job:
                    list_parsed.append((posting, job))
            except Exception as exc:
                logger.warning(
                    "Error parsing SmartRecruiters job from %s: %s", company_name, exc
                )
                continue

        already_described = urls_with_existing_description(
            str(job.url) for _, job in list_parsed
        )
        pending: List[Tuple[Dict, JobData]] = []
        detail_skipped = 0
        for posting, list_job in list_parsed:
            if str(list_job.url) in already_described:
                detail_skipped += 1
            else:
                pending.append((posting, list_job))

        max_details = self._max_detail_fetches()
        to_enrich = pending[:max_details]
        deferred = pending[max_details:]
        enrich_urls = {str(job.url) for _, job in to_enrich}

        jobs: List[JobData] = []
        detail_fetched = 0
        for posting, list_job in list_parsed:
            url = str(list_job.url)
            if url not in enrich_urls:
                jobs.append(list_job)
                continue

            posting_id = list_job.job_id_from_source
            detail = self._fetch_detail(company_endpoint, posting_id) if posting_id else None
            detail_fetched += 1
            if not detail:
                jobs.append(list_job)
                continue
            try:
                enriched = self._parse_job(
                    posting, company_name, company_endpoint, detail=detail
                )
                jobs.append(enriched if enriched else list_job)
            except Exception as exc:
                logger.warning(
                    "Error enriching SmartRecruiters job %s from %s: %s",
                    posting_id,
                    company_name,
                    exc,
                )
                jobs.append(list_job)

        jobs_with_desc = sum(1 for job in jobs if job.job_description)
        logger.info(
            "Fetched %d jobs from %s "
            "(detail fetched %d, skipped %d, deferred %d; "
            "%d with descriptions this run, %d without)",
            len(jobs),
            company_name,
            detail_fetched,
            detail_skipped,
            len(deferred),
            jobs_with_desc,
            len(jobs) - jobs_with_desc,
        )
        return jobs

    def _fetch_all_postings(self, company_endpoint: str, company_name: str) -> List[Dict]:
        all_postings: List[Dict] = []
        offset = 0

        for _page in range(MAX_PAGES):
            self.rate_limiter.wait_if_needed()
            url = f"{self.base_url}/{company_endpoint}/postings"
            try:
                response = requests.get(
                    url,
                    params={"limit": PAGE_SIZE, "offset": offset},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "SmartRecruiters list fetch failed at offset %d for %s: %s",
                    offset,
                    company_name,
                    exc,
                )
                # Never return a partial page set — presence reconcile would
                # incorrectly archive jobs on later pages.
                raise

            if not isinstance(data, dict):
                raise ValueError(
                    f"Unexpected SmartRecruiters list shape for {company_name}: "
                    f"{type(data).__name__}"
                )

            page = data.get("content") or []
            if not isinstance(page, list):
                raise ValueError(
                    f"Unexpected SmartRecruiters content shape for {company_name}: "
                    f"{type(page).__name__}"
                )
            all_postings.extend(page)
            total = data.get("totalFound") or 0
            logger.debug(
                "SmartRecruiters %s: fetched %d/%s",
                company_name,
                len(all_postings),
                total,
            )
            if not page:
                break
            if isinstance(total, int) and total > 0 and len(all_postings) >= total:
                break
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        else:
            logger.warning(
                "SmartRecruiters %s: hit MAX_PAGES=%d with %d postings",
                company_name,
                MAX_PAGES,
                len(all_postings),
            )

        return all_postings

    def _fetch_detail(self, company_endpoint: str, posting_id: str) -> Optional[Dict]:
        self.rate_limiter.wait_if_needed()
        url = f"{self.base_url}/{company_endpoint}/postings/{posting_id}"
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "SmartRecruiters detail fetch failed for %s/%s: %s",
                company_endpoint,
                posting_id,
                exc,
            )
            return None
        return data if isinstance(data, dict) else None

    def _parse_job(
        self,
        posting: Dict,
        company_name: str,
        company_endpoint: str,
        detail: Optional[Dict] = None,
    ) -> Optional[JobData]:
        payload = dict(posting)
        if detail:
            payload.update(detail)

        job_id = payload.get("id")
        if not job_id:
            raise ValueError("SmartRecruiters posting missing id")

        location_obj = payload.get("location") if isinstance(payload.get("location"), dict) else {}
        location = _format_location(location_obj)
        remote_allowed = bool(location_obj.get("remote")) if location_obj else False
        hybrid_allowed = bool(location and "hybrid" in location.lower())
        if not remote_allowed and location and "remote" in location.lower():
            remote_allowed = True

        employment = payload.get("typeOfEmployment")
        employment_type = None
        if isinstance(employment, dict):
            label = employment.get("label")
            if isinstance(label, str) and label.strip():
                employment_type = label.strip()
        elif isinstance(employment, str) and employment.strip():
            employment_type = employment.strip()

        date_posted = None
        released = payload.get("releasedDate")
        if isinstance(released, str) and released:
            try:
                date_posted = datetime.fromisoformat(released.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                pass

        description = self._description_from_job_ad(payload.get("jobAd")) if detail else None

        job_url = _public_job_url(company_endpoint, payload)
        apply_url = payload.get("applyUrl")
        application_url = (
            apply_url.strip()
            if isinstance(apply_url, str) and apply_url.strip()
            else job_url
        )

        resolved_company = company_name
        company_obj = payload.get("company")
        if isinstance(company_obj, dict):
            api_name = company_obj.get("name")
            if isinstance(api_name, str) and api_name.strip():
                resolved_company = api_name.strip()

        title = payload.get("name")
        return JobData(
            url=job_url,
            job_title=title,
            company=resolved_company,
            location=location,
            job_description=description,
            date_posted=date_posted,
            employment_type=employment_type,
            application_url=application_url,
            remote_allowed=remote_allowed,
            hybrid_allowed=hybrid_allowed,
            source_website=self.name,
            job_id_from_source=str(job_id),
            status="active",
            scraped_at=datetime.now(),
            created_at=datetime.now(),
            occupation_category=from_title(title or ""),
        )

    def _description_from_job_ad(self, job_ad: object) -> Optional[str]:
        if not isinstance(job_ad, dict):
            return None
        sections = job_ad.get("sections") or {}
        if not isinstance(sections, dict):
            return None
        parts: List[str] = []
        for key in _SECTION_ORDER:
            section = sections.get(key) or {}
            if not isinstance(section, dict):
                continue
            title = (section.get("title") or "").strip()
            text = (section.get("text") or "").strip()
            if not text:
                continue
            cleaned = self._clean_html(text)
            if not cleaned:
                continue
            parts.append(f"{title}\n{cleaned}" if title else cleaned)
        return "\n\n".join(parts) if parts else None

    def fetch_job_by_url(self, url: str) -> Optional[JobData]:
        parsed = parse_smartrecruiters_job_url(url)
        if not parsed:
            return None
        company_endpoint, posting_id = parsed
        detail = self._fetch_detail(company_endpoint, posting_id)
        if not detail:
            return None
        company_name = company_endpoint.replace("-", " ").title()
        try:
            return self._parse_job(detail, company_name, company_endpoint, detail=detail)
        except Exception as exc:
            logger.warning("SmartRecruiters _parse_job failed for %s: %s", url, exc, exc_info=True)
            return None

    def _clean_html(self, html_content: str) -> str:
        return html_to_text(html_content)

    def get_rate_limit(self) -> int:
        return self.rate_limiter.requests_per_minute
