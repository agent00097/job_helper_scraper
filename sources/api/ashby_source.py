"""
Ashby API source for fetching jobs from Ashby job boards.
"""
import logging
import requests
from typing import List, Dict, Optional
from datetime import datetime
from urllib.parse import urlparse

from sources.base_source import BaseSource
from models import JobData
from utils.html_text import html_to_text
from utils.occupation_category import from_title
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

ASHBY_POSTING_API = "https://api.ashbyhq.com/posting-api/job-board"


class AshbySource(BaseSource):
    """Ashby public posting-API source implementation."""

    def __init__(self, name: str, source_id: str, config: Dict, rate_limit_per_minute: int):
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self.base_url = config.get("base_url", ASHBY_POSTING_API)

    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        self.rate_limiter.wait_if_needed()
        api_url = f"{self.base_url}/{company_endpoint}"

        try:
            logger.info(f"Fetching jobs from Ashby for {company_name} ({company_endpoint})")
            response = requests.get(api_url, timeout=30)
            response.raise_for_status()

            data = response.json()
            jobs = []
            for job_data in data.get("jobs", []):
                try:
                    job = self._parse_job(job_data, company_name, company_endpoint)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning(f"Error parsing Ashby job from {company_name}: {e}")
                    continue

            jobs_with_desc = sum(1 for j in jobs if j.job_description)
            logger.info(
                f"Fetched {len(jobs)} jobs from {company_name} "
                f"({jobs_with_desc} with descriptions, {len(jobs) - jobs_with_desc} without)"
            )
            return jobs

        except requests.exceptions.RequestException as e:
            logger.warning(f"Error fetching Ashby jobs for {company_name}: {e}")
            # Raise so the worker does not archive the company's open jobs on failure.
            raise

    def _parse_job(self, job_data: Dict, company_name: str, company_endpoint: str) -> Optional[JobData]:
        job_id = job_data.get("id")
        if not job_id:
            raise ValueError("Ashby job payload missing id")

        # Ashby includes the full description in the list response — no detail call needed.
        # Prefer plain text; fall back to HTML-cleaned version.
        description = job_data.get("descriptionPlain") or None
        if not description:
            html_desc = job_data.get("descriptionHtml") or job_data.get("description")
            if html_desc:
                description = self._clean_html(html_desc)

        # Location may be a plain string or a nested object depending on board config.
        location_raw = job_data.get("location") or job_data.get("locationName")
        if isinstance(location_raw, dict):
            location = location_raw.get("name") or location_raw.get("locationName")
        else:
            location = location_raw or None

        # Date: prefer publishedAt, fall back to updatedAt.
        date_posted = None
        for date_field in ("publishedAt", "updatedAt"):
            raw = job_data.get(date_field)
            if raw:
                try:
                    date_posted = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
                    break
                except (ValueError, AttributeError):
                    continue

        remote_allowed = bool(job_data.get("isRemote", False))

        # Hybrid isn't a native Ashby field; infer from location text.
        hybrid_allowed = bool(location and "hybrid" in location.lower())

        employment_type = job_data.get("employmentType") or None

        # jobUrl is the public application page. Build a fallback if absent.
        job_url = job_data.get("jobUrl") or job_data.get("applyUrl")
        if not job_url:
            job_url = f"https://jobs.ashbyhq.com/{company_endpoint}/{job_id}"

        title = job_data.get("title")
        return JobData(
            url=job_url,
            job_title=title,
            company=company_name,
            location=location,
            job_description=description,
            date_posted=date_posted,
            employment_type=employment_type,
            application_url=job_url,
            remote_allowed=remote_allowed,
            hybrid_allowed=hybrid_allowed,
            source_website=self.name,
            job_id_from_source=str(job_id),
            status="active",
            scraped_at=datetime.now(),
            created_at=datetime.now(),
            occupation_category=from_title(title or ""),
        )

    def fetch_job_by_url(self, url: str) -> Optional[JobData]:
        """Fetch a single JobData from a public Ashby job page URL.

        URL format: https://jobs.ashbyhq.com/{company}/{job_id}

        The per-posting endpoint (posting-api/job-board/{company}/posting/{id})
        requires authentication and returns 401 for public callers.  Instead,
        fetch the full company board (which is public and includes all field data
        inline) and return the posting whose id matches the one in the URL.
        """
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != "jobs.ashbyhq.com":
            return None
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            logger.warning("Cannot parse Ashby job URL: %s", url)
            return None
        company = path_parts[0]
        job_id = path_parts[1]

        self.rate_limiter.wait_if_needed()
        board_url = f"{self.base_url}/{company}"
        try:
            resp = requests.get(board_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("Ashby board fetch failed for %s: %s", url, e)
            return None

        company_name = company.replace("-", " ").title()
        for job_data in data.get("jobs", []):
            if str(job_data.get("id")) == job_id:
                try:
                    return self._parse_job(job_data, company_name, company)
                except Exception as e:
                    logger.warning("Ashby _parse_job failed for %s: %s", url, e, exc_info=True)
                    return None

        logger.info("Ashby job %s not found in board for %s", job_id, company)
        return None

    def _clean_html(self, html_content: str) -> str:
        """Strip HTML to plain text. Uses BeautifulSoup when available; falls back to regex."""
        return html_to_text(html_content)

    def get_rate_limit(self) -> int:
        return self.rate_limiter.requests_per_minute
