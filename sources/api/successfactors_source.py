"""
SAP SuccessFactors Recruiting Marketing (RMK) source.

company_endpoint is the career-site origin (or search URL), e.g.:
    https://jobs.sap.com
    https://jobs.sap.com/search/?q=

List (HTML fragment, no JSON):
  GET {origin}/tile-search-results/?startrow={n}
Detail (full JD):
  GET {origin}/job/{slug}/{rmkId}/

Branded hosts (jobs.sap.com) do not contain "successfactors" in the hostname.
Hosted hosts (*.successfactors.com / *.sapsf.eu) are routed by company_discovery.
"""
from __future__ import annotations

import html as _html
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

import requests

from models import JobData
from sources.base_source import BaseSource
from utils.deduplication import urls_with_existing_description
from utils.occupation_category import from_title
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

MAX_PAGES = 200
# RMK tiles have no JD. First-run boards can have 150–2000 jobs; a detail GET
# per job exceeds COMPANY_SCRAPE_TIMEOUT_SECONDS and saves nothing. Cap details
# per run; later scrapes fill remaining descriptions via selective skip.
DEFAULT_MAX_DETAIL_FETCHES = 50
DEFAULT_DETAIL_WORKERS = 4
_JOB_PATH_RE = re.compile(r"/job/([^/]+)/(\d+)/?$", re.IGNORECASE)
_EMPLOYMENT_TYPE_RE = re.compile(
    r"Employment Type:\s*([^\n<]+)",
    re.IGNORECASE,
)
_HOSTED_SUFFIXES = (
    ".successfactors.com",
    ".successfactors.eu",
    ".sapsf.com",
    ".sapsf.eu",
    ".sapsf.cn",
)
_HOSTED_EXACT = frozenset(
    {
        "successfactors.com",
        "successfactors.eu",
        "sapsf.com",
        "sapsf.eu",
        "sapsf.cn",
        "jobs2web.com",
    }
)
_BOARD_PATH_STRIP_RE = re.compile(
    r"/(?:search|tile-search-results)(?:/.*)?$",
    re.IGNORECASE,
)
_SKIP_HOST_LABELS = frozenset({"www", "jobs", "job", "careers", "career"})
_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (compatible; HarcoJobScraper/1.0; +https://harco.app)"
    ),
}


def is_hosted_successfactors_host(hostname: str | None) -> bool:
    host = (hostname or "").lower()
    if not host:
        return False
    if host in _HOSTED_EXACT:
        return True
    return any(host.endswith(suffix) for suffix in _HOSTED_SUFFIXES)


def parse_successfactors_job_url(url: str) -> Optional[Tuple[str, str]]:
    """Return (origin, rmk_job_id) for an RMK job page URL."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.hostname:
        return None
    match = _JOB_PATH_RE.search(parsed.path or "")
    if not match:
        return None
    origin = f"{parsed.scheme or 'https'}://{parsed.hostname}"
    if parsed.port and parsed.port not in (80, 443):
        origin = f"{origin}:{parsed.port}"
    return origin, match.group(2)


def resolve_board_base(company_endpoint: str) -> str:
    """Normalize a stored endpoint to the RMK board base (origin + optional brand path)."""
    raw = (company_endpoint or "").strip()
    if not raw:
        raise ValueError("SuccessFactors company_endpoint is empty")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise ValueError(f"Unrecognized SuccessFactors board URL: {company_endpoint!r}")
    path = _BOARD_PATH_STRIP_RE.sub("", parsed.path or "").rstrip("/")
    return f"{parsed.scheme}://{parsed.hostname}{path}"


def city_from_slug(data_url: str, title: str) -> Optional[str]:
    """Recover city from `/job/{City}-{Title}-{code}/{id}/` when the tile omits it."""
    try:
        path = unquote(data_url or "")
    except Exception:
        path = data_url or ""
    match = re.search(r"/job/([^/]+)/", path)
    if not match:
        return None
    slug = match.group(1).lower()
    words = re.findall(r"[\w]+", (title or "").lower(), flags=re.UNICODE)
    if len(words) < 2:
        return None
    try:
        anchor = re.compile(
            re.escape(words[0]) + r"[^\w]+" + re.escape(words[1]),
            re.UNICODE,
        )
    except re.error:
        return None
    hit = anchor.search(slug)
    if not hit or hit.start() <= 0:
        return None
    prefix = slug[: hit.start()].strip("-")
    parts = [p for p in re.split(r"[^\w]+", prefix, flags=re.UNICODE) if p]
    if not parts:
        return None
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def parse_tiles(html_text: str, job_base: str) -> List[Dict]:
    """Parse RMK tile-search-results HTML into unique {id, title, url, location} rows."""
    if not html_text or not html_text.strip():
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _parse_tiles_regex(html_text, job_base)

    soup = BeautifulSoup(html_text, "html.parser")
    tiles = soup.select("li.job-tile[data-url]") or soup.select("[data-url]")
    out: List[Dict] = []
    seen: set[str] = set()
    for tile in tiles:
        path = (tile.get("data-url") or "").strip()
        parsed = _JOB_PATH_RE.search(path)
        if not parsed:
            continue
        job_id = parsed.group(2)
        if job_id in seen:
            continue
        seen.add(job_id)
        link = tile.select_one("a.jobTitle-link") or tile.find("a", href=True)
        title = ""
        if link:
            title = link.get_text(" ", strip=True)
            if not path:
                path = (link.get("href") or "").strip()
        loc_el = tile.select_one(".jobGeoLocation, .jobLocation, .job-location")
        location = loc_el.get_text(" ", strip=True) if loc_el else None
        if not location:
            location = city_from_slug(path, title)
        url = path if path.startswith(("http://", "https://")) else urljoin(job_base + "/", path.lstrip("/"))
        if not title:
            continue
        out.append(
            {
                "id": job_id,
                "title": title,
                "url": url,
                "location": location or None,
            }
        )
    return out


def _parse_tiles_regex(html_text: str, job_base: str) -> List[Dict]:
    out: List[Dict] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'data-url="([^"]+)"',
        html_text,
        re.IGNORECASE,
    ):
        path = _html.unescape(match.group(1)).strip()
        parsed = _JOB_PATH_RE.search(path)
        if not parsed:
            continue
        job_id = parsed.group(2)
        if job_id in seen:
            continue
        seen.add(job_id)
        title_match = re.search(
            r'class="[^"]*jobTitle-link[^"]*"[^>]*>\s*([^<]+)',
            html_text[match.start() : match.start() + 4000],
            re.IGNORECASE,
        )
        title = _html.unescape(title_match.group(1)).strip() if title_match else ""
        if not title:
            continue
        url = path if path.startswith(("http://", "https://")) else urljoin(job_base + "/", path.lstrip("/"))
        out.append(
            {
                "id": job_id,
                "title": title,
                "url": url,
                "location": city_from_slug(path, title),
            }
        )
    return out


def _company_domain_hint(board_base: str) -> Optional[str]:
    host = (urlparse(board_base).hostname or "").lower()
    if not host or is_hosted_successfactors_host(host):
        return None
    host = host.removeprefix("www.")
    labels = host.split(".")
    while len(labels) >= 3 and labels[0] in _SKIP_HOST_LABELS:
        labels = labels[1:]
    return ".".join(labels) if len(labels) >= 2 else host or None


def _parse_posted_date(raw: str | None) -> Optional[date]:
    if not raw or not str(raw).strip():
        return None
    value = re.sub(r"\s+", " ", str(raw)).strip()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed:
            return parsed.date()
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


class SuccessFactorsSource(BaseSource):
    """SuccessFactors RMK career-site source."""

    def __init__(self, name: str, source_id: str, config: Dict, rate_limit_per_minute: int):
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self._http = requests.Session()
        self._http.headers.update(_REQUEST_HEADERS)

    def _max_detail_fetches(self) -> int:
        raw = self.config.get("max_detail_fetches_per_run", DEFAULT_MAX_DETAIL_FETCHES)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_MAX_DETAIL_FETCHES

    def _detail_workers(self) -> int:
        raw = self.config.get("detail_workers", DEFAULT_DETAIL_WORKERS)
        try:
            return max(1, min(int(raw), 8))
        except (TypeError, ValueError):
            return DEFAULT_DETAIL_WORKERS

    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        try:
            board_base = resolve_board_base(company_endpoint)
        except ValueError as exc:
            logger.error("Invalid SuccessFactors company_endpoint for %s: %s", company_name, exc)
            raise

        logger.info(
            "Fetching jobs from SuccessFactors RMK for %s (%s)",
            company_name,
            board_base,
        )
        postings = self._fetch_all_postings(board_base, company_name)
        if not postings:
            return []

        list_parsed: List[Tuple[Dict, JobData]] = []
        for posting in postings:
            try:
                job = self._parse_job(posting, company_name, board_base, detail=None)
                if job:
                    list_parsed.append((posting, job))
            except Exception as exc:
                logger.warning("Error parsing SuccessFactors job from %s: %s", company_name, exc)
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
        details_by_url: Dict[str, Optional[Dict]] = {}
        if to_enrich:
            workers = min(self._detail_workers(), len(to_enrich))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(self._fetch_detail, str(job.url)): str(job.url)
                    for _, job in to_enrich
                }
                for fut in as_completed(futs):
                    details_by_url[futs[fut]] = fut.result()

        enrich_urls = {str(job.url) for _, job in to_enrich}
        jobs: List[JobData] = []
        detail_fetched = 0
        for posting, list_job in list_parsed:
            url = str(list_job.url)
            if url not in enrich_urls:
                jobs.append(list_job)
                continue
            detail_fetched += 1
            detail = details_by_url.get(url)
            if not detail:
                jobs.append(list_job)
                continue
            try:
                enriched = self._parse_job(
                    posting, company_name, board_base, detail=detail
                )
                jobs.append(enriched if enriched else list_job)
            except Exception as exc:
                logger.warning(
                    "Error enriching SuccessFactors job %s from %s: %s",
                    list_job.job_id_from_source,
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

    def _fetch_all_postings(self, board_base: str, company_name: str) -> List[Dict]:
        all_postings: List[Dict] = []
        seen_ids: set[str] = set()
        startrow = 0
        tile_url = f"{board_base}/tile-search-results/"

        for _page in range(MAX_PAGES):
            self.rate_limiter.wait_if_needed()
            try:
                response = self._http.get(
                    tile_url,
                    params={"startrow": startrow},
                    headers={"Referer": f"{board_base}/search/"},
                    timeout=30,
                )
                response.raise_for_status()
                html_text = response.text or ""
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "SuccessFactors list fetch failed at startrow %d for %s: %s",
                    startrow,
                    company_name,
                    exc,
                )
                raise

            tiles = parse_tiles(html_text, board_base)
            if not tiles:
                break

            fresh = 0
            for tile in tiles:
                job_id = str(tile.get("id") or "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                all_postings.append(tile)
                fresh += 1

            if fresh == 0:
                break
            startrow += len(tiles)
        else:
            logger.warning(
                "SuccessFactors %s: hit MAX_PAGES=%d with %d postings",
                company_name,
                MAX_PAGES,
                len(all_postings),
            )

        return all_postings

    def _fetch_detail(self, job_url: str) -> Optional[Dict]:
        self.rate_limiter.wait_if_needed()
        try:
            response = self._http.get(job_url, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.warning("SuccessFactors detail fetch failed for %s: %s", job_url, exc)
            return None
        return self._parse_detail_html(response.text or "", job_url)

    def _parse_detail_html(self, html_text: str, job_url: str) -> Optional[Dict]:
        if not html_text:
            return None
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {"description": self._clean_html(html_text) or None}

        soup = BeautifulSoup(html_text, "html.parser")
        # Comma selectors pick the first match in document order; the Apply
        # button sits in .jobTitle before the real h1 on RMK pages.
        title_el = (
            soup.select_one('h1 [itemprop="title"]')
            or soup.select_one('[itemprop="title"]')
            or soup.select_one("h1")
        )
        desc_el = (
            soup.select_one('[itemprop="description"]')
            or soup.select_one("span.jobdescription")
            or soup.select_one(".jobdescription")
        )
        loc_el = (
            soup.select_one(".jobGeoLocation")
            or soup.select_one("#job-location")
            or soup.select_one(".jobLocation")
        )
        posted_el = soup.select_one('meta[itemprop="datePosted"]') or soup.select_one(
            '[data-careersite-propertyid="date"]'
        )
        org_el = soup.select_one('meta[itemprop="hiringOrganization"]')
        apply_el = soup.select_one("a.dialogApplyBtn") or soup.select_one(
            "a[href*='/talentcommunity/apply/']"
        )

        posted_raw = None
        if posted_el:
            posted_raw = posted_el.get("content") or posted_el.get_text(" ", strip=True)

        apply_url = None
        if apply_el and apply_el.get("href"):
            apply_url = urljoin(job_url, apply_el["href"])

        description_html = str(desc_el) if desc_el else ""
        employment_type = None
        if desc_el:
            emp = _EMPLOYMENT_TYPE_RE.search(desc_el.get_text("\n", strip=True))
            if emp:
                employment_type = emp.group(1).strip() or None

        return {
            "title": title_el.get_text(" ", strip=True) if title_el else None,
            "description": self._clean_html(description_html) if description_html else None,
            "location": loc_el.get_text(" ", strip=True) if loc_el else None,
            "date_posted": _parse_posted_date(posted_raw),
            "company": org_el.get("content") if org_el else None,
            "employment_type": employment_type,
            "apply_url": apply_url,
        }

    def _parse_job(
        self,
        posting: Dict,
        company_name: str,
        board_base: str,
        detail: Optional[Dict] = None,
    ) -> Optional[JobData]:
        job_id = posting.get("id")
        if not job_id:
            raise ValueError("SuccessFactors posting missing id")

        title = (detail or {}).get("title") or posting.get("title")
        location = (detail or {}).get("location") or posting.get("location")
        description = (detail or {}).get("description") if detail else None
        date_posted = (detail or {}).get("date_posted") if detail else None
        employment_type = (detail or {}).get("employment_type") if detail else None
        resolved_company = (detail or {}).get("company") if detail else None
        if not isinstance(resolved_company, str) or not resolved_company.strip():
            resolved_company = company_name

        job_url = posting.get("url") or ""
        if not job_url:
            raise ValueError("SuccessFactors posting missing url")
        apply_url = (detail or {}).get("apply_url") if detail else None
        application_url = (
            apply_url
            if isinstance(apply_url, str) and apply_url.strip()
            else job_url
        )

        loc_lower = (location or "").lower()
        remote_allowed = "remote" in loc_lower
        hybrid_allowed = "hybrid" in loc_lower
        if description:
            desc_lower = description.lower()
            if not remote_allowed and "remote" in desc_lower:
                remote_allowed = True
            if not hybrid_allowed and "hybrid" in desc_lower:
                hybrid_allowed = True

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
            company_domain_hint=_company_domain_hint(board_base),
        )

    def fetch_job_by_url(self, url: str) -> Optional[JobData]:
        parsed = parse_successfactors_job_url(url)
        if not parsed:
            return None
        origin, job_id = parsed
        parsed_url = urlparse(url.strip())
        job_url = f"{origin}{parsed_url.path}"
        if not job_url.endswith("/"):
            job_url += "/"
        detail = self._fetch_detail(job_url)
        if not detail:
            return None
        posting = {
            "id": job_id,
            "title": detail.get("title"),
            "url": job_url,
            "location": detail.get("location"),
        }
        company_name = detail.get("company") or origin
        try:
            return self._parse_job(posting, company_name, origin, detail=detail)
        except Exception as exc:
            logger.warning("SuccessFactors _parse_job failed for %s: %s", url, exc, exc_info=True)
            return None

    def _clean_html(self, html_content: str) -> str:
        if not html_content:
            return ""
        try:
            from bs4 import BeautifulSoup

            text = BeautifulSoup(html_content, "html.parser").get_text(separator="\n")
            return _html.unescape(text).strip()
        except ImportError:
            text = re.sub(r"<[^>]+>", " ", html_content)
            return _html.unescape(re.sub(r"\s+", " ", text)).strip()

    def get_rate_limit(self) -> int:
        return self.rate_limiter.requests_per_minute
