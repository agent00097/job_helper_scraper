"""
Utilities for storing jobs in the database.
"""
import logging
from typing import List, Optional
from datetime import datetime
import db
from models import JobData
from utils.deduplication import is_duplicate_job, generate_content_hash
from utils.geo import parse_location
from utils.role_classifier import classify_role

logger = logging.getLogger(__name__)


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
    from services.company_check import ensure_company  # local import avoids circular dep

    # Resolve company_id (or None) before touching the DB transaction
    company_id = job.company_id
    if company_id is None and source_endpoint:
        company_id = ensure_company(job, job.source_website, source_endpoint)

    # Generate content hash
    content_hash = generate_content_hash(job)

    # Log description info for debugging
    desc_length = len(job.job_description) if job.job_description else 0
    logger.debug(f"Saving job {job.url}: description length = {desc_length}")

    conn = db.get_db_connection()
    saved_job_id = None
    try:
        with conn.cursor() as cur:
            # First, check if job exists by URL
            cur.execute("SELECT id, job_description FROM jobs WHERE url = %s", (str(job.url),))
            existing = cur.fetchone()

            if existing:
                # Job exists - update description if it's NULL or empty
                existing_id, existing_desc = existing
                logger.debug(
                    f"Job exists. Existing description is None: {existing_desc is None}, "
                    f"length: {len(existing_desc) if existing_desc else 0}"
                )
                logger.debug(
                    f"New description is None: {job.job_description is None}, "
                    f"length: {len(job.job_description) if job.job_description else 0}"
                )

                if not existing_desc or existing_desc.strip() == "":
                    # Update the description
                    logger.debug(f"Updating job description for job ID {existing_id}")
                    logger.debug(
                        f"Description to save: "
                        f"{job.job_description[:100] if job.job_description else 'None'}..."
                    )

                    # Phase 3 wiring — pending DB validation (add_role_function_to_jobs.sql).
                    try:
                        _rc = classify_role(job.job_title or "", getattr(job, "noc_code", None))
                        role_function = _rc.get("role_function")
                    except Exception:
                        role_function = None

                    geo = _geo_fields(job.location)

                    cur.execute(
                        """
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
                        """,
                        (
                            job.job_description,
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
                            existing_id,
                        ),
                    )
                    conn.commit()

                    # Verify the update
                    cur.execute("SELECT job_description FROM jobs WHERE id = %s", (existing_id,))
                    updated = cur.fetchone()
                    updated_desc = updated[0] if updated else None
                    logger.debug(
                        f"After update, description in DB is None: {updated_desc is None}, "
                        f"length: {len(updated_desc) if updated_desc else 0}"
                    )

                    desc_info = (
                        f" (description: {len(job.job_description)} chars)"
                        if job.job_description
                        else " (no description)"
                    )
                    logger.info(
                        f"Updated job description: {job.job_title} at {job.company}{desc_info}"
                    )
                    saved_job_id = existing_id
                else:
                    logger.debug(f"Job already exists with description: {job.url}")
                    return False
            else:
                # New job - check for duplicates before inserting
                if is_duplicate_job(job):
                    logger.debug(f"Duplicate job skipped: {job.url}")
                    return False

                # New job - insert it
                geo = _geo_fields(job.location)

                # Phase 3 wiring — pending DB validation (add_role_function_to_jobs.sql).
                try:
                    _rc = classify_role(job.job_title or "", getattr(job, "noc_code", None))
                    role_function = _rc.get("role_function")
                except Exception:
                    role_function = None

                cur.execute(
                    """
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
                    """,
                    (
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
                    ),
                )
                row = cur.fetchone()
                saved_job_id = row[0] if row else None
                conn.commit()
                desc_info = (
                    f" (description: {len(job.job_description)} chars)"
                    if job.job_description
                    else " (no description)"
                )
                logger.info(f"Saved new job: {job.job_title} at {job.company}{desc_info}")

    except Exception as e:
        logger.error(f"Error saving job {job.url}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

    if saved_job_id is not None:
        _extract_skills_safe(saved_job_id, job.job_title, job.job_description)
        return True
    return False


def save_jobs(
    jobs: List[JobData],
    source_endpoint: Optional[str] = None,
) -> tuple[int, int]:
    """
    Save multiple jobs to the database.

    source_endpoint is forwarded to save_job for company resolution;
    see save_job docstring for details.

    Returns (saved_count, duplicate_count).
    """
    saved_count = 0
    duplicate_count = 0

    for job in jobs:
        if save_job(job, source_endpoint=source_endpoint):
            saved_count += 1
        else:
            duplicate_count += 1

    return saved_count, duplicate_count
