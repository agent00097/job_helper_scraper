"""
Publish/consume per-company scrape tasks on RabbitMQ queue company_scrape_tasks.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from services.company_scrape import scrape_one_company
from services.scrape_request_service import MessageDisposition
from sources.source_factory import create_source
from utils.scrape_stats import ScrapeRunRecorder, record_dropped_company_task
from utils.source_loader import get_source_config
from workers.rabbitmq_settings import load_rabbitmq_worker_settings

logger = logging.getLogger(__name__)

COMPANY_SCRAPE_QUEUE = os.environ.get("COMPANY_SCRAPE_QUEUE", "company_scrape_tasks")

_source_cache: dict[str, Any] = {}


class CompanyScrapeTask(BaseModel):
    run_id: str
    source_id: str
    source_name: str
    company_id: str
    company_name: str
    normalized_name: str
    company_endpoint: str
    logo_url: Optional[str] = None
    domain: Optional[str] = None
    trigger: str = "scheduler"


def publish_company_scrape_tasks(
    *,
    run_id: str,
    source_id: str,
    source_name: str,
    companies: list[dict],
    trigger: str = "scheduler",
) -> int:
    """Publish one durable message per company. Returns count published."""
    import pika

    settings = load_rabbitmq_worker_settings()
    queue_name = COMPANY_SCRAPE_QUEUE
    params = pika.ConnectionParameters(
        host=settings.host,
        port=settings.port,
        virtual_host=settings.virtual_host,
        credentials=pika.PlainCredentials(settings.username, settings.password),
        heartbeat=settings.heartbeat,
        blocked_connection_timeout=settings.blocked_connection_timeout,
    )
    published = 0
    connection = None
    try:
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        channel.queue_declare(queue=queue_name, durable=True)
        channel.confirm_delivery()
        for company in companies:
            task = CompanyScrapeTask(
                run_id=run_id,
                source_id=source_id,
                source_name=source_name,
                company_id=str(company["id"]),
                company_name=company["company_name"],
                normalized_name=company["normalized_name"],
                company_endpoint=company["company_endpoint"],
                logo_url=company.get("logo_url"),
                domain=company.get("domain"),
                trigger=trigger,
            )
            channel.basic_publish(
                exchange="",
                routing_key=queue_name,
                body=task.model_dump_json().encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                ),
                mandatory=True,
            )
            published += 1
            if published == 1 or published % 500 == 0:
                logger.info(
                    "scrape_run publish source=%s run_id=%s %d/%d",
                    source_name,
                    run_id,
                    published,
                    len(companies),
                )
    except Exception:
        logger.exception(
            "scrape_run publish failed source=%s run_id=%s after %d/%d",
            source_name,
            run_id,
            published,
            len(companies),
        )
    finally:
        if connection is not None and connection.is_open:
            connection.close()
    logger.info(
        "scrape_run published source=%s run_id=%s count=%d queue=%s",
        source_name,
        run_id,
        published,
        queue_name,
    )
    return published


def _source_for(source_name: str):
    cached = _source_cache.get(source_name)
    if cached is not None:
        return cached
    cfg = get_source_config(source_name)
    if not cfg or not cfg.get("enabled"):
        return None
    source = create_source(cfg)
    if source is not None:
        _source_cache[source_name] = source
    return source


def _credit_drop_from_partial(data: dict, reason: str) -> None:
    """If enough identity fields exist, write a failed result so the run can drain."""
    run_id = data.get("run_id")
    source_id = data.get("source_id")
    source_name = data.get("source_name")
    company_id = data.get("company_id")
    if not all(
        isinstance(v, str) and v.strip()
        for v in (run_id, source_id, source_name, company_id)
    ):
        return
    ok = record_dropped_company_task(
        run_id=str(run_id),
        source_id=str(source_id),
        source_name=str(source_name),
        company_id=str(company_id),
        reason=reason,
    )
    if not ok:
        logger.error(
            "company_scrape_tasks: failed to credit drop for run=%s company=%s",
            run_id,
            company_id,
        )


def process_company_scrape_body(body: bytes | str) -> MessageDisposition:
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("company_scrape_tasks: body is not UTF-8")
            return MessageDisposition.NACK_NO_REQUEUE
    else:
        text = body
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("company_scrape_tasks: invalid JSON: %s", e)
        return MessageDisposition.NACK_NO_REQUEUE
    if not isinstance(data, dict):
        logger.warning("company_scrape_tasks: JSON root must be an object")
        return MessageDisposition.NACK_NO_REQUEUE

    try:
        task = CompanyScrapeTask.model_validate(data)
    except ValidationError as e:
        logger.warning("company_scrape_tasks: invalid payload: %s", e)
        _credit_drop_from_partial(data, f"invalid payload: {e}")
        return MessageDisposition.NACK_NO_REQUEUE

    source = _source_for(task.source_name)
    if source is None:
        logger.error(
            "company_scrape_tasks: cannot load source %s — requeue",
            task.source_name,
        )
        return MessageDisposition.NACK_REQUEUE

    company = {
        "id": task.company_id,
        "company_name": task.company_name,
        "normalized_name": task.normalized_name,
        "company_endpoint": task.company_endpoint,
        "logo_url": task.logo_url,
        "domain": task.domain,
    }
    recorder = ScrapeRunRecorder.attach(task.run_id, task.source_id, task.source_name)
    with recorder.company(task.company_id) as cr:
        outcome = scrape_one_company(source, company)
        if outcome.ok:
            cr.mark_success(
                fetched=outcome.jobs_fetched,
                saved=outcome.jobs_saved,
                duplicates=outcome.jobs_duplicates,
                archived=outcome.jobs_archived,
            )
        elif outcome.error is not None:
            cr.mark_failure(outcome.error, fetched=outcome.jobs_fetched)
        else:
            cr.mark_failure(RuntimeError("scrape failed"), fetched=outcome.jobs_fetched)

    if not cr.persisted:
        logger.error(
            "company_scrape_tasks: result not persisted run=%s company=%s — requeue",
            task.run_id,
            task.company_id,
        )
        return MessageDisposition.NACK_REQUEUE

    return MessageDisposition.ACK
