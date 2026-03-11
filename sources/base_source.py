"""
Abstract base class for all job sources (API and Scraper).
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from models import JobData


class BaseSource(ABC):
    """Abstract base class for all job sources."""
    
    def __init__(self, name: str, source_id: str, config: Dict):
        """
        Initialize the source.
        
        Args:
            name: Source name (e.g., 'greenhouse', 'ashby')
            source_id: UUID of the source from job_sources table
            config: Source configuration from job_sources.config
        """
        self.name = name
        self.source_id = source_id
        self.config = config
    
    @abstractmethod
    def fetch_jobs(self, company_endpoint: str, company_name: str) -> List[JobData]:
        """
        Fetch jobs for a specific company.
        
        Args:
            company_endpoint: Company endpoint/slug (e.g., 'airbnb' for Greenhouse)
            company_name: Company name (e.g., 'Airbnb')
            
        Returns:
            List of JobData objects
        """
        pass
    
    @abstractmethod
    def get_rate_limit(self) -> int:
        """
        Get the rate limit for this source (requests per minute).
        
        Returns:
            Rate limit as integer
        """
        pass
