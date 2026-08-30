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
from services.ats_logo_hint import resolve_ats_logo_hint
from utils.company_normalization import normalize_company_name
from utils.domain_resolver import is_junk_slug, resolve_domain
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
    domain_hint = getattr(job_data, "company_domain_hint", None) or None
    try:
        handle = _company_handle(source_name, source_endpoint)
        if domain_hint:
            logger.debug(
                "ensure_company: using source-provided domain hint %r",
                domain_hint,
            )
        elif is_junk_slug(handle, job_data.company or ""):
            logger.debug(
                "ensure_company: skipping domain resolution for junk slug %r",
                handle,
            )
        else:
            domain_hint = resolve_domain(job_data.company or "", handle)
    except Exception:
        logger.warning(
            "ensure_company: domain resolution raised unexpectedly for %r", source_endpoint
        )
        domain_hint = None

    # Capture the ATS-hosted logo URL while the board is being scraped. The
    # onboarding worker only uses this after domain-based methods fail.
    logo_hint_url = None
    try:
        logo_hint_url = resolve_ats_logo_hint(source_name, source_endpoint)
    except Exception:
        logger.warning(
            "ensure_company: ATS logo hint resolution raised unexpectedly for %r",
            source_endpoint,
        )

    payload = {
        "company_name": job_data.company,
        "normalized_name": normalized,
        "source_name": source_name,
        "source_endpoint": source_endpoint,
        "domain_hint": domain_hint,
        "logo_hint_url": logo_hint_url,
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


def queue_existing_company_enrichment(
    *,
    company_name: str,
    normalized_name: str,
    source_name: str,
    source_endpoint: str,
    stored_domain: Optional[str] = None,
    source_domain_hint: Optional[str] = None,
) -> bool:
    """Queue enrichment for an existing source_companies row.

    Scheduled scrapes start from existing company rows, so this is the normal
    path for filling a missing logo. Returns whether publishing succeeded.
    """
    domain_hint = stored_domain or source_domain_hint
    if not domain_hint:
        try:
            handle = _company_handle(source_name, source_endpoint)
            if not is_junk_slug(handle, company_name):
                domain_hint = resolve_domain(company_name, handle)
        except Exception:
            logger.warning(
                "Company enrichment domain resolution failed for %r",
                source_endpoint,
            )

    logo_hint_url = None
    try:
        logo_hint_url = resolve_ats_logo_hint(source_name, source_endpoint)
    except Exception:
        logger.warning(
            "Company enrichment ATS logo resolution failed for %r",
            source_endpoint,
        )

    payload = {
        "company_name": company_name,
        "normalized_name": normalized_name,
        "source_name": source_name,
        "source_endpoint": source_endpoint,
        "domain_hint": domain_hint,
        "logo_hint_url": logo_hint_url,
    }
    try:
        _publish_onboarding(payload)
        logger.info(
            "Queued enrichment for existing company %r via %s/%s",
            normalized_name,
            source_name,
            source_endpoint,
        )
        return True
    except Exception:
        logger.warning(
            "Company enrichment publish failed for %r",
            normalized_name,
        )
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _company_handle(source_name: str, source_endpoint: str) -> str:
    """Bare company handle for domain resolution.

    Most ATS sources store the slug directly in source_endpoint (e.g. "stripe").
    Workday stores a full board URL (e.g. "https://stripe.wd5.myworkdayjobs.com/careers");
    the first subdomain label is the tenant name we want.
    SuccessFactors stores a career-site origin (e.g. "https://jobs.sap.com");
    prefer ?company= when present, otherwise the brand host label.
    """
    if source_name in ("workday", "successfactors"):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(source_endpoint)
        if source_name == "successfactors":
            company = (parse_qs(parsed.query).get("company") or [""])[0]
            if company:
                return company
        host = parsed.hostname or ""
        parts = [p for p in host.split(".") if p]
        if source_name == "successfactors":
            skip = {"www", "jobs", "job", "careers", "career"}
            while len(parts) > 2 and parts[0].lower() in skip:
                parts = parts[1:]
        if parts and parts[0]:
            return parts[0]
    return source_endpoint


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
