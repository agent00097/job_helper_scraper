"""
Database utilities for connecting to PostgreSQL.

Uses a small process-wide pool so scrape workers reuse a few warm
connections instead of TCP+TLS handshake per job.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_pool_lock = threading.Lock()
_pool = None


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _connect_kwargs() -> dict:
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT")
    dbname = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    connect_timeout = max(1, _int_env("DB_CONNECT_TIMEOUT", 15))

    if host and port and dbname and user and password is not None:
        return {
            "host": host,
            "port": port,
            "dbname": dbname,
            "user": user,
            "password": password,
            "sslmode": os.getenv("PGSSLMODE", "prefer"),
            "connect_timeout": connect_timeout,
        }

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return {"conninfo": database_url, "connect_timeout": connect_timeout}

    raise ValueError(
        "Database configuration missing: set DB_HOST, DB_PORT, DB_NAME, DB_USER, "
        "and DB_PASSWORD, or DATABASE_URL"
    )


def _make_pool():
    from psycopg_pool import ConnectionPool

    params = _connect_kwargs()
    conninfo = params.pop("conninfo", "")
    min_size = max(0, _int_env("DB_POOL_MIN_SIZE", 0))
    max_size = max(1, _int_env("DB_POOL_MAX_SIZE", 2))
    if min_size > max_size:
        min_size = max_size
    timeout = max(1.0, float(_int_env("DB_POOL_TIMEOUT", 30)))
    max_idle = max(1.0, float(_int_env("DB_POOL_MAX_IDLE", 20)))

    logger.info(
        "Postgres pool min=%s max=%s timeout=%ss max_idle=%ss connect_timeout=%ss",
        min_size,
        max_size,
        int(timeout),
        int(max_idle),
        params.get("connect_timeout"),
    )
    return ConnectionPool(
        conninfo=conninfo,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        max_idle=max_idle,
        kwargs=params,
    )


def get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = _make_pool()
            atexit.register(_close_pool)
    return _pool


def _close_pool() -> None:
    global _pool
    pool = _pool
    if pool is None:
        return
    try:
        pool.close()
    except Exception:
        logger.debug("Postgres pool close failed", exc_info=True)
    _pool = None


class _PooledConnection:
    """Connection whose close() returns it to the pool instead of discarding it."""

    __slots__ = ("_conn", "_pool", "_released")

    def __init__(self, conn, pool):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_released", False)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self) -> None:
        if self._released:
            return
        object.__setattr__(self, "_released", True)
        try:
            self._pool.putconn(self._conn)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass


def _connect_unpooled():
    import psycopg

    params = _connect_kwargs()
    conninfo = params.pop("conninfo", None)
    if conninfo:
        return psycopg.connect(conninfo, **params)
    return psycopg.connect(**params)


def get_db_connection():
    """
    Borrow a PostgreSQL connection from the process pool.

    Callers must close() it; that returns the connection to the pool.
    """
    try:
        pool = get_pool()
    except ImportError:
        logger.warning("psycopg_pool not installed; using an unpooled connection")
        return _connect_unpooled()
    return _PooledConnection(pool.getconn(), pool)
