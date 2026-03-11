"""
Greenhouse API source for fetching jobs from Greenhouse job boards.
"""
import requests
import logging
from typing import List, Dict
from datetime import datetime, date

from sources.base_source import BaseSource
from models import JobData
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


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
        self.public_boards_url = config.get("public_boards_url", "https://boards.greenhouse.io")
    
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
                    job = self._parse_job(job_data, company_name, company_endpoint)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    logger.warning(f"Error parsing job from {company_name}: {e}")
                    continue
            
            logger.info(f"Fetched {len(jobs)} jobs from {company_name}")
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
        # Build job URL
        job_id = job_data.get("id")
        job_url = f"{self.public_boards_url}/{company_endpoint}/jobs/{job_id}"
        
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
        application_url = job_url  # Greenhouse jobs link to their own page
        
        job = JobData(
            url=job_url,
            job_title=job_data.get("title"),
            company=company_name,
            location=location,
            job_description=job_data.get("content") or job_data.get("description") or None,
            date_posted=date_posted,
            employment_type=employment_type,
            application_url=application_url,
            remote_allowed=remote_allowed,
            hybrid_allowed=hybrid_allowed,
            source_website=self.name,
            job_id_from_source=str(job_id),
            status="active",
            scraped_at=datetime.now(),
            created_at=datetime.now()
        )
        
        return job
    
    def get_rate_limit(self) -> int:
        """Get the rate limit for this source."""
        return self.rate_limiter.requests_per_minute
