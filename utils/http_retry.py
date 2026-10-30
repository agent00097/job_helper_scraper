"""
Retry transient HTTP failures before they count as a company scrape failure.

A DNS blip, a dropped connection, a read timeout or a 502/503/504 from the ATS
says nothing about the company's board, but without a retry it lands on the
Scraper health page as that company failing. 4xx responses (404 board gone,
422 bad site path, 403 blocked) are real answers and are returned immediately.

Wraps the call the source already makes, so existing mocks of
`requests.get` / `requests.post` / `session.get` keep working:

    resp = retry_call(requests.get, url, timeout=30)
    resp.raise_for_status()
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_EXCEPTIONS = (
    requests.exceptions.ConnectionError,   # includes DNS resolution failures
    requests.exceptions.Timeout,           # connect and read timeouts
    requests.exceptions.ChunkedEncodingError,
)
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0


def retry_call(
    fn: Callable[..., requests.Response],
    *args: Any,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    **kwargs: Any,
) -> requests.Response:
    """
    Call fn(*args, **kwargs), retrying transient failures with linear backoff
    (2s, then 4s by default: at most ~6s of extra waiting per call).

    Returns the final response (the caller still calls raise_for_status), or
    re-raises the last transient exception when every attempt failed.
    """
    attempts = max(1, attempts)
    target = args[0] if args else kwargs.get("url", "")
    for attempt in range(1, attempts + 1):
        try:
            resp = fn(*args, **kwargs)
        except RETRY_EXCEPTIONS as exc:
            if attempt == attempts:
                raise
            logger.info("Transient error on %s (attempt %d/%d): %s",
                        target, attempt, attempts, exc)
        else:
            status = getattr(resp, "status_code", None)
            if status not in RETRY_STATUSES or attempt == attempts:
                return resp
            logger.info("HTTP %s on %s (attempt %d/%d), retrying",
                        status, target, attempt, attempts)
            close = getattr(resp, "close", None)
            if callable(close):
                close()
        time.sleep(backoff_seconds * attempt)
    raise AssertionError("unreachable")  # pragma: no cover
