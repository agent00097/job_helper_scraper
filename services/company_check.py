"""
Company existence check and onboarding queue publisher.

Called by save_job before every INSERT. If the company already exists in
source_companies (matched by normalized_name) the UUID is returned and
stamped on the job row. If not found, a message is published to the
company_onboarding queue so the onboarding service can create the row;
the job is saved with company_id=NULL and backfilled later.

A RabbitMQ outage is non-fatal: the job is still saved with company_id=NULL.
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID

import db
import pika
from models import JobData
from utils.company_normalization import normalize_company_name
from workers.rabbitmq_settings import load_rabbitmq_worker_settings

logger = logging.getLogger(__name__)

_ONBOARDING_QUEUE = "company_onboarding"


def ensure_company(
    job_data: JobData,
    source_name: str,
    source_endpoint: str,
) -> Optional[UUID]:
    """Look up company by normalized name; publish onboarding event if absent.

    Returns the company UUID when the row exists, or None when it does not
    (the job is saved with company_id=NULL and backfilled by the onboarding
    service after it processes the queue message).
    """
    if not job_data.company:
        return None

    normalized = normalize_company_name(job_data.company)
    if not normalized:
        return None

    # --- DB lookup ---
    try:
        company_id = _lookup_company(normalized)
    except Exception:
        logger.exception("ensure_company: DB lookup failed for %r", normalized)
        return None

    if company_id is not None:
        return company_id

    # --- Not found: publish onboarding event ---
    # Domain resolution now happens in the company-onboarding-service (it resolves
    # from source_name/source_endpoint when domain_hint is absent), so the scraper
    # publishes domain_hint=None and lets the consumer resolve.
    domain_hint = None

    payload = {
        "company_name": job_data.company,
        "normalized_name": normalized,
        "source_name": source_name,
        "source_endpoint": source_endpoint,
        "domain_hint": domain_hint,
    }
    try:
        _publish_onboarding(payload)
        logger.info(
            "ensure_company: queued onboarding for %r via %s/%s",
            normalized, source_name, source_endpoint,
        )
    except Exception:
        logger.warning(
            "ensure_company: RabbitMQ publish failed for %r (non-fatal, job saved with company_id=NULL)",
            normalized,
        )

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _lookup_company(normalized_name: str) -> Optional[UUID]:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM source_companies WHERE normalized_name = %s",
                (normalized_name,),
            )
            row = cur.fetchone()
            if row:
                return UUID(str(row[0]))
            return None
    finally:
        conn.close()


def _publish_onboarding(payload: dict) -> None:
    """Connect to RabbitMQ, publish one persistent message, close."""
    settings = load_rabbitmq_worker_settings()
    params = pika.ConnectionParameters(
        host=settings.host,
        port=settings.port,
        virtual_host=settings.virtual_host,
        credentials=pika.PlainCredentials(settings.username, settings.password),
        connection_attempts=2,
        retry_delay=1,
        socket_timeout=5,
    )

    connection = pika.BlockingConnection(params)
    try:
        channel = connection.channel()
        channel.queue_declare(queue=_ONBOARDING_QUEUE, durable=True)
        channel.basic_publish(
            exchange="",
            routing_key=_ONBOARDING_QUEUE,
            body=json.dumps(payload).encode(),
            properties=pika.BasicProperties(delivery_mode=2),  # persistent
        )
    finally:
        if connection.is_open:
            connection.close()
