"""
Scrape one source_companies row: fetch jobs, save, optional logo onboarding.

Used by the in-process SourceWorker (JobBank / tests) and by the
company_scrape_tasks RabbitMQ consumer.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from sources.base_source import BaseSource
from utils.job_archive import (
    archive_jobs_missing_from_fetch,
    seen_keys_from_jobs,
    supports_presence_reconcile,
)
from utils.job_storage import save_jobs
from utils.source_loader import update_company_last_fetched

logger = logging.getLogger(__name__)

# ATS sources whose companies are dispatched to company_scrape_tasks.
# JobBank is a site-wide incremental crawl, not a per-company board list.
QUEUE_DISPATCH_SOURCES = frozenset({"ashby", "greenhouse", "lever", "workday"})


def uses_company_scrape_queue(source_name: str) -> bool:
    return source_name in QUEUE_DISPATCH_SOURCES


def _company_scrape_timeout_seconds() -> int:
    try:
        return max(0, int(os.environ.get("COMPANY_SCRAPE_TIMEOUT_SECONDS", "600")))
    except ValueError:
        return 600


def queue_existing_company_enrichment(**kwargs):
    """Lazy import keeps RabbitMQ dependencies out of import for unit tests."""
    from services.company_check import queue_existing_company_enrichment as queue

    return queue(**kwargs)


@dataclass
class CompanyScrapeOutcome:
    ok: bool
    jobs_fetched: int = 0
    jobs_saved: int = 0
    jobs_duplicates: int = 0
    jobs_archived: int = 0
    error: Optional[BaseException] = None


def scrape_one_company(source: BaseSource, company: dict[str, Any]) -> CompanyScrapeOutcome:
    """
    Fetch and persist jobs for a single company.

    `company` keys: id, company_name, normalized_name, company_endpoint,
    logo_url (optional), domain (optional).

    Enforces COMPANY_SCRAPE_TIMEOUT_SECONDS (default 600) wall-clock limit so
    one pathological board cannot hold a worker forever.
    """
    timeout = _company_scrape_timeout_seconds()
    if timeout <= 0:
        return _scrape_one_company_impl(source, company)

    company_name = company.get("company_name", "?")
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_scrape_one_company_impl, source, company)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeoutError:
            err = TimeoutError(
                f"{source.name}/{company_name}: exceeded "
                f"{timeout}s company scrape timeout"
            )
            logger.error("%s", err)
            return CompanyScrapeOutcome(ok=False, error=err)


def _scrape_one_company_impl(
    source: BaseSource, company: dict[str, Any]
) -> CompanyScrapeOutcome:
    company_name = company["company_name"]
    company_endpoint = company["company_endpoint"]
    company_id = company["id"]

    try:
        jobs = source.fetch_jobs(company_endpoint, company_name)
        fetched_count = len(jobs)

        for job in jobs:
            job.company_id = UUID(str(company_id))

        if not (company.get("logo_url") or "").strip():
            source_domain_hint = next(
                (job.company_domain_hint for job in jobs if job.company_domain_hint),
                None,
            )
            queue_existing_company_enrichment(
                company_name=company_name,
                normalized_name=company["normalized_name"],
                source_name=source.name,
                source_endpoint=company_endpoint,
                stored_domain=company.get("domain"),
                source_domain_hint=source_domain_hint,
            )

        archived_count = 0
        if supports_presence_reconcile(source.name):
            seen_ids, seen_urls = seen_keys_from_jobs(jobs)
            archived_count = archive_jobs_missing_from_fetch(
                source_website=source.name,
                company_name=company_name,
                seen_source_ids=seen_ids,
                seen_urls=seen_urls,
            )

        saved_count = 0
        duplicates_count = 0
        if jobs:
            saved_count, duplicates_count = save_jobs(
                jobs, source_endpoint=company_endpoint
            )
            logger.info(
                "%s/%s: fetched=%d saved=%d dupes=%d archived=%d",
                source.name,
                company_name,
                fetched_count,
                saved_count,
                duplicates_count,
                archived_count,
            )
        else:
            logger.info("%s/%s: no jobs found", source.name, company_name)

        update_company_last_fetched(company_id)
        return CompanyScrapeOutcome(
            ok=True,
            jobs_fetched=fetched_count,
            jobs_saved=saved_count,
            jobs_duplicates=duplicates_count,
            jobs_archived=archived_count,
        )
    except Exception as exc:
        logger.error("Error processing %s from %s: %s", company_name, source.name, exc)
        return CompanyScrapeOutcome(ok=False, error=exc)
