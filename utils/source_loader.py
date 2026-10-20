"""
Utilities for loading source configurations from the database.
"""
import logging
import os
from typing import List, Dict, Optional
import db

logger = logging.getLogger(__name__)


def get_source_config(source_name: str) -> Optional[Dict]:
    """
    Get source configuration from database.
    
    Args:
        source_name: Name of the source (e.g., 'greenhouse')
        
    Returns:
        Dictionary with source config or None if not found
    """
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, type, enabled, schedule_hours, 
                       rate_limit_per_minute, config, last_run_at
                FROM job_sources
                WHERE name = %s
            """, (source_name,))
            
            row = cur.fetchone()
            if not row:
                return None
            
            return {
                "id": str(row[0]),
                "name": row[1],
                "type": row[2],
                "enabled": row[3],
                "schedule_hours": row[4],
                "rate_limit_per_minute": row[5],
                "config": row[6] if row[6] else {},
                "last_run_at": row[7]  # Can be None or datetime
            }
    finally:
        conn.close()


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


# Empty-board backoff (see get_source_companies). Set SCRAPE_SKIP_EMPTY_AFTER
# to 0 to disable. Last N results succeeded with 0 jobs -> back off to one try
# every EMPTY_RETRY_HOURS instead of every scheduler run.
# Dead boards (404) are deliberately NOT skipped: they stay visible on the
# Scraper health page until relocated (scripts/resolve_dead_boards.py).
SKIP_EMPTY_AFTER = "SCRAPE_SKIP_EMPTY_AFTER"          # default 4
EMPTY_RETRY_HOURS = "SCRAPE_EMPTY_RETRY_HOURS"        # default 24

_COMPANY_COLUMNS = """
    sc.id, sc.company_name, sc.normalized_name,
    sc.source_endpoints->>%(source_name)s AS company_endpoint,
    sc.logo_url, sc.domain
"""

_PLAIN_COMPANIES_SQL = f"""
    SELECT {_COMPANY_COLUMNS}
    FROM source_companies sc
    WHERE jsonb_exists(sc.source_endpoints, %(source_name)s)
      AND sc.enabled = TRUE
    ORDER BY sc.company_name ASC
"""

_HEALTH_FILTERED_COMPANIES_SQL = f"""
    WITH recent AS (
        SELECT r.company_id, r.status, r.http_status, r.jobs_fetched, r.finished_at,
               ROW_NUMBER() OVER (
                   PARTITION BY r.company_id ORDER BY r.finished_at DESC
               ) AS rn
        FROM scrape_company_results r
        WHERE r.source_name = %(source_name)s
          AND r.finished_at > NOW() - INTERVAL '14 days'
    ),
    health AS (
        SELECT company_id,
               COUNT(*) FILTER (WHERE rn <= %(empty_n)s) AS empty_window,
               COUNT(*) FILTER (
                   WHERE rn <= %(empty_n)s AND status = 'success' AND jobs_fetched = 0
               ) AS empty_hits,
               MAX(finished_at) AS last_attempt_at
        FROM recent
        GROUP BY company_id
    )
    SELECT {_COMPANY_COLUMNS}
    FROM source_companies sc
    LEFT JOIN health h ON h.company_id = sc.id
    WHERE jsonb_exists(sc.source_endpoints, %(source_name)s)
      AND sc.enabled = TRUE
      AND NOT (
          %(empty_n)s > 0
          AND COALESCE(h.empty_window, 0) >= %(empty_n)s
          AND h.empty_hits = h.empty_window
          AND h.last_attempt_at > NOW() - make_interval(hours => %(empty_retry_hours)s)
      )
    ORDER BY sc.company_name ASC
"""


def _rows_to_companies(rows) -> List[Dict]:
    return [
        {
            "id": str(row[0]),
            "company_name": row[1],
            "normalized_name": row[2],
            "company_endpoint": row[3],
            "logo_url": row[4],
            "domain": row[5],
        }
        for row in rows
    ]


def get_source_companies(source_name: str) -> List[Dict]:
    """
    Get the enabled companies for a source that are worth scraping this run.

    source_endpoints is a JSONB column keyed by source name, e.g.
    {"ashby": "stripe", "workday": "https://stripe.wd5..."}.
    Returns rows whose source_endpoints map contains the given source_name key.

    Empty boards are backed off (not disabled): if the last
    SCRAPE_SKIP_EMPTY_AFTER results for this source succeeded with 0 jobs, the
    company is retried once every SCRAPE_EMPTY_RETRY_HOURS. Failures of any
    kind (404, network, timeout, 5xx) never cause skipping, so dead boards
    stay visible until relocated. If the health query fails for any reason,
    falls back to scraping every enabled company (the previous behaviour).

    Each returned dict exposes .id (company UUID), .company_name, and
    .company_endpoint (the per-source value from the JSONB map).
    """
    params = {
        "source_name": source_name,
        "empty_n": _env_int(SKIP_EMPTY_AFTER, 4),
        "empty_retry_hours": _env_int(EMPTY_RETRY_HOURS, 24),
    }
    conn = db.get_db_connection()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(_HEALTH_FILTERED_COMPANIES_SQL, params)
                companies = _rows_to_companies(cur.fetchall())
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM source_companies "
                    "WHERE jsonb_exists(source_endpoints, %(source_name)s) AND enabled = TRUE",
                    params,
                )
                total = cur.fetchone()[0]
            if total > len(companies):
                logger.info(
                    "%s: skipping %d of %d companies (empty boards, in backoff)",
                    source_name,
                    total - len(companies),
                    total,
                )
            return companies
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s: health-filtered company query failed (%s); scraping all enabled companies",
                source_name,
                exc,
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            with conn.cursor() as cur:
                cur.execute(_PLAIN_COMPANIES_SQL, params)
                return _rows_to_companies(cur.fetchall())
    finally:
        conn.close()


def update_source_last_run(source_id: str):
    """Update the last_run_at timestamp for a source."""
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE job_sources
                SET last_run_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (source_id,))
            conn.commit()
    finally:
        conn.close()


def update_company_last_fetched(company_id: str):
    """Update the last_fetched_at timestamp for a company."""
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE source_companies
                SET last_fetched_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (company_id,))
            conn.commit()
    finally:
        conn.close()
