"""
Keep long-lived scraper workers from retaining memory across companies.

Python threads cannot be killed. A timed-out scrape that is left running
while the worker takes the next RabbitMQ message stacks HTTP sessions,
parse trees, and job lists until the pod is OOMKilled. On timeout we
flag the process for recycle; after the message is ACKed the worker
exits so the OS reaps every leftover thread and buffer.
"""
from __future__ import annotations

import gc
import logging
import os
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_recycle_reason: str | None = None
_tasks_done = 0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def abandon_scrape_on_timeout() -> bool:
    """Company-queue workers should abandon hung scrapes and recycle the process."""
    return _env_bool("COMPANY_SCRAPE_ABANDON_ON_TIMEOUT", False)


def recycle_reason() -> str | None:
    with _lock:
        return _recycle_reason


def recycle_requested() -> bool:
    return recycle_reason() is not None


def request_recycle(reason: str) -> None:
    global _recycle_reason
    with _lock:
        if _recycle_reason is not None:
            return
        _recycle_reason = reason
    logger.warning("Worker recycle requested: %s", reason)


def reset_recycle_state_for_tests() -> None:
    """Test-only: clear recycle flag and task counter."""
    global _recycle_reason, _tasks_done
    with _lock:
        _recycle_reason = None
        _tasks_done = 0


def rss_mb() -> float | None:
    """Current resident set size in MiB, or None if unavailable."""
    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            resident_pages = int(fh.read().split()[1])
        page = os.sysconf("SC_PAGE_SIZE")
        return resident_pages * page / (1024 * 1024)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def collect_garbage(*, generation: int = 2) -> None:
    try:
        gc.collect(generation)
    except Exception:
        logger.debug("gc.collect failed", exc_info=True)


def log_rss(prefix: str) -> None:
    used = rss_mb()
    if used is None:
        return
    logger.info("%s rss_mb=%.1f", prefix, used)


def note_company_task_finished() -> None:
    """
    After each company_scrape_tasks message: drop garbage and recycle
    the process every COMPANY_SCRAPE_MAX_TASKS_PER_PROCESS (0 disables).
    """
    global _tasks_done
    with _lock:
        _tasks_done += 1
        done = _tasks_done
    collect_garbage()
    if done == 1 or done % 10 == 0:
        log_rss(f"company_scrape_tasks processed={done}")
    max_tasks = _env_int("COMPANY_SCRAPE_MAX_TASKS_PER_PROCESS", 80)
    if max_tasks > 0 and done >= max_tasks:
        request_recycle(f"max tasks per process ({max_tasks})")


def close_idle_resources() -> None:
    """Best-effort close of process-wide pools before a recycle exit."""
    collect_garbage()
    try:
        import db

        db.close_pool()
    except Exception:
        logger.debug("close_pool during recycle failed", exc_info=True)


def stop_consumer_for_recycle(channel, already_stopping: bool = False) -> bool:
    """
    Stop a pika blocking consumer after ACK so the process can recycle.

    Returns True when this call initiated the stop.
    """
    if already_stopping or not recycle_requested():
        return False
    logger.warning("Stopping consumer so this worker can recycle")
    try:
        channel.stop_consuming()
    except Exception as e:
        logger.debug("stop_consuming after recycle: %s", e)
    return True


def recycle_current_process(exit_code: int = 0) -> None:
    """
    Exit immediately so leftover non-daemon scrape threads cannot keep
    the pod alive. os._exit skips atexit; close pools first.
    """
    reason = recycle_reason() or "unspecified"
    log_rss(f"recycling worker reason={reason}")
    close_idle_resources()
    logger.warning("Exiting process for recycle (code=%s)", exit_code)
    os._exit(exit_code)
