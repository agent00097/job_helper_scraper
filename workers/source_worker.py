"""
Worker for running a job source and fetching jobs.
"""
import logging
from uuid import UUID

from sources.base_source import BaseSource
from utils.source_loader import get_source_companies, update_source_last_run, update_company_last_fetched
from utils.job_storage import save_jobs
from utils.job_archive import (
    archive_jobs_missing_from_fetch,
    seen_keys_from_jobs,
    supports_presence_reconcile,
)

logger = logging.getLogger(__name__)


def queue_existing_company_enrichment(**kwargs):
    """Lazy import keeps RabbitMQ dependencies out of worker module import."""
    from services.company_check import queue_existing_company_enrichment as queue

    return queue(**kwargs)


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

        # Get all companies for this source (new JSONB schema — keyed by source name)
        companies = get_source_companies(self.source.name)

        if not companies:
            logger.warning(f"No companies found for source: {self.source.name}")
            return {
                "source": self.source.name,
                "companies_processed": 0,
                "total_jobs_fetched": 0,
                "jobs_saved": 0,
                "jobs_duplicates": 0,
                "jobs_archived": 0,
                "errors": [],
            }

        logger.info(f"Processing {len(companies)} companies for {self.source.name}")

        total_jobs_fetched = 0
        total_jobs_saved = 0
        total_jobs_duplicates = 0
        total_jobs_archived = 0
        errors = []
        reconcile = supports_presence_reconcile(self.source.name)

        # Process each company
        for company in companies:
            company_name = company["company_name"]
            company_endpoint = company["company_endpoint"]
            company_id = company["id"]

            try:
                logger.info(f"Fetching jobs for {company_name} from {self.source.name}")

                # Fetch jobs from source. API sources raise on HTTP failure so we
                # never treat a failed board pull as "all jobs closed".
                jobs = self.source.fetch_jobs(company_endpoint, company_name)
                total_jobs_fetched += len(jobs)

                # This worker loaded the company from source_companies, so stamp
                # its known ID directly instead of re-discovering it per job.
                for job in jobs:
                    job.company_id = UUID(str(company_id))

                # Existing companies bypass ensure_company(). Queue onboarding
                # here when their logo is missing, using any source API domain
                # signal plus a logo URL captured from the ATS board.
                if not (company.get("logo_url") or "").strip():
                    source_domain_hint = next(
                        (
                            job.company_domain_hint
                            for job in jobs
                            if job.company_domain_hint
                        ),
                        None,
                    )
                    queue_existing_company_enrichment(
                        company_name=company_name,
                        normalized_name=company["normalized_name"],
                        source_name=self.source.name,
                        source_endpoint=company_endpoint,
                        stored_domain=company.get("domain"),
                        source_domain_hint=source_domain_hint,
                    )

                if reconcile:
                    seen_ids, seen_urls = seen_keys_from_jobs(jobs)
                    archived = archive_jobs_missing_from_fetch(
                        source_website=self.source.name,
                        company_name=company_name,
                        seen_source_ids=seen_ids,
                        seen_urls=seen_urls,
                    )
                    total_jobs_archived += archived

                if jobs:
                    # source_endpoint remains useful for non-scheduler callers;
                    # these jobs already carry company_id.
                    saved, duplicates = save_jobs(jobs, source_endpoint=company_endpoint)
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
            "jobs_archived": total_jobs_archived,
            "errors": errors,
        }

        logger.info(
            f"Completed worker for {self.source.name}: "
            f"{stats['jobs_saved']} jobs saved, "
            f"{stats['jobs_duplicates']} duplicates skipped, "
            f"{stats['jobs_archived']} archived"
        )

        return stats
