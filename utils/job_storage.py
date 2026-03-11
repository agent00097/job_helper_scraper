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
    # Check for duplicates
    if is_duplicate_job(job):
        logger.debug(f"Duplicate job skipped: {job.url}")
        return False
    
    # Generate content hash
    content_hash = generate_content_hash(job)
    
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
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
                ON CONFLICT (url) DO NOTHING
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
            
            if cur.rowcount > 0:
                logger.info(f"Saved job: {job.job_title} at {job.company}")
                return True
            else:
                logger.debug(f"Job already exists (URL conflict): {job.url}")
                return False
                
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
