"""
Utilities for storing jobs in the database.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import db
from models import JobData
from utils.deduplication import generate_content_hash
from utils.geo import parse_location
from utils.role_classifier import classify_role

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO jobs (
    url, job_title, company, location, job_description,
    date_posted, employment_type, salary_range, experience_level,
    education_required, skills_required, application_url,
    sponsorship_required, citizenship_required, remote_allowed,
    hybrid_allowed, source_website, job_id_from_source, status,
    last_updated, scraped_at, created_at, content_hash,
    country_code, admin1_code, admin1_name, locality, geo_precision,
    occupation_category, role_function, company_id
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s
)
RETURNING id
"""

_UPDATE_DESC_SQL = """
UPDATE jobs
SET job_description = %s,
    last_updated = %s,
    scraped_at = %s,
    content_hash = %s,
    occupation_category = COALESCE(occupation_category, %s),
    role_function = COALESCE(role_function, %s),
    company_id = COALESCE(company_id, %s),
    country_code = COALESCE(country_code, %s),
    admin1_code = COALESCE(admin1_code, %s),
    admin1_name = COALESCE(admin1_name, %s),
    locality = COALESCE(locality, %s),
    geo_precision = COALESCE(NULLIF(geo_precision, 'unknown'), %s)
WHERE id = %s
"""


def _chunk_size() -> int:
    try:
        return max(1, int(os.environ.get("SAVE_JOBS_CHUNK_SIZE", "100")))
    except ValueError:
        return 100


def _geo_fields(location: Optional[str]) -> dict:
    """Parse free-text location into DB columns; never raises."""
    try:
        parts = parse_location(location) if location else parse_location(None)
        return parts.as_dict()
    except Exception:
        return {
            "country_code": None,
            "admin1_code": None,
            "admin1_name": None,
            "locality": None,
            "geo_precision": "unknown",
        }


def _role_function(job: JobData) -> Optional[str]:
    try:
        return classify_role(job.job_title or "", getattr(job, "noc_code", None)).get(
            "role_function"
        )
    except Exception:
        return None


def _extract_skills_safe(job_id, title: Optional[str], description: Optional[str]) -> None:
    """Best-effort skill extraction; never fails the scrape save path."""
    if not job_id or not description or not str(description).strip():
        return
    try:
        from services.skill_extraction_service import extract_skills_for_job

        extract_skills_for_job(job_id, title, description)
    except Exception as exc:
        logger.warning(
            "Skill extraction failed for job %s (job still saved): %s",
            job_id,
            exc,
            exc_info=True,
        )


def _release_description(job: JobData) -> None:
    """Drop large description text after persist so big boards do not retain it."""
    try:
        job.job_description = None
    except Exception:
        pass


def _insert_params(job: JobData, company_id, content_hash: str, geo: dict, role_function):
    return (
        str(job.url),
        job.job_title,
        job.company,
        job.location,
        job.job_description,
        job.date_posted,
        job.employment_type,
        job.salary_range,
        job.experience_level,
        job.education_required,
        job.skills_required,
        str(job.application_url) if job.application_url else None,
        job.sponsorship_required,
        job.citizenship_required,
        job.remote_allowed,
        job.hybrid_allowed,
        job.source_website,
        job.job_id_from_source,
        job.status,
        job.last_updated,
        job.scraped_at,
        job.created_at,
        content_hash,
        geo["country_code"],
        geo["admin1_code"],
        geo["admin1_name"],
        geo["locality"],
        geo["geo_precision"],
        job.occupation_category,
        role_function,
        str(company_id) if company_id else None,
    )


def _lookup_existing(cur, urls: List[str]) -> dict[str, tuple]:
    """url -> (id, needs_description). Does not fetch description text."""
    if not urls:
        return {}
    cur.execute(
        """
        SELECT url, id,
               (job_description IS NULL OR btrim(job_description) = '') AS needs_description
        FROM jobs
        WHERE url = ANY(%s)
        """,
        (urls,),
    )
    found: dict[str, tuple] = {}
    for url, job_id, needs_description in cur.fetchall():
        found[str(url)] = (job_id, bool(needs_description))
    return found


def _existing_hashes(cur, hashes: List[str]) -> set[str]:
    if not hashes:
        return set()
    cur.execute(
        "SELECT content_hash FROM jobs WHERE content_hash = ANY(%s)",
        (hashes,),
    )
    return {str(row[0]) for row in cur.fetchall() if row and row[0]}


def _persist_chunk(
    jobs: List[JobData],
    source_endpoint: Optional[str],
) -> tuple[int, int, list[tuple]]:
    """
    Persist one chunk on a single borrowed connection.

    Returns (saved_count, duplicate_count, skill_jobs).
    skill_jobs is (job_id, title, description) for post-commit extraction.
    """
    from services.company_check import ensure_company

    saved_count = 0
    duplicate_count = 0
    skill_jobs: list[tuple] = []

    urls = [str(job.url) for job in jobs]
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            existing = _lookup_existing(cur, urls)

            insert_candidates: list[JobData] = []
            for job in jobs:
                url = str(job.url)
                row = existing.get(url)
                if row is None:
                    insert_candidates.append(job)
                    continue
                job_id, needs_description = row
                if not needs_description:
                    duplicate_count += 1
                    _release_description(job)
                    continue

                company_id = job.company_id
                if company_id is None and source_endpoint:
                    company_id = ensure_company(job, job.source_website, source_endpoint)
                content_hash = generate_content_hash(job)
                geo = _geo_fields(job.location)
                role_function = _role_function(job)
                description = job.job_description
                cur.execute(
                    _UPDATE_DESC_SQL,
                    (
                        description,
                        job.last_updated,
                        job.scraped_at,
                        content_hash,
                        job.occupation_category,
                        role_function,
                        str(company_id) if company_id else None,
                        geo["country_code"],
                        geo["admin1_code"],
                        geo["admin1_name"],
                        geo["locality"],
                        geo["geo_precision"],
                        job_id,
                    ),
                )
                saved_count += 1
                skill_jobs.append((job_id, job.job_title, description))
                _release_description(job)

            hashes = [generate_content_hash(job) for job in insert_candidates]
            known_hashes = _existing_hashes(cur, hashes)

            for job in insert_candidates:
                content_hash = generate_content_hash(job)
                if content_hash in known_hashes:
                    duplicate_count += 1
                    _release_description(job)
                    continue

                company_id = job.company_id
                if company_id is None and source_endpoint:
                    company_id = ensure_company(job, job.source_website, source_endpoint)
                geo = _geo_fields(job.location)
                role_function = _role_function(job)
                description = job.job_description
                cur.execute(
                    _INSERT_SQL,
                    _insert_params(job, company_id, content_hash, geo, role_function),
                )
                row = cur.fetchone()
                new_id = row[0] if row else None
                if new_id is not None:
                    known_hashes.add(content_hash)
                    saved_count += 1
                    skill_jobs.append((new_id, job.job_title, description))
                else:
                    duplicate_count += 1
                _release_description(job)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return saved_count, duplicate_count, skill_jobs


def save_job(
    job: JobData,
    source_endpoint: Optional[str] = None,
) -> bool:
    """
    Save a single job to the database if it's not a duplicate.

    source_endpoint: the ATS-specific company slug/URL (e.g. "stripe" for Ashby).
    When provided, ensure_company is called to resolve or queue company onboarding.
    JobBank jobs omit this (no per-company endpoint) and are saved with company_id=NULL.

    On successful insert (or description fill-in update), runs skill extraction into
    job_skills (alias + embeddings when configured).

    Returns True if the job was saved or updated, False if duplicate or error.
    """
    try:
        saved, _dupes = save_jobs([job], source_endpoint=source_endpoint)
        return saved > 0
    except Exception as exc:
        logger.error("Error saving job %s: %s", job.url, exc)
        return False


def save_jobs(
    jobs: List[JobData],
    source_endpoint: Optional[str] = None,
) -> tuple[int, int]:
    """
    Save multiple jobs using one pooled connection per chunk.

    Existing URLs are resolved with a single `url = ANY(...)` lookup (no
    description payload). New rows are inserted on that same connection.

    Returns (saved_count, duplicate_count).
    """
    if not jobs:
        return 0, 0

    saved_count = 0
    duplicate_count = 0
    size = _chunk_size()

    for offset in range(0, len(jobs), size):
        chunk = jobs[offset : offset + size]
        try:
            saved, dupes, skill_jobs = _persist_chunk(chunk, source_endpoint)
        except Exception as exc:
            logger.error(
                "Error saving job chunk offset=%s size=%s: %s",
                offset,
                len(chunk),
                exc,
            )
            raise
        saved_count += saved
        duplicate_count += dupes
        for job_id, title, description in skill_jobs:
            _extract_skills_safe(job_id, title, description)

    return saved_count, duplicate_count
