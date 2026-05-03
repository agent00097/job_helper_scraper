"""
Greenhouse API source for fetching jobs from Greenhouse job boards.
"""
import re
import requests
import logging
import html
from typing import List, Dict, Optional
from datetime import datetime, date

from sources.base_source import BaseSource
from models import JobData
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Board / embed pages redirect to the employer's careers site (often behind Cloudflare).
# Canonical URL for automation is the public JSON API job resource:
#   https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}
GREENHOUSE_JOB_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^https?://boards-api\.greenhouse\.io/v1/boards/([^/]+)/jobs/(\d+)/?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://boards\.greenhouse\.io/([^/]+)/jobs/(\d+)/?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https?://job-boards\.greenhouse\.io/([^/]+)/jobs/(\d+)/?$",
        re.IGNORECASE,
    ),
)


def parse_greenhouse_board_job_url(url: str) -> Optional[tuple[str, int]]:
    """
    If url identifies a Greenhouse board job, return (board_token, job_id).

    Accepts API job URLs, boards.greenhouse.io, and job-boards.greenhouse.io paths.
    Board token is the path segment before /jobs/{id} (e.g. 'airbnb').
    """
    if not url:
        return None
    s = url.strip()
    for pat in GREENHOUSE_JOB_URL_PATTERNS:
        m = pat.match(s)
        if m:
            return m.group(1), int(m.group(2))
    return None


def greenhouse_api_job_url(base_url: str, company_endpoint: str, job_id: int) -> str:
    """Stable Greenhouse JSON URL for a job (no redirect to employer careers domain)."""
    root = base_url.rstrip("/")
    return f"{root}/boards/{company_endpoint}/jobs/{job_id}"


class GreenhouseSource(BaseSource):
    """Greenhouse API source implementation."""
    
    def __init__(self, name: str, source_id: str, config: Dict, rate_limit_per_minute: int):
        """
        Initialize Greenhouse source.
        
        Args:
            name: Source name
            source_id: Source UUID
            config: Source configuration
            rate_limit_per_minute: Rate limit for API requests
        """
        super().__init__(name, source_id, config)
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self.base_url = config.get("base_url", "https://boards-api.greenhouse.io/v1")
    
    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        """
        Fetch jobs from Greenhouse for a specific company.
        
        Args:
            company_endpoint: Company slug (e.g., 'airbnb')
            company_name: Company name (e.g., 'Airbnb')
            
        Returns:
            List of JobData objects
        """
        self.rate_limiter.wait_if_needed()
        
        api_url = f"{self.base_url}/boards/{company_endpoint}/jobs"
        
        try:
            logger.info(f"Fetching jobs from Greenhouse for {company_name} ({company_endpoint})")
            response = requests.get(api_url, timeout=30)
            response.raise_for_status()
            
            jobs_data = response.json()
            jobs = []
            
            for job_data in jobs_data.get("jobs", []):
                try:
                    # Parse basic job info first
                    job = self._parse_job(job_data, company_name, company_endpoint)
                    if job:
                        # Fetch full job description from detail endpoint
                        job_id = job_data.get("id")
                        job_description = self._fetch_job_description(
                            company_endpoint, 
                            job_id
                        )
                        if job_description:
                            job.job_description = job_description
                            logger.debug(f"Fetched description for job {job_id} ({len(job_description)} chars)")
                        else:
                            logger.debug(f"No description found for job {job_id}")
                        
                        jobs.append(job)
                except Exception as e:
                    logger.warning(f"Error parsing job from {company_name}: {e}")
                    continue
            
            # Count how many jobs have descriptions
            jobs_with_descriptions = sum(1 for job in jobs if job.job_description)
            logger.info(
                f"Fetched {len(jobs)} jobs from {company_name} "
                f"({jobs_with_descriptions} with descriptions, {len(jobs) - jobs_with_descriptions} without)"
            )
            return jobs
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching jobs from Greenhouse for {company_name}: {e}")
            return []
    
    def _parse_job(self, job_data: Dict, company_name: str, company_endpoint: str) -> JobData:
        """
        Parse a single job from Greenhouse API response.
        
        Args:
            job_data: Job data from API
            company_name: Company name
            company_endpoint: Company endpoint
            
        Returns:
            JobData object
        """
        job_id = job_data.get("id")
        if job_id is None:
            raise ValueError("Greenhouse job payload missing id")
        job_id_int = int(job_id)
        # Use boards-api job URL so automation does not follow redirects to absolute_url
        # (employer careers sites are often behind Cloudflare).
        job_url = greenhouse_api_job_url(self.base_url, company_endpoint, job_id_int)
        
        # Parse location
        location = None
        if job_data.get("location"):
            location = job_data["location"].get("name")
        
        # Parse date posted
        date_posted = None
        if job_data.get("updated_at"):
            try:
                date_posted = datetime.fromisoformat(job_data["updated_at"].replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                pass
        
        # Parse employment type - look for relevant metadata fields
        employment_type = None
        metadata = job_data.get("metadata")
        if metadata and isinstance(metadata, list):
            # Look for employment-related metadata (Workplace Type, Employment Type, etc.)
            for item in metadata:
                name = item.get("name", "").lower()
                value = item.get("value")
                value_type = item.get("value_type", "")
                
                # Only use string values, skip booleans
                if isinstance(value, str) and (
                    "employment" in name or 
                    "workplace" in name or 
                    "type" in name
                ):
                    employment_type = value
                    break
        
        # Check if remote or hybrid based on location and metadata
        remote_allowed = False
        hybrid_allowed = False
        
        if location:
            location_lower = location.lower()
            if "remote" in location_lower or "anywhere" in location_lower:
                remote_allowed = True
        
        # Check metadata for workplace type
        if metadata and isinstance(metadata, list):
            for item in metadata:
                name = item.get("name", "").lower()
                value = item.get("value")
                if isinstance(value, str):
                    value_lower = value.lower()
                    if "workplace" in name or "type" in name:
                        if "remote" in value_lower:
                            remote_allowed = True
                        elif "hybrid" in value_lower:
                            hybrid_allowed = True
        
        # Build application URL
        application_url = job_url
        
        # Note: job_description will be fetched separately in fetch_jobs()
        # to avoid making too many API calls in the list endpoint
        
        job = JobData(
            url=job_url,
            job_title=job_data.get("title"),
            company=company_name,
            location=location,
            job_description=None,  # Will be fetched from detail endpoint
            date_posted=date_posted,
            employment_type=employment_type,
            application_url=application_url,
            remote_allowed=remote_allowed,
            hybrid_allowed=hybrid_allowed,
            source_website=self.name,
            job_id_from_source=str(job_id_int),
            status="active",
            scraped_at=datetime.now(),
            created_at=datetime.now()
        )
        
        return job
    
    def _fetch_job_description(self, company_endpoint: str, job_id: Optional[int]) -> Optional[str]:
        """
        Fetch the full job description from Greenhouse detail endpoint.
        
        Args:
            company_endpoint: Company slug (e.g., 'airbnb')
            job_id: Job ID from Greenhouse
            
        Returns:
            Cleaned job description text or None if fetch fails
        """
        if not job_id:
            return None
        
        self.rate_limiter.wait_if_needed()
        
        detail_url = f"{self.base_url}/boards/{company_endpoint}/jobs/{job_id}"
        
        try:
            logger.debug(f"Fetching job description from {detail_url}")
            response = requests.get(detail_url, timeout=30)
            response.raise_for_status()
            
            job_detail = response.json()
            content = job_detail.get("content")
            
            if content:
                # Decode HTML entities (e.g., &lt; becomes <)
                decoded_content = html.unescape(content)
                # Clean up the HTML - remove tags but keep text
                cleaned_content = self._clean_html(decoded_content)
                logger.debug(f"Successfully fetched and cleaned description for job {job_id} ({len(cleaned_content)} chars)")
                return cleaned_content
            else:
                logger.debug(f"No content field found in job detail for job {job_id}")
                return None
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error fetching job description for job {job_id} from {detail_url}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Error processing job description for job {job_id}: {e}", exc_info=True)
            return None

    def _infer_company_name(self, job_detail: Dict, company_endpoint: str) -> str:
        """Best-effort company display name from API detail or board slug."""
        company = job_detail.get("company")
        if isinstance(company, dict) and company.get("name"):
            return str(company["name"])
        if isinstance(company, str) and company.strip():
            return company.strip()
        return company_endpoint.replace("-", " ").title()

    def fetch_job_by_board_page_url(self, job_page_url: str) -> Optional[JobData]:
        """
        Fetch a single JobData from a public Greenhouse job URL using the same API
        parsing path as fetch_jobs (one GET to the job detail endpoint).
        """
        parsed = parse_greenhouse_board_job_url(job_page_url)
        if not parsed:
            return None
        company_endpoint, job_id = parsed
        self.rate_limiter.wait_if_needed()
        detail_url = f"{self.base_url}/boards/{company_endpoint}/jobs/{job_id}"
        try:
            logger.debug("Fetching Greenhouse job detail from %s", detail_url)
            response = requests.get(detail_url, timeout=30)
            response.raise_for_status()
            job_detail = response.json()
        except requests.exceptions.RequestException as e:
            logger.warning("Greenhouse detail fetch failed for %s: %s", job_page_url, e)
            return None
        except Exception as e:
            logger.warning("Greenhouse detail parse failed for %s: %s", job_page_url, e, exc_info=True)
            return None

        company_name = self._infer_company_name(job_detail, company_endpoint)
        try:
            job = self._parse_job(job_detail, company_name, company_endpoint)
        except Exception as e:
            logger.warning("Greenhouse _parse_job failed for %s: %s", job_page_url, e, exc_info=True)
            return None

        content = job_detail.get("content")
        if content:
            decoded = html.unescape(content)
            job.job_description = self._clean_html(decoded)
        return job

    def _clean_html(self, html_content: str) -> str:
        """
        Clean HTML content to extract readable text.
        
        Args:
            html_content: HTML string
            
        Returns:
            Cleaned text content
        """
        if not html_content:
            return ""
        
        # Simple HTML tag removal using regex (basic approach)
        # For more advanced cleaning, we could use BeautifulSoup, but keeping it simple for now
        import re
        
        # Remove script and style tags and their content
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        
        # Replace common HTML entities with newlines for better readability
        html_content = html_content.replace('</p>', '\n\n')
        html_content = html_content.replace('</div>', '\n')
        html_content = html_content.replace('<br>', '\n')
        html_content = html_content.replace('<br/>', '\n')
        html_content = html_content.replace('<br />', '\n')
        html_content = html_content.replace('</li>', '\n')
        html_content = html_content.replace('</h1>', '\n\n')
        html_content = html_content.replace('</h2>', '\n\n')
        html_content = html_content.replace('</h3>', '\n\n')
        html_content = html_content.replace('</h4>', '\n\n')
        
        # Remove all remaining HTML tags
        html_content = re.sub(r'<[^>]+>', '', html_content)
        
        # Clean up whitespace
        html_content = re.sub(r'\n\s*\n\s*\n+', '\n\n', html_content)  # Multiple newlines to double
        html_content = re.sub(r'[ \t]+', ' ', html_content)  # Multiple spaces to single
        html_content = html_content.strip()
        
        return html_content
    
    def get_rate_limit(self) -> int:
        """Get the rate limit for this source."""
        return self.rate_limiter.requests_per_minute
