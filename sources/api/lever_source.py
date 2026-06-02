"""
Lever API source for fetching jobs from Lever job boards.
"""
import logging
import re
import requests
from typing import List, Dict, Optional
from datetime import datetime, timezone
from urllib.parse import urlparse

from sources.base_source import BaseSource
from models import JobData
from utils.occupation_category import from_title
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

LEVER_API_BASE = "https://api.lever.co/v0/postings"


class LeverSource(BaseSource):
    """Lever public postings API source implementation."""

    def __init__(self, name: str, source_id: str, config: Dict, rate_limit_per_minute: int):
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self.base_url = config.get("base_url", LEVER_API_BASE)

    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        self.rate_limiter.wait_if_needed()
        api_url = f"{self.base_url}/{company_endpoint}?mode=json"

        try:
            logger.info(f"Fetching jobs from Lever for {company_name} ({company_endpoint})")
            response = requests.get(api_url, timeout=30)
            response.raise_for_status()

            # Lever returns a top-level JSON array, not a dict with a nested key.
            postings = response.json()
            if not isinstance(postings, list):
                logger.warning(
                    f"Unexpected Lever response shape for {company_name}: "
                    f"expected list, got {type(postings).__name__}"
                )
                return []

            jobs = []
            for posting in postings:
                try:
                    job = self._parse_job(posting, company_name)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning(f"Error parsing Lever job from {company_name}: {e}")
                    continue

            jobs_with_desc = sum(1 for j in jobs if j.job_description)
            logger.info(
                f"Fetched {len(jobs)} jobs from {company_name} "
                f"({jobs_with_desc} with descriptions, {len(jobs) - jobs_with_desc} without)"
            )
            return jobs

        except requests.exceptions.RequestException as e:
            logger.warning(f"Error fetching Lever jobs for {company_name}: {e}")
            return []

    def _parse_job(self, posting: Dict, company_name: str) -> Optional[JobData]:
        job_id = posting.get("id")
        if not job_id:
            raise ValueError("Lever posting missing id")

        categories = posting.get("categories") or {}

        # Build a complete description from the main body and the structured lists sections.
        # Lever splits role content (e.g. Requirements, Responsibilities) into lists[].
        description = self._build_description(posting) or None

        # Location lives under categories.
        location = categories.get("location") or None

        # createdAt is epoch milliseconds.
        date_posted = None
        created_at_ms = posting.get("createdAt")
        if created_at_ms:
            try:
                date_posted = datetime.fromtimestamp(
                    int(created_at_ms) / 1000, tz=timezone.utc
                ).date()
            except (ValueError, TypeError, OSError):
                pass

        # workplaceType: "remote" | "on-site" | "hybrid" — not present on older postings.
        workplace = (posting.get("workplaceType") or "").lower()
        remote_allowed = workplace == "remote"
        hybrid_allowed = workplace == "hybrid"

        # Fall back to inferring from location text when workplaceType is absent.
        if not remote_allowed and not hybrid_allowed and location:
            loc_lower = location.lower()
            if "remote" in loc_lower:
                remote_allowed = True
            elif "hybrid" in loc_lower:
                hybrid_allowed = True

        # categories.commitment maps to employment type (e.g. "Full-time", "Internship").
        employment_type = categories.get("commitment") or None

        # hostedUrl is the canonical public page; applyUrl opens the apply form directly.
        job_url = posting.get("hostedUrl") or posting.get("applyUrl")
        if not job_url:
            raise ValueError(f"Lever posting {job_id} has no hostedUrl or applyUrl")

        title = posting.get("text")
        return JobData(
            url=job_url,
            job_title=title,
            company=company_name,
            location=location,
            job_description=description,
            date_posted=date_posted,
            employment_type=employment_type,
            application_url=posting.get("applyUrl") or job_url,
            remote_allowed=remote_allowed,
            hybrid_allowed=hybrid_allowed,
            source_website=self.name,
            job_id_from_source=str(job_id),
            status="active",
            scraped_at=datetime.now(),
            created_at=datetime.now(),
            occupation_category=from_title(title or ""),
        )

    def _build_description(self, posting: Dict) -> str:
        """
        Concatenate the main description body with any structured lists sections.
        Lever splits role-specific content (Requirements, Responsibilities, etc.) into
        lists[], which is separate from the main description field — joining both gives
        a complete picture of the role.
        """
        parts = []

        plain = (posting.get("descriptionPlain") or "").strip()
        if plain:
            parts.append(plain)
        else:
            html_desc = posting.get("description") or ""
            if html_desc:
                parts.append(self._clean_html(html_desc))

        for section in posting.get("lists") or []:
            section_title = (section.get("text") or "").strip()
            section_content = (section.get("content") or "").strip()
            if not section_content:
                continue
            cleaned = self._clean_html(section_content)
            if cleaned:
                parts.append(f"{section_title}\n{cleaned}" if section_title else cleaned)

        return "\n\n".join(parts)

    def fetch_job_by_url(self, url: str) -> Optional[JobData]:
        """Fetch a single JobData from a public Lever job page URL.

        URL format: https://jobs.lever.co/{company}/{job_id}
        Uses Lever's single-posting endpoint so only one API call is needed.
        """
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != "jobs.lever.co":
            return None
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            logger.warning("Cannot parse Lever job URL: %s", url)
            return None
        company = path_parts[0]
        job_id = path_parts[1]

        self.rate_limiter.wait_if_needed()
        api_url = f"{self.base_url}/{company}/{job_id}?mode=json"
        try:
            resp = requests.get(api_url, timeout=30)
            resp.raise_for_status()
            posting = resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("Lever single-job fetch failed for %s: %s", url, e)
            return None

        company_name = company.replace("-", " ").title()
        try:
            return self._parse_job(posting, company_name)
        except Exception as e:
            logger.warning("Lever _parse_job failed for %s: %s", url, e, exc_info=True)
            return None

    def _clean_html(self, html_content: str) -> str:
        """Strip HTML to plain text using BeautifulSoup, falling back to regex."""
        if not html_content:
            return ""
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html_content, "html.parser").get_text(separator="\n").strip()
        except ImportError:
            text = re.sub(r"<[^>]+>", " ", html_content)
            return re.sub(r"\s+", " ", text).strip()

    def get_rate_limit(self) -> int:
        return self.rate_limiter.requests_per_minute
