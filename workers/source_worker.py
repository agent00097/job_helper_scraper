"""
Worker for running a job source and fetching jobs.
"""
import logging
import time
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

# INFO heartbeat so kubectl logs stay readable on multi-thousand-company runs.
_PROGRESS_EVERY = 25


def _eta_seconds(done: int, total: int, elapsed: float) -> int | None:
    if done <= 0 or elapsed <= 0 or done >= total:
        return 0 if done >= total else None
    return int((total - done) * (elapsed / done))


def _log_run_progress(
    *,
    source_name: str,
    run_id: str | None,
    index: int,
    total: int,
    elapsed: float,
    ok: int,
    failed: int,
    last_company: str,
) -> None:
    eta = _eta_seconds(index, total, elapsed)
    pct = (100.0 * index / total) if total else 100.0
    logger.info(
        "scrape_run progress source=%s run_id=%s %d/%d (%.1f%%) "
        "elapsed=%ds eta=%s ok=%d fail=%d last=%s",
        source_name,
        run_id or "none",
        index,
        total,
        pct,
        int(elapsed),
        f"{eta}s" if eta is not None else "?",
        ok,
        failed,
        last_company,
    )


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
        logger.info(
            "scrape_run begin source=%s trigger=%s source_id=%s",
            self.source.name,
            self.run_trigger,
            self.source.source_id,
        )

        # Open a scrape_runs row up-front. If the DB is unreachable this returns
        # None; we still run the scrape, just without persisted stats.
        recorder = ScrapeRunRecorder.start(
            source_id=self.source.source_id,
            source_name=self.source.name,
            trigger=self.run_trigger,
        )
        run_id = str(recorder.run_id) if recorder is not None else None
        if recorder is None:
            logger.warning(
                "scrape_run stats recorder unavailable source=%s — "
                "companies will scrape but admin 'last run' stays empty",
                self.source.name,
            )
        else:
            logger.info("scrape_run opened source=%s run_id=%s", self.source.name, run_id)

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
                "run_id": run_id,
            }

        total = len(companies)
        logger.info(
            "scrape_run loaded source=%s run_id=%s companies=%d first=%s last=%s",
            self.source.name,
            run_id or "none",
            total,
            companies[0]["company_name"],
            companies[-1]["company_name"],
        )

        total_jobs_fetched = 0
        total_jobs_saved = 0
        total_jobs_duplicates = 0
        total_jobs_archived = 0
        errors = []
        ok_count = 0
        fail_count = 0
        reconcile = supports_presence_reconcile(self.source.name)
        started_at = time.monotonic()

        # Process each company
        for index, company in enumerate(companies, start=1):
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
                    logger.debug(
                        "Fetching jobs for %s from %s (%d/%d)",
                        company_name,
                        self.source.name,
                        index,
                        total,
                    )

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
                    ok_count += 1

                except Exception as e:
                    error_msg = f"Error processing {company_name}: {str(e)}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    cr.mark_failure(e)
                    fail_count += 1
                    # Swallow so one bad company doesn't kill the whole run.

            if index == 1 or index % _PROGRESS_EVERY == 0 or index == total:
                _log_run_progress(
                    source_name=self.source.name,
                    run_id=run_id,
                    index=index,
                    total=total,
                    elapsed=time.monotonic() - started_at,
                    ok=ok_count,
                    failed=fail_count,
                    last_company=company_name,
                )

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
            "run_id": run_id,
        }

        if recorder is not None:
            recorder.finish()

        logger.info(
            "scrape_run complete source=%s run_id=%s companies=%d ok=%d fail=%d "
            "fetched=%d saved=%d dupes=%d archived=%d elapsed=%ds",
            self.source.name,
            run_id or "none",
            total,
            ok_count,
            fail_count,
            total_jobs_fetched,
            total_jobs_saved,
            total_jobs_duplicates,
            total_jobs_archived,
            int(time.monotonic() - started_at),
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
