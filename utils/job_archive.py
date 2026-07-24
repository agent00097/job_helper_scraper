"""
Presence-based archival for ATS board scrapes.

When a company board fetch succeeds, any previously active job for that
company/source that is absent from the live listing is marked removed and
dropped from user_job_matches (apply feed cache). Tracker / applications
rows are left intact.
"""
from __future__ import annotations

import logging
from typing import Iterable, Sequence, Set

import db

logger = logging.getLogger(__name__)

# Sources that return a full current board per company (not incremental).
PRESENCE_RECONCILE_SOURCES = frozenset({"greenhouse", "ashby", "lever", "workday"})

_CLOSED_STATUS = "removed"


def supports_presence_reconcile(source_name: str) -> bool:
    return (source_name or "").strip().lower() in PRESENCE_RECONCILE_SOURCES


def archive_jobs_missing_from_fetch(
    *,
    source_website: str,
    company_name: str,
    seen_source_ids: Iterable[str],
    seen_urls: Iterable[str],
) -> int:
    """
    Mark active jobs missing from a successful board fetch as removed.

    Matching is scoped by source_website + company name (same values written
    on JobData during fetch). Identity uses job_id_from_source and/or url.

    Returns the number of jobs archived.
    """
    source = (source_website or "").strip()
    company = (company_name or "").strip()
    if not source or not company:
        return 0

    ids: Set[str] = {str(x).strip() for x in seen_source_ids if x and str(x).strip()}
    urls: Set[str] = {str(x).strip() for x in seen_urls if x and str(x).strip()}
    if not ids and not urls:
        # Successful empty board — archive all active jobs for this company/source.
        pass

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            # Collect ids to archive first (for match-cache cleanup).
            cur.execute(
                """
                SELECT id
                FROM jobs
                WHERE lower(source_website) = lower(%s)
                  AND lower(company) = lower(%s)
                  AND COALESCE(status, 'active') = 'active'
                  AND NOT (
                    (job_id_from_source IS NOT NULL AND job_id_from_source = ANY(%s))
                    OR (url = ANY(%s))
                  )
                """,
                (source, company, list(ids), list(urls)),
            )
            job_ids = [str(row[0]) for row in cur.fetchall()]
            if not job_ids:
                return 0

            cur.execute(
                """
                UPDATE jobs
                SET status = %s,
                    last_updated = CURRENT_TIMESTAMP
                WHERE id = ANY(%s::uuid[])
                """,
                (_CLOSED_STATUS, job_ids),
            )
            archived = cur.rowcount

            cur.execute(
                """
                DELETE FROM user_job_matches
                WHERE job_id = ANY(%s::uuid[])
                """,
                (job_ids,),
            )
            matches_deleted = cur.rowcount
            conn.commit()

            logger.info(
                "Archived %d job(s) for %s / %s (removed %d user_job_matches row(s))",
                archived,
                source,
                company,
                matches_deleted,
            )
            return int(archived or 0)
    except Exception:
        conn.rollback()
        logger.exception(
            "Failed to archive missing jobs for %s / %s", source, company
        )
        return 0
    finally:
        conn.close()


def seen_keys_from_jobs(jobs: Sequence) -> tuple[Set[str], Set[str]]:
    """Extract job_id_from_source and url sets from JobData-like objects."""
    ids: Set[str] = set()
    urls: Set[str] = set()
    for job in jobs:
        sid = getattr(job, "job_id_from_source", None)
        if sid is not None and str(sid).strip():
            ids.add(str(sid).strip())
        url = getattr(job, "url", None)
        if url is not None and str(url).strip():
            urls.add(str(url).strip())
    return ids, urls
