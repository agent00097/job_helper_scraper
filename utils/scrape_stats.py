"""
Persistence helpers for scrape run statistics.

Writes into two tables introduced by infra migration V007:
  - scrape_runs               (one row per source-level run)
  - scrape_company_results    (one row per (run, company))

Kept intentionally defensive: any failure to write stats must NOT break the
actual scraping workflow. All public helpers swallow exceptions and log them.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

import db

logger = logging.getLogger(__name__)

# Persist retries: never ACK a company task until a result row lands.
_PERSIST_ATTEMPTS = 3
_PERSIST_RETRY_SLEEP_SECONDS = 0.25

# Stale queue-run recovery (env-overridable). A run is abandoned when it is
# older than SCRAPE_QUEUE_STALE_AFTER_HOURS AND has made no new company
# progress for SCRAPE_QUEUE_STALL_MINUTES (or never got any results).
def _stale_after_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("SCRAPE_QUEUE_STALE_AFTER_HOURS", "4")))
    except ValueError:
        return 4.0


def _stall_minutes() -> float:
    try:
        return max(0.0, float(os.environ.get("SCRAPE_QUEUE_STALL_MINUTES", "60")))
    except ValueError:
        return 60.0


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

    # Storage before generic "timeout" matching: psycopg.ConnectionTimeout
    # would otherwise land in the network bucket.
    if module.startswith(("psycopg", "sqlalchemy")) or "database" in name or "operationalerror" in name:
        return ErrorBucket.STORAGE, status

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

    @classmethod
    def attach(cls, run_id: str, source_id: str, source_name: str) -> "ScrapeRunRecorder":
        """Bind to an existing scrape_runs row (queue workers)."""
        return cls(run_id=UUID(str(run_id)), source_id=source_id, source_name=source_name)

    def set_queue_meta(self, queued: int) -> None:
        """Mark this run as queue-dispatched with an expected company count."""
        notes = json.dumps({"mode": "queue", "queued": int(queued)})
        try:
            conn = db.get_db_connection()
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_stats: cannot connect to set queue meta: %s", exc)
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE scrape_runs
                    SET notes = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (notes, self.run_id),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_stats: failed to set queue meta for %s: %s", self.run_id, exc)
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

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
    ) -> bool:
        """Insert the company result row. Returns True if persisted (or already existed)."""
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

        params = (
            self.run_id, company_id, self.source_id, self.source_name,
            status, started_at, duration_ms,
            jobs_fetched, jobs_saved, jobs_duplicates, jobs_archived,
            error_bucket, error_type,
            (error_message[:_ERROR_MESSAGE_MAX_LEN] if error_message else None),
            http_status,
        )
        last_exc: Optional[BaseException] = None
        for attempt in range(1, _PERSIST_ATTEMPTS + 1):
            conn = None
            try:
                conn = db.get_db_connection()
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
                        params,
                    )
                    conn.commit()
                return True
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "scrape_stats: failed to record company result "
                    "(run=%s company=%s attempt=%d/%d): %s",
                    self.run_id, company_id, attempt, _PERSIST_ATTEMPTS, exc,
                )
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                if attempt < _PERSIST_ATTEMPTS:
                    time.sleep(_PERSIST_RETRY_SLEEP_SECONDS * attempt)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

        logger.error(
            "scrape_stats: giving up recording company result "
            "(run=%s company=%s): %s",
            self.run_id, company_id, last_exc,
        )
        return False


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
        self.persisted: bool = False

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

    def mark_skipped(self, reason: str = "skipped") -> None:
        self._status = "skipped"
        self._error_type = "skipped"
        self._error_message = reason

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
        self.persisted = self._recorder._record_company_result(
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


def record_dropped_company_task(
    *,
    run_id: str,
    source_id: str,
    source_name: str,
    company_id: str,
    reason: str,
) -> bool:
    """Credit a terminal drop so finalize can reach queued count. Returns persist ok."""
    recorder = ScrapeRunRecorder.attach(run_id, source_id, source_name)
    with recorder.company(company_id) as cr:
        cr.mark_failure(RuntimeError(reason))
    return cr.persisted


def _to_json(value):
    """psycopg accepts a python object cast to ::jsonb via json.dumps."""
    if value is None:
        return None
    return json.dumps(value)


def source_has_active_queue_run(source_id: str) -> bool:
    """True if this source still has a queue-dispatched scrape_runs row in 'running'."""
    try:
        conn = db.get_db_connection()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape_stats: cannot check active queue run: %s", exc)
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM scrape_runs
                WHERE source_id = %s
                  AND status = 'running'
                  AND notes IS NOT NULL
                  AND notes LIKE '{%%'
                  AND notes::jsonb->>'mode' = 'queue'
                LIMIT 1
                """,
                (source_id,),
            )
            return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape_stats: active queue run check failed: %s", exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _parse_queue_meta(notes) -> tuple[Optional[dict], int]:
    try:
        meta = json.loads(notes) if isinstance(notes, str) else (notes or {})
        if not isinstance(meta, dict):
            return None, 0
        queued = int(meta.get("queued") or 0)
        return meta, queued
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, 0


def _close_queue_run(
    conn,
    *,
    run_id: str,
    source_id: str,
    source_name: str,
    started_at,
    done: int,
    ok: int,
    failed: int,
    fetched: int,
    saved: int,
    dupes: int,
    archived: int,
    notes: Optional[str] = None,
    log_label: str = "complete",
    incomplete: bool = False,
) -> bool:
    if done == 0:
        status = "failed"
    elif incomplete:
        # Missing results / abandoned early — never report pure success.
        status = "partial" if ok > 0 else "failed"
    elif failed == 0:
        status = "success"
    elif ok == 0:
        status = "failed"
    else:
        status = "partial"

    duration_ms = None
    if started_at is not None:
        try:
            duration_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        except Exception:  # noqa: BLE001
            duration_ms = None

    with conn.cursor() as cur:
        if notes is not None:
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
                    notes = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'running'
                """,
                (
                    status,
                    duration_ms,
                    done,
                    ok,
                    failed,
                    fetched,
                    saved,
                    dupes,
                    archived,
                    notes,
                    run_id,
                ),
            )
        else:
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
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'running'
                """,
                (
                    status,
                    duration_ms,
                    done,
                    ok,
                    failed,
                    fetched,
                    saved,
                    dupes,
                    archived,
                    run_id,
                ),
            )
        if not cur.rowcount:
            return False
        cur.execute(
            """
            UPDATE job_sources
            SET last_run_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (source_id,),
        )
    logger.info(
        "scrape_run %s source=%s run_id=%s status=%s done=%d ok=%d fail=%d",
        log_label,
        source_name,
        run_id,
        status,
        done,
        ok,
        failed,
    )
    return True


def _company_result_counts(conn, run_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS done,
                COUNT(*) FILTER (WHERE status = 'success') AS ok,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                COALESCE(SUM(jobs_fetched), 0),
                COALESCE(SUM(jobs_saved), 0),
                COALESCE(SUM(jobs_duplicates), 0),
                COALESCE(SUM(jobs_archived), 0),
                MAX(finished_at) AS last_finished
            FROM scrape_company_results
            WHERE run_id = %s
            """,
            (run_id,),
        )
        return cur.fetchone()


def finalize_completed_queue_runs() -> int:
    """Close queue-mode scrape_runs whose company results have caught up.

    Also abandons stale runs that will never reach queued (lost messages /
    unrecorded results) so the scheduler can dispatch again.

    Returns the number of runs finalized. Also stamps job_sources.last_run_at.
    """
    try:
        conn = db.get_db_connection()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape_stats: cannot finalize queue runs: %s", exc)
        return 0

    finalized = 0
    stale_after = timedelta(hours=_stale_after_hours())
    stall_for = timedelta(minutes=_stall_minutes())
    now = datetime.utcnow()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, source_id::text, source_name, notes, started_at
                FROM scrape_runs
                WHERE status = 'running'
                  AND notes IS NOT NULL
                  AND notes LIKE '{%%'
                  AND notes::jsonb->>'mode' = 'queue'
                """
            )
            rows = cur.fetchall()

        for run_id, source_id, source_name, notes, started_at in rows:
            meta, queued = _parse_queue_meta(notes)
            if meta is None:
                continue

            row = _company_result_counts(conn, run_id)
            done = int(row[0] or 0)
            ok = int(row[1] or 0)
            failed = int(row[2] or 0)
            fetched = int(row[3] or 0)
            saved = int(row[4] or 0)
            dupes = int(row[5] or 0)
            archived = int(row[6] or 0)
            last_finished = row[7]

            if queued > 0 and done >= queued:
                if _close_queue_run(
                    conn,
                    run_id=run_id,
                    source_id=source_id,
                    source_name=source_name,
                    started_at=started_at,
                    done=done,
                    ok=ok,
                    failed=failed,
                    fetched=fetched,
                    saved=saved,
                    dupes=dupes,
                    archived=archived,
                    log_label="complete",
                ):
                    finalized += 1
                conn.commit()
                continue

            if queued > 0 and done < queued:
                logger.info(
                    "scrape_run drain source=%s run_id=%s %d/%d",
                    source_name,
                    run_id,
                    done,
                    queued,
                )

            # Stale / stuck recovery (including queued<=0 left open forever)
            if started_at is None:
                continue
            try:
                started = started_at.replace(tzinfo=None) if getattr(started_at, "tzinfo", None) else started_at
            except Exception:  # noqa: BLE001
                started = started_at
            age = now - started
            if age < stale_after:
                continue

            last_progress = last_finished or started
            try:
                last_progress = (
                    last_progress.replace(tzinfo=None)
                    if getattr(last_progress, "tzinfo", None)
                    else last_progress
                )
            except Exception:  # noqa: BLE001
                pass
            stall = now - last_progress
            if stall < stall_for:
                continue

            meta = dict(meta)
            meta["abandoned_stale"] = True
            meta["abandoned_at"] = now.isoformat() + "Z"
            meta["abandoned_reason"] = (
                f"no progress for {stall_for}; age {age}; done={done} queued={queued}"
            )
            if _close_queue_run(
                conn,
                run_id=run_id,
                source_id=source_id,
                source_name=source_name,
                started_at=started_at,
                done=done,
                ok=ok,
                failed=failed,
                fetched=fetched,
                saved=saved,
                dupes=dupes,
                archived=archived,
                notes=json.dumps(meta),
                log_label="abandon_stale",
                incomplete=True,
            ):
                finalized += 1
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape_stats: finalize queue runs failed: %s", exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return finalized
