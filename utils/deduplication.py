"""
Deduplication utilities to prevent duplicate jobs.
"""
import hashlib
from typing import Optional
from models import JobData
import db


def generate_content_hash(job: JobData) -> str:
    """
    Generate a content hash for a job based on key fields.
    
    Args:
        job: JobData object
        
    Returns:
        SHA256 hash string
    """
    # Create a string from key identifying fields
    content_string = f"{job.job_title}|{job.company}|{job.location}|{job.date_posted}"
    return hashlib.sha256(content_string.encode()).hexdigest()


def job_exists_by_url(url: str) -> bool:
    """
    Check if a job with the given URL already exists.
    
    Args:
        url: Job URL
        
    Returns:
        True if job exists, False otherwise
    """
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE url = %s LIMIT 1", (str(url),))
            return cur.fetchone() is not None
    finally:
        conn.close()


def job_exists_by_hash(content_hash: str) -> bool:
    """
    Check if a job with the given content hash already exists.
    
    Args:
        content_hash: Content hash string
        
    Returns:
        True if job exists, False otherwise
    """
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE content_hash = %s LIMIT 1", (content_hash,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def is_duplicate_job(job: JobData) -> bool:
    """
    Check if a job is a duplicate using multiple strategies.
    
    Strategy:
    1. Check URL (exact match)
    2. Check content hash (title + company + location + date)
    
    Args:
        job: JobData object to check
        
    Returns:
        True if duplicate, False otherwise
    """
    # Level 1: URL check (fastest)
    if job_exists_by_url(str(job.url)):
        return True
    
    # Level 2: Content hash check
    content_hash = generate_content_hash(job)
    if job_exists_by_hash(content_hash):
        return True
    
    return False
