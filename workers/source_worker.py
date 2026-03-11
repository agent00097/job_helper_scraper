"""
Worker for running a job source and fetching jobs.
"""
import logging
from typing import List
from sources.base_source import BaseSource
from utils.source_loader import get_source_companies, update_source_last_run, update_company_last_fetched
from utils.job_storage import save_jobs

logger = logging.getLogger(__name__)


class SourceWorker:
    """Worker that processes a single source."""
    
    def __init__(self, source: BaseSource):
        """
        Initialize the worker.
        
        Args:
            source: BaseSource instance to work with
        """
        self.source = source
    
    def run(self) -> dict:
        """
        Run the source worker to fetch and save jobs.
        
        Returns:
            Dictionary with statistics about the run
        """
        logger.info(f"Starting worker for source: {self.source.name}")
        
        # Get all companies for this source
        companies = get_source_companies(self.source.source_id)
        
        if not companies:
            logger.warning(f"No companies found for source: {self.source.name}")
            return {
                "source": self.source.name,
                "companies_processed": 0,
                "total_jobs_fetched": 0,
                "jobs_saved": 0,
                "jobs_duplicates": 0,
                "errors": []
            }
        
        logger.info(f"Processing {len(companies)} companies for {self.source.name}")
        
        total_jobs_fetched = 0
        total_jobs_saved = 0
        total_jobs_duplicates = 0
        errors = []
        
        # Process each company
        for company in companies:
            company_name = company["company_name"]
            company_endpoint = company["company_endpoint"]
            company_id = company["id"]
            
            try:
                logger.info(f"Fetching jobs for {company_name} from {self.source.name}")
                
                # Fetch jobs from source
                jobs = self.source.fetch_jobs(company_endpoint, company_name)
                total_jobs_fetched += len(jobs)
                
                if jobs:
                    # Save jobs to database
                    saved, duplicates = save_jobs(jobs)
                    total_jobs_saved += saved
                    total_jobs_duplicates += duplicates
                    
                    logger.info(
                        f"{company_name}: Fetched {len(jobs)} jobs, "
                        f"saved {saved}, duplicates {duplicates}"
                    )
                else:
                    logger.info(f"{company_name}: No jobs found")
                
                # Update company last fetched timestamp
                update_company_last_fetched(company_id)
                
            except Exception as e:
                error_msg = f"Error processing {company_name}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
                continue
        
        # Update source last run timestamp
        update_source_last_run(self.source.source_id)
        
        stats = {
            "source": self.source.name,
            "companies_processed": len(companies),
            "total_jobs_fetched": total_jobs_fetched,
            "jobs_saved": total_jobs_saved,
            "jobs_duplicates": total_jobs_duplicates,
            "errors": errors
        }
        
        logger.info(
            f"Completed worker for {self.source.name}: "
            f"{stats['jobs_saved']} jobs saved, "
            f"{stats['jobs_duplicates']} duplicates skipped"
        )
        
        return stats
