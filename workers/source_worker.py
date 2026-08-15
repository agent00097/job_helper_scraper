"""
Worker for running a job source and fetching jobs (in-process).

ATS company lists are normally dispatched to company_scrape_tasks; this
worker remains for JobBank and for tests / force-run of a full source.
"""
import logging
import time

from sources.base_source import BaseSource
from services.company_scrape import scrape_one_company
from utils.source_loader import get_source_companies, update_source_last_run
from utils.scrape_stats import ScrapeRunRecorder

logger = logging.getLogger(__name__)

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


class SourceWorker:
    """Worker that processes a single source in this process."""

    def __init__(self, source: BaseSource, run_trigger: str = "scheduler"):
        self.source = source
        self.run_trigger = run_trigger

    def run(self) -> dict:
        logger.info(
            "scrape_run begin source=%s trigger=%s source_id=%s mode=in-process",
            self.source.name,
            self.run_trigger,
            self.source.source_id,
        )

        recorder = ScrapeRunRecorder.start(
            source_id=self.source.source_id,
            source_name=self.source.name,
            trigger=self.run_trigger,
        )
        run_id = str(recorder.run_id) if recorder is not None else None
        if recorder is None:
            logger.warning(
                "scrape_run stats recorder unavailable source=%s",
                self.source.name,
            )

        companies = get_source_companies(self.source.name)

        if not companies:
            logger.warning("No companies found for source: %s", self.source.name)
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
        started_at = time.monotonic()

        for index, company in enumerate(companies, start=1):
            company_ctx = (
                recorder.company(company["id"]) if recorder is not None else _NullCompanyCtx()
            )
            with company_ctx as cr:
                outcome = scrape_one_company(self.source, company)
                if outcome.ok:
                    cr.mark_success(
                        fetched=outcome.jobs_fetched,
                        saved=outcome.jobs_saved,
                        duplicates=outcome.jobs_duplicates,
                        archived=outcome.jobs_archived,
                    )
                    ok_count += 1
                    total_jobs_fetched += outcome.jobs_fetched
                    total_jobs_saved += outcome.jobs_saved
                    total_jobs_duplicates += outcome.jobs_duplicates
                    total_jobs_archived += outcome.jobs_archived
                else:
                    if outcome.error is not None:
                        errors.append(f"Error processing {company['company_name']}: {outcome.error}")
                        cr.mark_failure(outcome.error)
                    fail_count += 1

            if index == 1 or index % _PROGRESS_EVERY == 0 or index == total:
                _log_run_progress(
                    source_name=self.source.name,
                    run_id=run_id,
                    index=index,
                    total=total,
                    elapsed=time.monotonic() - started_at,
                    ok=ok_count,
                    failed=fail_count,
                    last_company=company["company_name"],
                )

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
