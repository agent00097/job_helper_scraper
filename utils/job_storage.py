"""
Utilities for storing jobs in the database.
"""
import logging
from typing import List
from datetime import datetime
import db
from models import JobData
from utils.deduplication import is_duplicate_job, generate_content_hash

logger = logging.getLogger(__name__)


def save_job(job: JobData) -> bool:
    """
    Save a single job to the database if it's not a duplicate.
    
    Args:
        job: JobData object to save
        
    Returns:
        True if job was saved, False if duplicate or error
    """
    # Generate content hash
    content_hash = generate_content_hash(job)
    
    # Log description info for debugging
    desc_length = len(job.job_description) if job.job_description else 0
    logger.debug(f"Saving job {job.url}: description length = {desc_length}")
    
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            # First, check if job exists by URL
            cur.execute("SELECT id, job_description FROM jobs WHERE url = %s", (str(job.url),))
            existing = cur.fetchone()
            
            if existing:
                # Job exists - update description if it's NULL or empty
                existing_id, existing_desc = existing
                logger.debug(f"Job exists. Existing description is None: {existing_desc is None}, length: {len(existing_desc) if existing_desc else 0}")
                logger.debug(f"New description is None: {job.job_description is None}, length: {len(job.job_description) if job.job_description else 0}")
                
                if not existing_desc or existing_desc.strip() == '':
                    # Update the description
                    logger.debug(f"Updating job description for job ID {existing_id}")
                    logger.debug(f"Description to save: {job.job_description[:100] if job.job_description else 'None'}...")
                    
                    cur.execute("""
                        UPDATE jobs 
                        SET job_description = %s,
                            last_updated = %s,
                            scraped_at = %s,
                            content_hash = %s
                        WHERE id = %s
                    """, (
                        job.job_description,
                        job.last_updated,
                        job.scraped_at,
                        content_hash,
                        existing_id
                    ))
                    conn.commit()
                    
                    # Verify the update
                    cur.execute("SELECT job_description FROM jobs WHERE id = %s", (existing_id,))
                    updated = cur.fetchone()
                    updated_desc = updated[0] if updated else None
                    logger.debug(f"After update, description in DB is None: {updated_desc is None}, length: {len(updated_desc) if updated_desc else 0}")
                    
                    desc_info = f" (description: {len(job.job_description)} chars)" if job.job_description else " (no description)"
                    logger.info(f"Updated job description: {job.job_title} at {job.company}{desc_info}")
                    return True
                else:
                    logger.debug(f"Job already exists with description: {job.url}")
                    return False
            else:
                # New job - check for duplicates before inserting
                if is_duplicate_job(job):
                    logger.debug(f"Duplicate job skipped: {job.url}")
                    return False
                
                # New job - insert it
                cur.execute("""
                    INSERT INTO jobs (
                        url, job_title, company, location, job_description,
                        date_posted, employment_type, salary_range, experience_level,
                        education_required, skills_required, application_url,
                        sponsorship_required, citizenship_required, remote_allowed,
                        hybrid_allowed, source_website, job_id_from_source, status,
                        last_updated, scraped_at, created_at, content_hash
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                """, (
                    str(job.url), job.job_title, job.company, job.location,
                    job.job_description, job.date_posted, job.employment_type,
                    job.salary_range, job.experience_level, job.education_required,
                    job.skills_required, str(job.application_url) if job.application_url else None,
                    job.sponsorship_required, job.citizenship_required,
                    job.remote_allowed, job.hybrid_allowed, job.source_website,
                    job.job_id_from_source, job.status, job.last_updated,
                    job.scraped_at, job.created_at, content_hash
                ))
                conn.commit()
                desc_info = f" (description: {len(job.job_description)} chars)" if job.job_description else " (no description)"
                logger.info(f"Saved new job: {job.job_title} at {job.company}{desc_info}")
                return True
                
    except Exception as e:
        logger.error(f"Error saving job {job.url}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def save_jobs(jobs: List[JobData]) -> tuple[int, int]:
    """
    Save multiple jobs to the database.
    
    Args:
        jobs: List of JobData objects
        
    Returns:
        Tuple of (saved_count, duplicate_count)
    """
    saved_count = 0
    duplicate_count = 0
    
    for job in jobs:
        if save_job(job):
            saved_count += 1
        else:
            duplicate_count += 1
    
    return saved_count, duplicate_count
