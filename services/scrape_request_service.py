"""
Process job scrape request messages: validate payload, enrich when possible, persist via save_job.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum, auto
from typing import Any, Mapping, Union

import db
from models import JobData
from pydantic import BaseModel, HttpUrl, ValidationError
from sources.api.ashby_source import AshbySource
from sources.api.greenhouse_source import GreenhouseSource
from sources.api.lever_source import LeverSource
from sources.api.workday_source import WorkdaySource
from sources.source_factory import create_source
from utils.company_discovery import parse_company_from_url, source_name_for_url
from utils.deduplication import generate_content_hash, job_exists_by_hash, job_exists_by_url
from utils.job_storage import save_job
from utils.source_loader import get_source_config

logger = logging.getLogger(__name__)


class MessageDisposition(Enum):
    """How the AMQP consumer should handle the delivery."""

    ACK = auto()
    NACK_NO_REQUEUE = auto()
    NACK_REQUEUE = auto()


class JobScrapeRequestPayload(BaseModel):
    """Payload shape published by scripts/bloomberry_parser.py (asdict(JobEntry))."""

    url: HttpUrl
    title: str | None = None
    rank: int | None = None
    location: str | None = None
    company: str | None = None
    salary: str | None = None
    summary: str | None = None
    email_subject: str | None = None
    email_sender: str | None = None
    source: str = "email_raw_manual"


class JobScrapeRequestMessage(BaseModel):
    event_id: str | None = None
    event_type: str | None = None
    occurred_at: str | None = None
    payload: JobScrapeRequestPayload


def job_data_from_payload(payload: JobScrapeRequestPayload) -> JobData:
    """Build JobData from a validated queue payload (metadata from the publisher)."""
    now = datetime.now()
    return JobData(
        url=payload.url,
        job_title=payload.title,
        company=payload.company,
        location=payload.location,
        job_description=payload.summary,
        salary_range=payload.salary,
        source_website=payload.source,
        job_id_from_source=None,
        status="active",
        scraped_at=now,
        created_at=now,
    )


def try_enrich_by_url(job: JobData) -> JobData:
    """Inspect job.url, route to the matching source, return an API-enriched JobData.

    Supported hostnames:
      boards.greenhouse.io / job-boards.greenhouse.io  -> Greenhouse
      jobs.ashbyhq.com                                 -> Ashby
      jobs.lever.co                                    -> Lever
      *.myworkdayjobs.com                              -> Workday

    Falls back to the original job unchanged if the URL doesn't match a known
    source, the source is disabled/unconfigured, or the API call fails.
    """
    url_str = str(job.url)
    source_name = source_name_for_url(url_str)
    if source_name is None:
        return job

    cfg = get_source_config(source_name)
    if not cfg or not cfg.get("enabled"):
        return job

    source = create_source(cfg)
    if source is None:
        return job

    try:
        if isinstance(source, GreenhouseSource):
            enriched = source.fetch_job_by_board_page_url(url_str)
        elif isinstance(source, (AshbySource, LeverSource, WorkdaySource)):
            enriched = source.fetch_job_by_url(url_str)
        else:
            logger.warning("Source %s has no URL-based enrichment method", type(source).__name__)
            return job
    except Exception:
        logger.exception("%s enrich raised unexpectedly for %s", source_name, url_str)
        return job

    if enriched is None:
        logger.info("%s enrich returned nothing for %s; keeping payload fields", source_name, url_str)
        return job

    # Preserve company from the email payload when the API response didn't resolve it
    # (e.g. Workday detail endpoint doesn't include company name).
    if not enriched.company and job.company:
        enriched = enriched.model_copy(update={"company": job.company})

    return enriched


def persist_scrape_job(job: JobData) -> bool:
    """
    Persist using the same path as the scheduler (save_job). Returns True if the outcome
    should be treated as success for ACK (insert, update, duplicate skip, or existing row).
    """
    if save_job(job):
        return True
    if job_exists_by_url(str(job.url)):
        return True
    if job_exists_by_hash(generate_content_hash(job)):
        return True
    return False


def _auto_discover_company(url: str) -> None:
    """Insert a newly-seen (source, company_endpoint) pair into source_companies.

    Phase 2 wiring — untested pending DB validation.

    Called once per inbound queue message; a discovery failure must never
    affect job processing, so callers always wrap this in try/except.

    Open question for review: auto-discovered companies are inserted with
    enabled=TRUE so they are picked up on the next scheduler run. An
    alternative is enabled=FALSE (manual review before activation).
    """
    info = parse_company_from_url(url)
    if info is None:
        return

    source_name = info["source"]
    company_endpoint = info["company_endpoint"]
    company_name = info["company_name"]

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM job_sources WHERE name = %s AND enabled = TRUE",
                (source_name,),
            )
            row = cur.fetchone()
            if not row:
                logger.debug(
                    "Auto-discovery: source %r not found or disabled, skipping %s",
                    source_name, url,
                )
                return
            source_id = row[0]

            cur.execute(
                """
                INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (source_id, company_endpoint) DO NOTHING
                """,
                (source_id, company_name, company_endpoint),
            )
            if cur.rowcount:
                logger.info(
                    "Auto-discovered new company: %s (%s via %s)",
                    company_name, company_endpoint, source_name,
                )
            conn.commit()
    finally:
        conn.close()


def process_job_scrape_request_dict(data: Mapping[str, Any]) -> MessageDisposition:
    """
    Full pipeline for one message body (already decoded JSON object).

    Invalid JSON shape / missing url -> NACK without requeue.
    Transient-style failures -> NACK with requeue per worker config (caller passes via separate path);
    here we return NACK_REQUEUE when persist_scrape_job returns False.
    """
    try:
        message = JobScrapeRequestMessage.model_validate(data)
    except ValidationError as e:
        logger.warning("Invalid job scrape request message: %s", e)
        return MessageDisposition.NACK_NO_REQUEUE

    job = job_data_from_payload(message.payload)

    # Phase 2 — auto-discovery: register new (source, company) pairs seen in
    # the queue so the scheduler picks them up on its next run.
    try:
        _auto_discover_company(str(message.payload.url))
    except Exception:
        logger.exception("Auto-discovery failed for %s (non-fatal)", message.payload.url)

    try:
        job = try_enrich_by_url(job)
    except Exception:
        logger.exception("Unexpected error during URL enrich for %s", job.url)
        return MessageDisposition.NACK_REQUEUE

    try:
        ok = persist_scrape_job(job)
    except Exception:
        logger.exception("Unexpected error persisting job %s", job.url)
        return MessageDisposition.NACK_REQUEUE

    if ok:
        return MessageDisposition.ACK
    logger.error("Failed to save job and no existing row for url=%s", job.url)
    return MessageDisposition.NACK_REQUEUE


def process_job_scrape_request_body(body: Union[bytes, str]) -> MessageDisposition:
    """Parse JSON bytes/str then run process_job_scrape_request_dict."""
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Message body is not valid UTF-8")
            return MessageDisposition.NACK_NO_REQUEUE
    else:
        text = body
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON in message body: %s", e)
        return MessageDisposition.NACK_NO_REQUEUE
    if not isinstance(data, dict):
        logger.warning("JSON root must be an object")
        return MessageDisposition.NACK_NO_REQUEUE
    return process_job_scrape_request_dict(data)
