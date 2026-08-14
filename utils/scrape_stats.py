"""
Persistence helpers for scrape run statistics.

Writes into two tables introduced by infra migration V007:
  - scrape_runs               (one row per source-level run)
  - scrape_company_results    (one row per (run, company))

Kept intentionally defensive: any failure to write stats must NOT break the
actual scraping workflow. All public helpers swallow exceptions and log them.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime
from typing import Optional
from uuid import UUID

import db

logger = logging.getLogger(__name__)


# Keep in sync with the scrape_error_bucket enum in
# job-helper-infra/database/migrations/V007__scrape_run_stats.sql
class ErrorBucket:
    NETWORK = "network"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    PARSE = "parse"
    STORAGE = "storage"
    UNKNOWN = "unknown"


# Cap error_message column so a giant stack trace can't blow up the row.
_ERROR_MESSAGE_MAX_LEN = 2000


def _extract_http_status(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status extraction from common exception shapes."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status

    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None) or getattr(response, "status", None)
        if isinstance(code, int):
            return code
    return None


def classify_exception(exc: BaseException) -> tuple[str, Optional[int]]:
    """
    Classify an exception into (bucket, http_status).

    Order matters: check HTTP status first so requests.HTTPError with a 5xx
    doesn't get swallowed by the generic "network" bucket.
    """
    status = _extract_http_status(exc)
    if status is not None:
        if 400 <= status < 500:
            return ErrorBucket.HTTP_4XX, status
        if 500 <= status < 600:
            return ErrorBucket.HTTP_5XX, status

    module = (getattr(type(exc), "__module__", "") or "").lower()
    name = type(exc).__name__.lower()

    # Network / transport errors
    network_hints = (
        "timeout",
        "connectionerror",
        "connecterror",
        "connecttimeout",
        "readtimeout",
        "sslerror",
        "dnserror",
        "proxyerror",
        "chunkedencodingerror",
        "remotedisconnected",
    )
    if any(hint in name for hint in network_hints):
        return ErrorBucket.NETWORK, status
    if module.startswith(("urllib3", "httpx", "requests", "socket", "ssl")):
        # requests.HTTPError without a status usually already returned above;
        # anything else from these transport libs is a network-layer issue.
        return ErrorBucket.NETWORK, status

    # Parsing / data-shape errors
    parse_hints = (
        "jsondecode",
        "valueerror",
        "keyerror",
        "attributeerror",
        "typeerror",
        "validationerror",
        "parseerror",
        "xmlparse",
        "unicodedecode",
    )
    if any(hint in name for hint in parse_hints):
        return ErrorBucket.PARSE, status

    # Storage errors (Postgres / psycopg / disk)
    if module.startswith(("psycopg", "sqlalchemy")) or "database" in name or "operationalerror" in name:
        return ErrorBucket.STORAGE, status

    return ErrorBucket.UNKNOWN, status


class ScrapeRunRecorder:
    """
    Records a single source-level scrape run and per-company results.

    Usage:
        recorder = ScrapeRunRecorder.start(source_id, source_name, trigger="scheduler")
        for company in companies:
            with recorder.company(company_id) as cr:
                # do work
                cr.mark_success(fetched=42, saved=10, duplicates=32, archived=0)
                # or on exception:
                cr.mark_failure(exc)   # bucket auto-classified
        recorder.finish()   # sets status/finished_at/error_summary
    """

    def __init__(self, run_id: UUID, source_id: str, source_name: str):
        self.run_id = run_id
        self.source_id = source_id
        self.source_name = source_name
        self._started_monotonic = time.monotonic()
        self._companies_processed = 0
        self._companies_succeeded = 0
        self._companies_failed = 0
        self._total_jobs_fetched = 0
        self._total_jobs_saved = 0
        self._total_jobs_duplicates = 0
        self._total_jobs_archived = 0
        self._error_buckets: Counter[str] = Counter()

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    def start(
        cls,
        source_id: str,
        source_name: str,
        trigger: str = "scheduler",
    ) -> Optional["ScrapeRunRecorder"]:
        """Insert a scrape_runs row and return a recorder, or None on failure."""
        try:
            conn = db.get_db_connection()
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_stats: cannot connect to record run start: %s", exc)
            return None

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO scrape_runs (source_id, source_name, status, trigger)
                    VALUES (%s, %s, 'running', %s)
                    RETURNING id
                    """,
                    (source_id, source_name, trigger),
                )
                row = cur.fetchone()
                conn.commit()
                run_id = row[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_stats: failed to insert scrape_runs row: %s", exc)
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        return cls(run_id=run_id, source_id=source_id, source_name=source_name)

    def finish(self, notes: Optional[str] = None) -> None:
        """Finalize the run: compute status, duration, and error_summary."""
        duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
        if self._companies_processed == 0:
            status = "success"
        elif self._companies_failed == 0:
            status = "success"
        elif self._companies_succeeded == 0:
            status = "failed"
        else:
            status = "partial"

        error_summary = dict(self._error_buckets) if self._error_buckets else None

        try:
            conn = db.get_db_connection()
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_stats: cannot connect to finalize run %s: %s", self.run_id, exc)
            return

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE scrape_runs
                    SET status = %s,
                        finished_at = CURRENT_TIMESTAMP,
                        duration_ms = %s,
                        companies_processed = %s,
                        companies_succeeded = %s,
                        companies_failed = %s,
                        total_jobs_fetched = %s,
                        jobs_saved = %s,
                        jobs_duplicates = %s,
                        jobs_archived = %s,
                        error_summary = %s::jsonb,
                        notes = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        status,
                        duration_ms,
                        self._companies_processed,
                        self._companies_succeeded,
                        self._companies_failed,
                        self._total_jobs_fetched,
                        self._total_jobs_saved,
                        self._total_jobs_duplicates,
                        self._total_jobs_archived,
                        _to_json(error_summary),
                        notes,
                        self.run_id,
                    ),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_stats: failed to finalize run %s: %s", self.run_id, exc)
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- per-company recording ------------------------------------------

    def company(self, company_id: str) -> "CompanyResultCtx":
        """Return a context manager that records the per-company row on exit."""
        return CompanyResultCtx(self, company_id)

    def _record_company_result(
        self,
        *,
        company_id: str,
        status: str,
        started_at: datetime,
        duration_ms: int,
        jobs_fetched: int,
        jobs_saved: int,
        jobs_duplicates: int,
        jobs_archived: int,
        error_bucket: Optional[str],
        error_type: Optional[str],
        error_message: Optional[str],
        http_status: Optional[int],
    ) -> None:
        self._companies_processed += 1
        if status == "success":
            self._companies_succeeded += 1
        elif status == "failed":
            self._companies_failed += 1
        self._total_jobs_fetched += jobs_fetched
        self._total_jobs_saved += jobs_saved
        self._total_jobs_duplicates += jobs_duplicates
        self._total_jobs_archived += jobs_archived
        if error_bucket:
            self._error_buckets[error_bucket] += 1

        try:
            conn = db.get_db_connection()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scrape_stats: cannot connect to record company result "
                "(run=%s company=%s): %s",
                self.run_id, company_id, exc,
            )
            return

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO scrape_company_results (
                        run_id, company_id, source_id, source_name,
                        status, started_at, finished_at, duration_ms,
                        jobs_fetched, jobs_saved, jobs_duplicates, jobs_archived,
                        error_bucket, error_type, error_message, http_status
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, CURRENT_TIMESTAMP, %s,
                        %s, %s, %s, %s,
                        %s::scrape_error_bucket, %s, %s, %s
                    )
                    ON CONFLICT (run_id, company_id) DO NOTHING
                    """,
                    (
                        self.run_id, company_id, self.source_id, self.source_name,
                        status, started_at, duration_ms,
                        jobs_fetched, jobs_saved, jobs_duplicates, jobs_archived,
                        error_bucket, error_type,
                        (error_message[:_ERROR_MESSAGE_MAX_LEN] if error_message else None),
                        http_status,
                    ),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scrape_stats: failed to record company result "
                "(run=%s company=%s): %s",
                self.run_id, company_id, exc,
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class CompanyResultCtx:
    """Context manager for a single (run, company) result row."""

    def __init__(self, recorder: ScrapeRunRecorder, company_id: str):
        self._recorder = recorder
        self._company_id = company_id
        self._started_at = datetime.utcnow()
        self._started_monotonic = time.monotonic()
        self._jobs_fetched = 0
        self._jobs_saved = 0
        self._jobs_duplicates = 0
        self._jobs_archived = 0
        self._status: Optional[str] = None
        self._error_bucket: Optional[str] = None
        self._error_type: Optional[str] = None
        self._error_message: Optional[str] = None
        self._http_status: Optional[int] = None

    # explicit setters keep the call sites readable
    def mark_success(
        self,
        *,
        fetched: int = 0,
        saved: int = 0,
        duplicates: int = 0,
        archived: int = 0,
    ) -> None:
        self._jobs_fetched = fetched
        self._jobs_saved = saved
        self._jobs_duplicates = duplicates
        self._jobs_archived = archived
        self._status = "success"

    def mark_failure(self, exc: BaseException, *, fetched: int = 0) -> None:
        self._jobs_fetched = fetched
        self._status = "failed"
        bucket, http_status = classify_exception(exc)
        self._error_bucket = bucket
        self._error_type = type(exc).__name__
        self._error_message = str(exc) or repr(exc)
        self._http_status = http_status

    def mark_skipped(self) -> None:
        self._status = "skipped"

    def __enter__(self) -> "CompanyResultCtx":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # If the caller didn't mark anything, infer from exception state.
        if self._status is None:
            if exc is not None:
                self.mark_failure(exc)
            else:
                # No exception, no explicit call — assume success with zero counts.
                self.mark_success()

        duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
        self._recorder._record_company_result(
            company_id=self._company_id,
            status=self._status or "failed",
            started_at=self._started_at,
            duration_ms=duration_ms,
            jobs_fetched=self._jobs_fetched,
            jobs_saved=self._jobs_saved,
            jobs_duplicates=self._jobs_duplicates,
            jobs_archived=self._jobs_archived,
            error_bucket=self._error_bucket,
            error_type=self._error_type,
            error_message=self._error_message,
            http_status=self._http_status,
        )
        # never suppress the caller's exception
        return False


def _to_json(value):
    """psycopg accepts a python object cast to ::jsonb via json.dumps."""
    if value is None:
        return None
    import json
    return json.dumps(value)
