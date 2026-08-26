"""
Workday API source for fetching jobs from Workday career boards.

company_endpoint is the full board URL, e.g.:
    https://salesforce.wd12.myworkdayjobs.com/External_Career_Site
"""
import html as _html
import logging
import re
import requests
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from models import JobData
from sources.base_source import BaseSource
from utils.deduplication import urls_with_existing_description
from utils.occupation_category import from_title
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

PAGE_SIZE = 20

_TIME_TYPE_MAP = {
    "Full_time": "Full-time",
    "Part_time": "Part-time",
    "FULL_TIME": "Full-time",
    "PART_TIME": "Part-time",
    "Temporary": "Contract",
}

_MULTI_LOCATION_RE = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)


def parse_workday_board_url(board_url: str) -> Tuple[str, str, str]:
    """
    Parse a Workday board URL into (tenant, dc, site).

    Examples:
      "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"
        -> ("salesforce", "wd12", "External_Career_Site")
      "https://amazon.wd5.myworkdayjobs.com/en-US/Amazon_Jobs"
        -> ("amazon", "wd5", "en-US/Amazon_Jobs")
    """
    parsed = urlparse(board_url)
    hostname = parsed.hostname or ""
    parts = hostname.split(".")
    # Expected: {tenant}.{dc}.myworkdayjobs.com  (4 dot-separated parts)
    if len(parts) < 4 or parts[2] != "myworkdayjobs":
        raise ValueError(f"Unrecognized Workday board URL: {board_url!r}")
    tenant = parts[0]
    dc = parts[1]
    site = parsed.path.strip("/")
    if not site:
        raise ValueError(f"Workday board URL missing site path: {board_url!r}")
    return tenant, dc, site


class WorkdaySource(BaseSource):
    """Workday careers API source implementation."""

    def __init__(self, name: str, source_id: str, config: Dict, rate_limit_per_minute: int):
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)

    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        try:
            tenant, dc, site = parse_workday_board_url(company_endpoint)
        except ValueError as e:
            logger.error("Invalid Workday company_endpoint for %s: %s", company_name, e)
            # Raise so the worker does not archive on bad config / failed fetch.
            raise

        base_host = f"https://{tenant}.{dc}.myworkdayjobs.com"
        api_base = f"{base_host}/wday/cxs/{tenant}/{site}"
        public_base = f"{base_host}/{site}"

        logger.info(
            "Fetching jobs from Workday for %s (tenant=%s, dc=%s, site=%s)",
            company_name, tenant, dc, site,
        )

        postings = self._fetch_all_postings(api_base, company_name)
        if not postings:
            return []

        # List-only parse first (needed for presence reconcile). Detail-fetch
        # descriptions only for jobs that are new or stored without a description.
        list_parsed: List[Tuple[Dict, JobData]] = []
        for posting in postings:
            try:
                job = self._parse_job(
                    posting, company_name, public_base, detail=None
                )
                if job:
                    list_parsed.append((posting, job))
            except Exception as e:
                logger.warning("Error parsing Workday job from %s: %s", company_name, e)
                continue
        del postings

        already_described = urls_with_existing_description(
            str(job.url) for _, job in list_parsed
        )
        jobs: List[JobData] = []
        detail_fetched = 0
        detail_skipped = 0

        for posting, list_job in list_parsed:
            if str(list_job.url) in already_described:
                detail_skipped += 1
                jobs.append(list_job)
                continue

            external_path = posting.get("externalPath") or ""
            detail = self._fetch_detail(api_base, external_path)
            detail_fetched += 1
            try:
                enriched = self._parse_job(
                    posting, company_name, public_base, detail=detail
                )
                jobs.append(enriched if enriched else list_job)
            except Exception as e:
                logger.warning(
                    "Error enriching Workday job from %s (%s): %s",
                    company_name, external_path, e,
                )
                jobs.append(list_job)

        jobs_with_desc = sum(1 for j in jobs if j.job_description)
        logger.info(
            "Fetched %d jobs from %s "
            "(detail fetched %d, skipped %d; %d with descriptions this run, %d without)",
            len(jobs),
            company_name,
            detail_fetched,
            detail_skipped,
            jobs_with_desc,
            len(jobs) - jobs_with_desc,
        )
        del list_parsed
        return jobs

    def _fetch_all_postings(self, api_base: str, company_name: str) -> List[Dict]:
        """Paginate the Workday jobs list endpoint (PAGE_SIZE per request)."""
        all_postings: List[Dict] = []
        offset = 0

        while True:
            self.rate_limiter.wait_if_needed()
            payload = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            }
            try:
                resp = requests.post(
                    f"{api_base}/jobs",
                    json=payload,
                    timeout=30,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.RequestException as e:
                logger.warning(
                    "Workday list fetch failed at offset %d for %s: %s",
                    offset, company_name, e,
                )
                # Never return a partial page set — presence reconcile would
                # incorrectly archive jobs on later pages.
                raise

            page = data.get("jobPostings") or []
            all_postings.extend(page)
            total = data.get("total", 0)
            logger.debug("Workday %s: fetched %d/%d", company_name, len(all_postings), total)

            if not page or len(all_postings) >= total:
                break
            offset += PAGE_SIZE

        return all_postings

    def _parse_job(
        self,
        posting: Dict,
        company_name: str,
        public_base: str,
        detail: Optional[Dict] = None,
    ) -> Optional[JobData]:
        """
        Build JobData from a list posting.

        When detail is None, only list fields are used (no HTTP). Pass the
        detail JSON to fill description, employment type, and remote flags.
        """
        external_path = posting.get("externalPath")
        if not external_path:
            raise ValueError("Workday posting missing externalPath")

        job_url = f"{public_base}{external_path}"
        # Stable ID from the path; slashes replaced so it's a single token.
        job_id = external_path.lstrip("/").replace("/", "_")

        locations_text = posting.get("locationsText") or None
        date_posted = self._parse_date(posting.get("postedOn"))

        description = None
        employment_type = None
        remote_allowed = False
        hybrid_allowed = False

        if detail:
            info = detail.get("jobPostingInfo") or {}

            html_desc = info.get("jobDescription") or ""
            if html_desc:
                description = self._clean_html(html_desc)

            raw_time_type = info.get("timeType") or ""
            employment_type = _TIME_TYPE_MAP.get(raw_time_type, raw_time_type or None)

            # remoteType is present when the role is explicitly remote or hybrid.
            remote_type = (info.get("remoteType") or "").lower()
            remote_allowed = "remote" in remote_type
            hybrid_allowed = "hybrid" in remote_type

        location = self._resolve_location(locations_text, detail)

        # Infer remote/hybrid from location text when API flags are absent.
        if not remote_allowed and not hybrid_allowed and location:
            loc_lower = location.lower()
            if "remote" in loc_lower:
                remote_allowed = True
            elif "hybrid" in loc_lower:
                hybrid_allowed = True

        title = posting.get("title")
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
            job_id_from_source=job_id,
            status="active",
            scraped_at=datetime.now(),
            created_at=datetime.now(),
            occupation_category=from_title(title or ""),
        )

    def _resolve_location(self, locations_text: Optional[str], detail: Optional[Dict]) -> Optional[str]:
        """Return the best location string for a posting.

        If locations_text is a real place name, return it unchanged.
        If it is a count string ("6 Locations") or absent, pull location names
        from the per-job detail response and join them with "; ".
        """
        if locations_text and not _MULTI_LOCATION_RE.match(locations_text):
            return locations_text

        if not detail:
            return locations_text or None

        info = detail.get("jobPostingInfo") or {}
        parts: List[str] = []

        req_loc = info.get("jobRequisitionLocation")
        if isinstance(req_loc, dict):
            name = req_loc.get("descriptor") or req_loc.get("name") or ""
            if name:
                parts.append(name)
        elif isinstance(req_loc, str) and req_loc.strip():
            parts.append(req_loc.strip())

        for loc in info.get("additionalLocations") or []:
            if isinstance(loc, dict):
                name = loc.get("descriptor") or loc.get("name") or ""
                if name:
                    parts.append(name)
            elif isinstance(loc, str) and loc.strip():
                parts.append(loc.strip())

        return "; ".join(parts) if parts else (locations_text or None)

    def _fetch_detail(self, api_base: str, external_path: str) -> Optional[Dict]:
        """GET the per-job detail endpoint; return parsed JSON or None on failure."""
        self.rate_limiter.wait_if_needed()
        detail_url = f"{api_base}{external_path}"
        try:
            resp = requests.get(
                detail_url,
                timeout=30,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("Workday detail fetch failed for %s: %s", external_path, e)
            return None
        except Exception as e:
            logger.warning("Workday detail parse failed for %s: %s", external_path, e, exc_info=True)
            return None

    def _parse_date(self, raw: Optional[str]) -> Optional[date]:
        if not raw:
            return None
        # MM/DD/YYYY
        try:
            return datetime.strptime(raw, "%m/%d/%Y").date()
        except ValueError:
            pass
        # ISO 8601
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            pass
        # "Posted N Days Ago" — only exact counts; "30+" is too imprecise.
        m = re.search(r"posted\s+(\d+)\s+days?\s+ago", raw, re.IGNORECASE)
        if m:
            return (datetime.now() - timedelta(days=int(m.group(1)))).date()
        if re.search(r"posted\s+today", raw, re.IGNORECASE):
            return datetime.now().date()
        return None

    def _clean_html(self, html_content: str) -> str:
        """Strip HTML to plain text using BeautifulSoup, falling back to regex.

        html.unescape is applied after extraction so numeric entity codes like
        &#xa; (newline) and &#x2019; (right-quote) become real characters even
        if the parser left any residual references in the output.
        """
        if not html_content:
            return ""
        try:
            from bs4 import BeautifulSoup
            text = BeautifulSoup(html_content, "html.parser").get_text(separator="\n")
            return _html.unescape(text).strip()
        except ImportError:
            text = re.sub(r"<[^>]+>", " ", html_content)
            return _html.unescape(re.sub(r"\s+", " ", text)).strip()

    def fetch_job_by_url(self, url: str) -> Optional[JobData]:
        """Fetch a single JobData from a public Workday job page URL.

        Splits at '/job/' to reconstruct the board URL and externalPath, then
        issues one detail GET without going through the full paginated list path.
        """
        job_marker = "/job/"
        idx = url.find(job_marker)
        if idx == -1:
            logger.warning("Cannot parse Workday job URL (no /job/ segment): %s", url)
            return None

        board_url = url[:idx]
        external_path = url[idx:]  # e.g. /job/Software-Engineer_JR-12345

        try:
            tenant, dc, site = parse_workday_board_url(board_url)
        except ValueError as e:
            logger.warning("Cannot parse Workday board URL from %s: %s", url, e)
            return None

        base_host = f"https://{tenant}.{dc}.myworkdayjobs.com"
        api_base = f"{base_host}/wday/cxs/{tenant}/{site}"
        public_base = f"{base_host}/{site}"
        job_url = f"{public_base}{external_path}"
        job_id = external_path.lstrip("/").replace("/", "_")
        company_name = tenant.replace("-", " ").title()

        detail = self._fetch_detail(api_base, external_path)
        if not detail:
            return None

        info = detail.get("jobPostingInfo") or {}
        html_desc = info.get("jobDescription") or ""
        description = self._clean_html(html_desc) if html_desc else None

        raw_time_type = info.get("timeType") or ""
        employment_type = _TIME_TYPE_MAP.get(raw_time_type, raw_time_type or None)

        remote_type = (info.get("remoteType") or "").lower()
        remote_allowed = "remote" in remote_type
        hybrid_allowed = "hybrid" in remote_type

        location = self._resolve_location(None, detail)

        if not remote_allowed and not hybrid_allowed and location:
            loc_lower = location.lower()
            if "remote" in loc_lower:
                remote_allowed = True
            elif "hybrid" in loc_lower:
                hybrid_allowed = True

        title = info.get("title")
        return JobData(
            url=job_url,
            job_title=title,
            company=company_name,
            location=location,
            job_description=description,
            date_posted=None,  # postedOn is only in the list response, not the detail
            employment_type=employment_type,
            application_url=job_url,
            remote_allowed=remote_allowed,
            hybrid_allowed=hybrid_allowed,
            source_website=self.name,
            job_id_from_source=job_id,
            status="active",
            scraped_at=datetime.now(),
            created_at=datetime.now(),
            occupation_category=from_title(title or ""),
        )

    def get_rate_limit(self) -> int:
        return self.rate_limiter.requests_per_minute
