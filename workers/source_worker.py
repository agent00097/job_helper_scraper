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
from utils.scrape_stats import ScrapeRunRecorder

logger = logging.getLogger(__name__)


def queue_existing_company_enrichment(**kwargs):
    """Lazy import keeps RabbitMQ dependencies out of worker module import."""
    from services.company_check import queue_existing_company_enrichment as queue

    return queue(**kwargs)


class SourceWorker:
    """Worker that processes a single source."""

    def __init__(self, source: BaseSource, run_trigger: str = "scheduler"):
        """
        Initialize the worker.

        Args:
            source: BaseSource instance to work with
            run_trigger: How this run was triggered — one of
                'scheduler', 'manual', 'on_demand', 'backfill'.
                Persisted on the scrape_runs row.
        """
        self.source = source
        self.run_trigger = run_trigger

    def run(self) -> dict:
        """
        Run the source worker to fetch and save jobs.

        Returns:
            Dictionary with statistics about the run.
        """
        logger.info(f"Starting worker for source: {self.source.name}")

        # Open a scrape_runs row up-front. If the DB is unreachable this returns
        # None; we still run the scrape, just without persisted stats.
        recorder = ScrapeRunRecorder.start(
            source_id=self.source.source_id,
            source_name=self.source.name,
            trigger=self.run_trigger,
        )

        # Get all companies for this source (new JSONB schema — keyed by source name)
        companies = get_source_companies(self.source.name)

        if not companies:
            logger.warning(f"No companies found for source: {self.source.name}")
            if recorder is not None:
                recorder.finish(notes="No companies configured for this source.")
            return {
                "source": self.source.name,
                "companies_processed": 0,
                "total_jobs_fetched": 0,
                "jobs_saved": 0,
                "jobs_duplicates": 0,
                "jobs_archived": 0,
                "errors": [],
                "run_id": str(recorder.run_id) if recorder else None,
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

            # Use a null-safe context so the loop still works when the recorder
            # couldn't be created (e.g. transient DB blip at run start).
            company_ctx = (
                recorder.company(company_id) if recorder is not None else _NullCompanyCtx()
            )

            with company_ctx as cr:
                try:
                    logger.info(f"Fetching jobs for {company_name} from {self.source.name}")

                    # Fetch jobs from source. API sources raise on HTTP failure so we
                    # never treat a failed board pull as "all jobs closed".
                    jobs = self.source.fetch_jobs(company_endpoint, company_name)
                    fetched_count = len(jobs)
                    total_jobs_fetched += fetched_count

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

                    archived_count = 0
                    if reconcile:
                        seen_ids, seen_urls = seen_keys_from_jobs(jobs)
                        archived_count = archive_jobs_missing_from_fetch(
                            source_website=self.source.name,
                            company_name=company_name,
                            seen_source_ids=seen_ids,
                            seen_urls=seen_urls,
                        )
                        total_jobs_archived += archived_count

                    saved_count = 0
                    duplicates_count = 0
                    if jobs:
                        # source_endpoint remains useful for non-scheduler callers;
                        # these jobs already carry company_id.
                        saved_count, duplicates_count = save_jobs(
                            jobs, source_endpoint=company_endpoint
                        )
                        total_jobs_saved += saved_count
                        total_jobs_duplicates += duplicates_count

                        logger.info(
                            f"{company_name}: Fetched {fetched_count} jobs, "
                            f"saved {saved_count}, duplicates {duplicates_count}"
                        )
                    else:
                        logger.info(f"{company_name}: No jobs found")

                    # Update company last fetched timestamp
                    update_company_last_fetched(company_id)

                    cr.mark_success(
                        fetched=fetched_count,
                        saved=saved_count,
                        duplicates=duplicates_count,
                        archived=archived_count,
                    )

                except Exception as e:
                    error_msg = f"Error processing {company_name}: {str(e)}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    cr.mark_failure(e)
                    # Swallow so one bad company doesn't kill the whole run.
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
            "run_id": str(recorder.run_id) if recorder else None,
        }

        if recorder is not None:
            recorder.finish()

        logger.info(
            f"Completed worker for {self.source.name}: "
            f"{stats['jobs_saved']} jobs saved, "
            f"{stats['jobs_duplicates']} duplicates skipped, "
            f"{stats['jobs_archived']} archived"
        )

        return stats


class _NullCompanyCtx:
    """No-op stand-in used when the ScrapeRunRecorder could not be created."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def mark_success(self, **_kwargs):
        pass

    def mark_failure(self, _exc, **_kwargs):
        pass

    def mark_skipped(self):
        pass
