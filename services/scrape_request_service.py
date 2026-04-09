"""
Process job scrape request messages: validate payload, enrich when possible, persist via save_job.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum, auto
from typing import Any, Mapping, Union

from models import JobData
from pydantic import BaseModel, HttpUrl, ValidationError
from sources.api.greenhouse_source import GreenhouseSource, parse_greenhouse_board_job_url
from sources.source_factory import create_source
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


def _try_greenhouse_enrich(job: JobData) -> JobData:
    """If URL is a Greenhouse board job link and API fetch succeeds, replace with API JobData."""
    if not parse_greenhouse_board_job_url(str(job.url)):
        return job
    cfg = get_source_config("greenhouse")
    if not cfg or not cfg.get("enabled"):
        return job
    source = create_source(cfg)
    if not isinstance(source, GreenhouseSource):
        return job
    enriched = source.fetch_job_by_board_page_url(str(job.url))
    if enriched is None:
        logger.info("Greenhouse enrich failed; using payload fields for %s", job.url)
        return job
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
    try:
        job = _try_greenhouse_enrich(job)
    except Exception:
        logger.exception("Unexpected error during Greenhouse enrich for %s", job.url)
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
