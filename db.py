"""
Database utilities for connecting to PostgreSQL.
"""
import os
import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_db_connection():
    """
    Return a PostgreSQL connection.

    Prefer discrete DB_* variables (Kubernetes / production) so passwords with
    special characters are not embedded in a URL. Fall back to DATABASE_URL
    for local development.
    """
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT")
    dbname = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    if host and port and dbname and user and password is not None:
        sslmode = os.getenv("PGSSLMODE", "prefer")
        return psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            connect_timeout=5,
        )

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg.connect(database_url, connect_timeout=5)

    raise ValueError(
        "Database configuration missing: set DB_HOST, DB_PORT, DB_NAME, DB_USER, "
        "and DB_PASSWORD, or DATABASE_URL"
    )
