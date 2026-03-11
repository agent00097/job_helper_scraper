"""
Utilities for loading source configurations from the database.
"""
import logging
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


def get_source_companies(source_id: str) -> List[Dict]:
    """
    Get all enabled companies for a source.
    
    Args:
        source_id: UUID of the source
        
    Returns:
        List of company dictionaries
    """
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, company_name, company_endpoint, enabled
                FROM source_companies
                WHERE source_id = %s AND enabled = TRUE
            """, (source_id,))
            
            companies = []
            for row in cur.fetchall():
                companies.append({
                    "id": str(row[0]),
                    "company_name": row[1],
                    "company_endpoint": row[2],
                    "enabled": row[3]
                })
            
            return companies
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
