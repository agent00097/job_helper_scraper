# Recommended cron entry (server time must be ET, or use explicit TZ):
#   0 23 * * * /path/to/venv/bin/python /path/to/scripts/jobbank_backfill.py >> /var/log/jobbank_backfill.log 2>&1
#
# Runs nightly at 23:00 ET with an 8-hour budget (28 800 s).
# Once backfill_completed_at is set the script exits immediately — no scraping.

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from sources.scraper.jobbank_source import JobBankSource
from utils.source_loader import get_source_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("jobbank_backfill")


def _get_progress(source_id: str) -> dict | None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT backfill_last_page, backfill_completed_at FROM scrape_progress WHERE source_id = %s",
                (source_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"backfill_last_page": row[0], "backfill_completed_at": row[1]}
    finally:
        conn.close()


def main() -> None:
    cfg = get_source_config("jobbank")
    if not cfg:
        logger.error("jobbank source not found in job_sources — run setup_jobbank_source.sql first")
        sys.exit(1)

    source_id = cfg["id"]

    progress = _get_progress(source_id)
    if progress and progress.get("backfill_completed_at"):
        logger.info(
            "Backfill already completed at %s (page %d) — nothing to do",
            progress["backfill_completed_at"],
            progress.get("backfill_last_page", 0),
        )
        return

    resume_page = progress["backfill_last_page"] if progress else 0
    logger.info("Starting backfill: resuming after page %d", resume_page)

    # Fetch the company_endpoint from the single "Job Bank (Canada)" source_companies row.
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT company_endpoint FROM source_companies WHERE source_id = %s AND enabled = TRUE LIMIT 1",
                (source_id,),
            )
            row = cur.fetchone()
            company_endpoint = row[0] if row else ""
    finally:
        conn.close()

    # company_endpoint for Job Bank is a query-string fragment (e.g. "keywords=nurse"),
    # not a full URL. If the DB row was seeded with a full URL, pass "" so _build_url
    # doesn't double-append it.
    if company_endpoint and company_endpoint.startswith("http"):
        company_endpoint = ""

    source = JobBankSource(
        name=cfg["name"],
        source_id=source_id,
        config=cfg.get("config", {}),
        rate_limit_per_minute=cfg.get("rate_limit_per_minute", 30),
        mode="backfill",
        max_runtime_seconds=28_800,  # 8 hours
        max_jobs=100_000,
        skip_db=False,
    )

    source.fetch_jobs(company_endpoint, "Job Bank Canada")
    logger.info("Backfill script complete.")


if __name__ == "__main__":
    main()
