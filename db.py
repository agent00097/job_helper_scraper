"""
Database utilities for connecting to PostgreSQL.
"""
import os
import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_db_connection():
    """Get a database connection using DATABASE_URL environment variable."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required")
    
    return psycopg.connect(
        database_url,
        connect_timeout=5  # 5 second timeout
    )
