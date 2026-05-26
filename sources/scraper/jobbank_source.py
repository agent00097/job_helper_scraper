"""
Job Bank (jobbank.gc.ca) HTML scraper source.

company_endpoint is an optional query-string fragment appended to the search
URL (e.g. "keywords=nurse&province=ON"). Job Bank is not company-scoped, so
the same source fetches across all employers.
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup, NavigableString

from models import JobData
from sources.base_source import BaseSource
from utils.geo import derive_country
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

JOBBANK_BASE = "https://www.jobbank.gc.ca"
_SEARCH_PATH = "/jobsearch/jobsearch"
_DETAIL_PATH = "/jobsearch/jobposting"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class JobBankSource(BaseSource):
    """HTML scraper for Canada's Job Bank (jobbank.gc.ca).

    Does not use Playwright — results are server-rendered HTML.
    """

    def __init__(
        self,
        name: str,
        source_id: str,
        config: Dict,
        rate_limit_per_minute: int,
    ) -> None:
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        # max_pages caps pagination; set low for testing, higher for production.
        self.max_pages: int = int(config.get("max_pages", 20))
        # fetch_descriptions=False skips per-job detail calls (list-only mode).
        self.fetch_descriptions: bool = bool(config.get("fetch_descriptions", True))
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)

    # ------------------------------------------------------------------
    # BaseSource interface
    # ------------------------------------------------------------------

    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        """Paginate Job Bank search results and return a flat list of JobData.

        company_endpoint: optional query-string fragment appended to the search
            URL (e.g. "keywords=software+developer&province=ON"). Pass "" to
            fetch all recent postings sorted by most-recent.
        company_name: unused — Job Bank is not company-scoped.
        """
        jobs: List[JobData] = []

        for page in range(1, self.max_pages + 1):
            self.rate_limiter.wait_if_needed()
            time.sleep(random.uniform(0.5, 1.5))  # jitter; Job Bank blocks aggressive scrapers

            url = f"{JOBBANK_BASE}{_SEARCH_PATH}?sort=M&page={page}"
            if company_endpoint:
                url += f"&{company_endpoint}"

            logger.info("Job Bank: fetching page %d", page)
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
            except requests.exceptions.RequestException as e:
                logger.warning("Job Bank page %d request failed: %s", page, e)
                break

            page_jobs = self._parse_search_page(resp.text)
            if not page_jobs:
                logger.info("Job Bank page %d: no articles — pagination complete", page)
                break

            jobs.extend(page_jobs)
            logger.info("Job Bank page %d: %d jobs (total: %d)", page, len(page_jobs), len(jobs))

        if self.fetch_descriptions:
            for job in jobs:
                try:
                    desc = self.fetch_description(str(job.url))
                    if desc:
                        job.job_description = desc
                except Exception as e:
                    logger.warning("Job Bank description fetch failed for %s: %s", job.url, e)

        jobs_with_desc = sum(1 for j in jobs if j.job_description)
        logger.info(
            "Job Bank: %d jobs total (%d with descriptions, %d without)",
            len(jobs), jobs_with_desc, len(jobs) - jobs_with_desc,
        )
        return jobs

    def get_rate_limit(self) -> int:
        return self.rate_limiter.requests_per_minute

    # ------------------------------------------------------------------
    # Search-page parsing
    # ------------------------------------------------------------------

    def _parse_search_page(self, html: str) -> List[JobData]:
        soup = BeautifulSoup(html, "html.parser")
        articles = soup.find_all("article", class_="action-buttons")
        jobs: List[JobData] = []
        for article in articles:
            try:
                job = self._parse_article(article)
                if job:
                    jobs.append(job)
            except Exception as e:
                logger.warning("Job Bank: skipping article %s — %s", article.get("id", "?"), e)
        return jobs

    def _parse_article(self, article) -> Optional[JobData]:
        # Posting ID from article id="article-{id}" — used to build a clean URL.
        raw_id = article.get("id", "")
        posting_id = raw_id.replace("article-", "", 1).strip()
        if not posting_id:
            raise ValueError(f"article missing id: {raw_id!r}")

        job_url = f"{JOBBANK_BASE}{_DETAIL_PATH}/{posting_id}"

        title_el = article.find("span", class_="noctitle")
        job_title = title_el.get_text(strip=True) if title_el else None

        biz_el = article.find("li", class_="business")
        company = biz_el.get_text(strip=True) if biz_el else None

        # Direct NavigableString children skip the "Location" wb-inv label.
        location = _direct_text(article.find("li", class_="location"))

        # Normalise "(XX)" → ", XX" so derive_country recognises province abbrevs.
        country_code: Optional[str] = None
        if location:
            try:
                country_code = derive_country(re.sub(r"\(([A-Z]{2})\)", r", \1", location))
            except Exception:
                pass

        salary: Optional[str] = None
        sal_el = article.find("li", class_="salary")
        if sal_el:
            raw = sal_el.get_text(strip=True)
            salary = re.sub(r"^Salary\s*", "", raw, flags=re.IGNORECASE).strip() or None

        tel_el = article.find("span", class_="telework")
        telework = tel_el.get_text(strip=True).lower() if tel_el else ""
        remote_allowed = "remote" in telework
        hybrid_allowed = "hybrid" in telework

        date_posted = None
        date_el = article.find("li", class_="date")
        if date_el:
            try:
                date_posted = datetime.strptime(date_el.get_text(strip=True), "%b %d, %Y").date()
            except ValueError:
                pass

        # Job number — direct text child of li.source is the bare digit string.
        job_number = _direct_text(article.find("li", class_="source"))
        if job_number and not re.match(r"^\d+$", job_number):
            m = re.search(r"\d+", job_number)
            job_number = m.group(0) if m else None

        now = datetime.now()
        return JobData(
            url=job_url,
            job_title=job_title,
            company=company,
            location=location,
            salary_range=salary,
            date_posted=date_posted,
            remote_allowed=remote_allowed,
            hybrid_allowed=hybrid_allowed,
            country_code=country_code,
            source_website=self.name,
            job_id_from_source=job_number,
            status="active",
            scraped_at=now,
            created_at=now,
        )

    # ------------------------------------------------------------------
    # Detail-page description fetch
    # ------------------------------------------------------------------

    def fetch_description(self, job_url: str) -> Optional[str]:
        """Fetch the full description from a Job Bank detail page.

        Selector: div.job-posting-detail-requirements
        Contains Overview (language/education/experience), Responsibilities,
        and any extra sections the employer provided.
        """
        self.rate_limiter.wait_if_needed()
        time.sleep(random.uniform(0.5, 1.5))
        try:
            resp = self.session.get(job_url, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.warning("Job Bank detail fetch failed for %s: %s", job_url, e)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        desc_el = soup.find("div", class_="job-posting-detail-requirements")
        if not desc_el:
            logger.warning("Job Bank: no description container at %s", job_url)
            return None

        return desc_el.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _direct_text(element) -> Optional[str]:
    """Return text from direct NavigableString children only.

    Skips text inside nested tags — in particular wb-inv screen-reader labels
    and icon spans — giving clean field values without label contamination.
    """
    if element is None:
        return None
    parts = [
        str(c).strip()
        for c in element.children
        if isinstance(c, NavigableString) and str(c).strip()
    ]
    return " ".join(parts).strip() or None
