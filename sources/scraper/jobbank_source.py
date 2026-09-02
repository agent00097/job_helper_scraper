"""
Job Bank (jobbank.gc.ca) HTML scraper source.

Supports two execution modes:
  "incremental"  – daily run, fetches only jobs newer than last_scraped_at watermark
  "backfill"     – nightly run, pages backwards through older results with a time budget

company_endpoint is an optional query-string fragment appended to the search
URL (e.g. "keywords=nurse&province=ON"). Job Bank is not company-scoped, so
the same source fetches across all employers.
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString

import db
from models import JobData
from sources.base_source import BaseSource
from utils.geo import derive_country
from utils.html_text import parsed_html
from utils.occupation_category import from_noc, from_title
from utils.rate_limiter import RateLimiter
from utils.job_storage import save_job

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

_ET = ZoneInfo("America/Toronto")


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
        mode: str = "incremental",
        max_runtime_seconds: int = 28800,
        max_jobs: int = 100_000,
        skip_db: bool = False,
    ) -> None:
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        # max_pages: used by skip_db (test) mode and as a per-session cap for backfill.
        self.max_pages: int = int(config.get("max_pages", 20))
        # max_pages_incremental: safety-net page cap for incremental mode only.
        # The operational exit is the page-level "all jobs older than cutoff" check.
        self.max_pages_incremental: int = int(config.get("max_pages_incremental", 200))
        self.fetch_descriptions: bool = bool(config.get("fetch_descriptions", True))
        self.mode = mode
        self.max_runtime_seconds = max_runtime_seconds
        self.max_jobs = max_jobs
        # skip_db disables all DB access. Set True explicitly, or auto-set when
        # source_id is absent or starts with "test-" (standalone test guard).
        self.skip_db: bool = skip_db or not source_id or source_id.startswith("test-")
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self._progress = self._load_progress()

    # ------------------------------------------------------------------
    # BaseSource interface
    # ------------------------------------------------------------------

    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        """Dispatch to incremental or backfill runner based on self.mode."""
        if self.mode == "backfill":
            return self._run_backfill(company_endpoint)
        return self._run_incremental(company_endpoint)

    def get_rate_limit(self) -> int:
        return self.rate_limiter.requests_per_minute

    def release_resources(self) -> None:
        try:
            self.session.close()
        except Exception:
            logger.debug("JobBank session close failed", exc_info=True)
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)

    # ------------------------------------------------------------------
    # Incremental runner
    # ------------------------------------------------------------------

    def _run_incremental(self, company_endpoint: str) -> List[JobData]:
        """Fetch only jobs newer than the stored watermark.

        Stops paginating once an entire page falls below the cutoff date.
        On success, updates scrape_progress.last_scraped_at.

        Returns the list of new JobData objects (unsaved — caller persists).
        When skip_db=True (test mode) all parsed jobs are returned regardless
        of age or DB state.
        """
        watermark: Optional[date] = None
        if self._progress and self._progress.get("last_scraped_at"):
            ts = self._progress["last_scraped_at"]
            if isinstance(ts, datetime):
                watermark = ts.astimezone(_ET).date()
            else:
                watermark = ts  # already a date

        # 3-day overlap: Job Bank commonly back-dates postings 2-3 days when employers
        # submit via partner channels. URL dedup makes the wider window essentially free —
        # every duplicate is caught at the SELECT-before-INSERT step and silently skipped.
        cutoff_date: Optional[date] = (watermark - timedelta(days=3)) if watermark else None

        page_limit = self.max_pages if self.skip_db else self.max_pages_incremental
        collected: List[JobData] = []

        for page in range(1, page_limit + 1):
            self.rate_limiter.wait_if_needed()
            time.sleep(random.uniform(0.4, 1.6))

            url = self._build_url(page, company_endpoint)
            logger.info("JobBank incremental: fetching page %d", page)
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
            except requests.exceptions.RequestException as e:
                logger.warning("JobBank page %d request failed: %s", page, e)
                break

            page_jobs = self._parse_search_page(resp.text)
            if not page_jobs:
                logger.info("JobBank incremental: page %d empty — done", page)
                break

            old_count = 0
            for job in page_jobs:
                if cutoff_date and job.date_posted and job.date_posted < cutoff_date:
                    old_count += 1
                    continue
                # Skip description fetch for URLs already in DB (saves request budget).
                if not self.skip_db and self._job_url_exists(str(job.url)):
                    continue
                if self.fetch_descriptions:
                    try:
                        desc, noc = self._fetch_detail_page_data(str(job.url))
                        if desc:
                            job.job_description = desc
                        job.occupation_category = (
                            from_noc(noc) if noc else from_title(job.job_title or "")
                        )
                    except Exception as e:
                        logger.warning("JobBank description fetch failed %s: %s", job.url, e)
                else:
                    job.occupation_category = from_title(job.job_title or "")
                collected.append(job)

            logger.info(
                "JobBank incremental page %d: %d total, %d old/skipped, %d new",
                page, len(page_jobs), old_count, len(page_jobs) - old_count,
            )

            # Stop when every job on the page predates the cutoff.
            if cutoff_date and old_count == len(page_jobs):
                logger.info("JobBank incremental: full page below cutoff — stopping")
                break

            if len(page_jobs) < 25:
                logger.info("JobBank incremental: partial page (%d) — end of results", len(page_jobs))
                break

        if not self.skip_db:
            self._save_incremental_done()
        logger.info("JobBank incremental complete: %d jobs collected", len(collected))
        return collected

    # ------------------------------------------------------------------
    # Backfill runner
    # ------------------------------------------------------------------

    def _run_backfill(self, company_endpoint: str) -> List[JobData]:
        """Page through older results, resuming from the last completed page.

        Saves jobs directly (not returned) — backfill volumes are too large to
        hold in memory. Always returns [].

        Respects max_runtime_seconds budget and max_jobs safety cap.
        On natural end-of-results, sets backfill_completed_at.
        """
        start_time = time.monotonic()
        resume_page = (self._progress.get("backfill_last_page") or 0) if self._progress else 0
        start_page = resume_page + 1
        total_saved = 0
        pages_completed = 0
        oldest_date: Optional[date] = None

        for page in range(start_page, start_page + 10_000):  # 10k pages = hard safety cap
            elapsed = time.monotonic() - start_time
            if elapsed >= self.max_runtime_seconds:
                logger.info("JobBank backfill: time budget exhausted at page %d (%.0fs elapsed)", page, elapsed)
                break

            if total_saved >= self.max_jobs:
                logger.info("JobBank backfill: max_jobs cap (%d) reached at page %d", self.max_jobs, page)
                break

            self.rate_limiter.wait_if_needed()
            time.sleep(random.uniform(0.4, 1.6))

            url = self._build_url(page, company_endpoint)
            logger.info("JobBank backfill: fetching page %d (elapsed %.0fs)", page, elapsed)
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
            except requests.exceptions.RequestException as e:
                logger.warning("JobBank backfill page %d request failed: %s", page, e)
                break

            page_jobs = self._parse_search_page(resp.text)
            if not page_jobs:
                logger.info("JobBank backfill: page %d empty — end of results", page)
                if not self.skip_db:
                    self._save_backfill_done(page - 1, oldest_date)
                logger.info(
                    "JobBank backfill session: %d jobs saved over %d pages",
                    total_saved, pages_completed,
                )
                return []

            for job in page_jobs:
                if job.date_posted and (oldest_date is None or job.date_posted < oldest_date):
                    oldest_date = job.date_posted
                if not self.skip_db:
                    if self._job_url_exists(str(job.url)):
                        continue
                    if self.fetch_descriptions:
                        try:
                            desc, noc = self._fetch_detail_page_data(str(job.url))
                            if desc:
                                job.job_description = desc
                            job.occupation_category = (
                                from_noc(noc) if noc else from_title(job.job_title or "")
                            )
                        except Exception as e:
                            logger.warning("JobBank description fetch failed %s: %s", job.url, e)
                    else:
                        job.occupation_category = from_title(job.job_title or "")
                    save_job(job)
                total_saved += 1

            pages_completed += 1
            if not self.skip_db:
                self._save_progress_page(page, oldest_date)

            logger.info(
                "JobBank backfill page %d: %d jobs on page, %d saved total, oldest=%s",
                page, len(page_jobs), total_saved, oldest_date,
            )

            if len(page_jobs) < 25:
                logger.info(
                    "JobBank backfill: partial page (%d jobs) — end of results, backfill complete",
                    len(page_jobs),
                )
                if not self.skip_db:
                    self._save_backfill_done(page, oldest_date)
                logger.info(
                    "JobBank backfill session: %d jobs saved over %d pages",
                    total_saved, pages_completed,
                )
                return []

        logger.info(
            "JobBank backfill session: %d jobs saved over %d pages",
            total_saved, pages_completed,
        )
        return []

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _job_url_exists(self, url: str) -> bool:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM jobs WHERE url = %s LIMIT 1", (url,))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def _load_progress(self) -> Optional[Dict]:
        if self.skip_db:
            return None
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT last_scraped_at, backfill_last_page,
                              backfill_oldest_job_date, backfill_completed_at
                       FROM scrape_progress WHERE source_id = %s""",
                    (self.source_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "last_scraped_at":         row[0],
                    "backfill_last_page":       row[1],
                    "backfill_oldest_job_date": row[2],
                    "backfill_completed_at":    row[3],
                }
        except Exception:
            logger.exception("JobBank: failed to load scrape_progress")
            return None
        finally:
            conn.close()

    def _save_incremental_done(self) -> None:
        if self.skip_db:
            return
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO scrape_progress (source_id, last_scraped_at, updated_at)
                       VALUES (%s, NOW(), NOW())
                       ON CONFLICT (source_id) DO UPDATE
                           SET last_scraped_at = NOW(), updated_at = NOW()""",
                    (self.source_id,),
                )
            conn.commit()
        except Exception:
            logger.exception("JobBank: failed to update last_scraped_at")
            conn.rollback()
        finally:
            conn.close()

    def _save_progress_page(self, page: int, oldest_date: Optional[date]) -> None:
        if self.skip_db:
            return
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO scrape_progress
                           (source_id, backfill_last_page, backfill_oldest_job_date, updated_at)
                       VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (source_id) DO UPDATE
                           SET backfill_last_page       = EXCLUDED.backfill_last_page,
                               backfill_oldest_job_date = EXCLUDED.backfill_oldest_job_date,
                               updated_at               = NOW()""",
                    (self.source_id, page, oldest_date),
                )
            conn.commit()
        except Exception:
            logger.exception("JobBank: failed to save backfill page progress")
            conn.rollback()
        finally:
            conn.close()

    def _save_backfill_done(self, last_page: int, oldest_date: Optional[date]) -> None:
        if self.skip_db:
            return
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO scrape_progress
                           (source_id, backfill_last_page, backfill_oldest_job_date,
                            backfill_completed_at, updated_at)
                       VALUES (%s, %s, %s, NOW(), NOW())
                       ON CONFLICT (source_id) DO UPDATE
                           SET backfill_last_page       = EXCLUDED.backfill_last_page,
                               backfill_oldest_job_date = EXCLUDED.backfill_oldest_job_date,
                               backfill_completed_at    = NOW(),
                               updated_at               = NOW()""",
                    (self.source_id, last_page, oldest_date),
                )
            conn.commit()
        except Exception:
            logger.exception("JobBank: failed to save backfill_completed_at")
            conn.rollback()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Search-page parsing
    # ------------------------------------------------------------------

    def _parse_search_page(self, html: str) -> List[JobData]:
        with parsed_html(html) as soup:
            articles = soup.find_all("article", class_="action-buttons")
            jobs: List[JobData] = []
            for article in articles:
                try:
                    job = self._parse_article(article)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning(
                        "Job Bank: skipping article %s — %s",
                        article.get("id", "?"),
                        e,
                    )
            return jobs

    def _parse_article(self, article) -> Optional[JobData]:
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
        """Fetch full description from a Job Bank detail page (public API).

        Wraps _fetch_detail_page_data and discards the NOC code so the
        existing test interface is unchanged.
        """
        desc, _ = self._fetch_detail_page_data(job_url)
        return desc

    def _fetch_detail_page_data(
        self, job_url: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Fetch description and NOC code from a Job Bank detail page.

        Returns (description, noc_code). Either may be None on failure.
        """
        self.rate_limiter.wait_if_needed()
        time.sleep(random.uniform(0.4, 1.6))
        try:
            resp = self.session.get(job_url, timeout=30)
        except requests.exceptions.RequestException as e:
            logger.warning("Job Bank detail fetch failed for %s: %s", job_url, e)
            return None, None
        try:
            resp.raise_for_status()
            html_text = resp.text
        except requests.exceptions.RequestException as e:
            logger.warning("Job Bank detail fetch failed for %s: %s", job_url, e)
            return None, None
        finally:
            resp.close()

        with parsed_html(html_text) as soup:
            desc_el = soup.find("div", class_="job-posting-detail-requirements")
            if not desc_el:
                logger.warning("Job Bank: no description container at %s", job_url)
            description = desc_el.get_text(separator="\n", strip=True) if desc_el else None
            noc_code = self._extract_noc(soup)
            return description, noc_code

    @staticmethod
    def _extract_noc(soup: BeautifulSoup) -> Optional[str]:
        """Extract the NOC code from a Job Bank detail page.

        Tries definition-list pattern first (most reliable), then a regex
        scan of the full page text as fallback.
        """
        # Definition-list pattern: <dt>NOC</dt><dd>12345</dd>
        for dt in soup.find_all("dt"):
            if "noc" in dt.get_text().lower():
                dd = dt.find_next_sibling("dd")
                if dd:
                    m = re.search(r"\b(\d{4,5})\b", dd.get_text())
                    if m:
                        return m.group(1)

        # Fallback: regex anywhere in page text
        m = re.search(r"\bNOC\s*:?\s*(\d{4,5})\b", soup.get_text(), re.IGNORECASE)
        if m:
            return m.group(1)

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_url(page: int, company_endpoint: str) -> str:
        if company_endpoint:
            return f"{JOBBANK_BASE}{_SEARCH_PATH}?sort=M&page={page}&{company_endpoint}"
        return f"{JOBBANK_BASE}{_SEARCH_PATH}?sort=M&page={page}"


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
