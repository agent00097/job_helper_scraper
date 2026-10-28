"""
Build a reviewed plan for broken company job boards ("Find broken boards").

Triggered by the backend publishing {"run_id": ...} to the
source_reconcile_tasks queue after inserting a source_reconcile_runs row.
Reads recent scrape results from the database, decides what to do with each
(company, source) that has really stopped working, searches for the board the
company uses now, and writes proposals to source_reconcile_actions.

It NEVER changes source_companies. Applying (and undoing) a proposal is a
separate, admin-approved step in the backend, which re-checks the board live
first.

Safety rules (a source is only considered broken when ALL hold):
  * its last MIN_FAILED_RUNS results all failed, spanning at least
    MIN_FAILURE_SPAN_HOURS, with no successful run in LOOKBACK_DAYS;
  * every one of those failures is a "board is gone" error (404/410/422,
    Workday site/host gone, 400/405 not caused by our own bugs) or a 403.
    Temporary errors and scraper bugs (timeouts, DB pool, language code in
    URL, DB insert errors, 5xx, DNS blips on shared API hosts) never qualify;
  * a source is only removed when the company keeps another source that
    scraped successfully WITH jobs in the lookback window;
  * a relocation is only proposed for a board that answered live with jobs
    and isn't already scraped by another company row.
Empty boards (successful runs with 0 jobs for the whole window) are only
considered when the run asks for it.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import db
from services.scrape_request_service import MessageDisposition
from utils import board_finder as bf

logger = logging.getLogger(__name__)

SOURCE_RECONCILE_QUEUE = os.environ.get("SOURCE_RECONCILE_QUEUE", "source_reconcile_tasks")
LOOKBACK_DAYS = int(os.environ.get("RECONCILE_LOOKBACK_DAYS", "7"))
MIN_FAILED_RUNS = int(os.environ.get("RECONCILE_MIN_FAILED_RUNS", "3"))
MIN_FAILURE_SPAN_HOURS = int(os.environ.get("RECONCILE_MIN_FAILURE_SPAN_HOURS", "24"))
SEARCH_WORKERS = int(os.environ.get("RECONCILE_SEARCH_WORKERS", "6"))

_SHARED_API_HOSTS = (
    "boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co",
    "api.ashbyhq.com", "api.smartrecruiters.com",
)
BROKEN_CATEGORIES = {"board_gone", "blocked"}


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------
def classify_failure(source: str, reason: str, http_status: Optional[int] = None) -> tuple[str, str]:
    """
    (category, label) for one failed result.

      transient    network blip or our own capacity issue
      code_fixed   caused by a scraper bug (timeouts, DB errors, locale in URL)
      external     the company's own site problem (expired SSL)
      board_gone   the board/endpoint no longer exists or is wrong
      blocked      the site refuses us (403)
    """
    r = reason or ""
    m = re.search(r"\b(\d{3}) (?:Client|Server) Error", r)
    status = http_status or (int(m.group(1)) if m else None)

    if "couldn't get a connection" in r:
        return "transient", "scraper DB pool exhausted (our side)"
    if re.search(r"exceeded \d+s company scrape timeout", r):
        return "code_fixed", "company scrape timeout"
    if "duplicate key value" in r or "index row size" in r:
        return "code_fixed", "database insert error"
    if "certificate" in r.lower():
        return "external", "expired/invalid SSL certificate"
    if status in (429, 500, 502, 503, 504) or "Read timed out" in r or "Connection aborted" in r \
            or "RemoteDisconnected" in r or "ConnectTimeout" in r:
        return "transient", f"temporary error ({status or 'timeout'})"
    if "NameResolution" in r or "Failed to resolve" in r:
        hm = re.search(r"host='([^']+)'", r)
        host = hm.group(1) if hm else ""
        if host in _SHARED_API_HOSTS or ("startrow=" in r and "startrow=0" not in r):
            return "transient", "DNS blip"
        return "board_gone", f"host does not resolve ({host})"
    if source == "workday" and status in (404, 405) and \
            re.search(r"/wday/cxs/[^/]+/[a-z]{2}(?:-[A-Za-z]{2})?(?:/|/jobs)", r):
        return "code_fixed", "language code in Workday URL"
    if source == "successfactors" and status == 404 and \
            re.search(r"\.com/[a-z]{2}(?:_[A-Z]{2})?/tile-search", r):
        return "code_fixed", "language code in SuccessFactors URL"
    if "has no site name" in r:
        return "board_gone", "Workday URL has only a language code"
    if status == 403:
        return "blocked", "403 Forbidden"
    if status in (400, 404, 405, 410, 422):
        return "board_gone", f"HTTP {status}"
    return "transient", "unclassified"


# --------------------------------------------------------------------------
# Loading state from the database
# --------------------------------------------------------------------------
@dataclass
class Company:
    id: str
    name: str
    sources: dict[str, str]
    domain: Optional[str]
    enabled: bool


@dataclass
class SourceHealth:
    company_id: str
    source: str
    runs: int = 0
    successes_with_jobs: int = 0
    successes_empty: int = 0
    last_status: str = ""
    last_failures: list[tuple] = field(default_factory=list)   # (finished_at, http_status, message)


_HEALTH_SQL = """
    SELECT r.company_id::text, r.source_name, r.status, r.http_status,
           r.error_message, r.jobs_fetched, r.finished_at
    FROM scrape_company_results r
    JOIN source_companies sc ON sc.id = r.company_id
    WHERE r.finished_at > NOW() - make_interval(days => %(days)s)
      AND sc.enabled = TRUE
    ORDER BY r.company_id, r.source_name, r.finished_at DESC
"""

_COMPANIES_SQL = """
    SELECT id::text, company_name, source_endpoints, domain, enabled
    FROM source_companies
    WHERE enabled = TRUE
"""


def load_companies(conn) -> dict[str, Company]:
    out: dict[str, Company] = {}
    with conn.cursor() as cur:
        cur.execute(_COMPANIES_SQL)
        for cid, name, endpoints, domain, enabled in cur.fetchall():
            eps = endpoints if isinstance(endpoints, dict) else json.loads(endpoints or "{}")
            out[cid] = Company(cid, name, {k: str(v) for k, v in eps.items() if v}, domain or None, bool(enabled))
    return out


def load_health(conn) -> dict[tuple[str, str], SourceHealth]:
    health: dict[tuple[str, str], SourceHealth] = {}
    with conn.cursor() as cur:
        cur.execute(_HEALTH_SQL, {"days": LOOKBACK_DAYS})
        for cid, src, status, http_status, msg, jobs, finished in cur.fetchall():
            h = health.setdefault((cid, src), SourceHealth(cid, src))
            if h.runs == 0:
                h.last_status = status
            h.runs += 1
            if status == "success":
                if (jobs or 0) > 0:
                    h.successes_with_jobs += 1
                else:
                    h.successes_empty += 1
            elif status == "failed" and len(h.last_failures) < MIN_FAILED_RUNS \
                    and h.runs == len(h.last_failures) + 1:
                # only the unbroken run of failures at the top (most recent first)
                h.last_failures.append((finished, http_status, msg or ""))
    return health


# --------------------------------------------------------------------------
# Safety rules
# --------------------------------------------------------------------------
def broken_reason(source: str, h: SourceHealth) -> Optional[tuple[str, str]]:
    """(category, label) if this source has really stopped working, else None."""
    if h.successes_with_jobs or h.successes_empty or len(h.last_failures) < MIN_FAILED_RUNS:
        return None
    newest, oldest = h.last_failures[0][0], h.last_failures[-1][0]
    if newest - oldest < timedelta(hours=MIN_FAILURE_SPAN_HOURS):
        return None
    cats = [classify_failure(source, msg, status) for _, status, msg in h.last_failures]
    if any(c not in BROKEN_CATEGORIES for c, _ in cats):
        return None
    return cats[0]


def is_empty_board(h: SourceHealth) -> bool:
    return h.successes_with_jobs == 0 and h.successes_empty >= MIN_FAILED_RUNS and h.last_status == "success"


def healthy_other_sources(company: Company, source: str, health) -> dict[str, str]:
    """Other sources of this company that scraped successfully with jobs recently."""
    out = {}
    for s, e in company.sources.items():
        if s == source:
            continue
        h = health.get((company.id, s))
        if h and h.successes_with_jobs > 0 and h.last_status == "success":
            out[s] = e
    return out


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def _action(company: Company, source: str, action: str, confidence: str, category: str,
            reason: str, evidence: str = "", new_source: str = "", new_endpoint: str = "",
            new_jobs: Optional[int] = None) -> dict:
    return {
        "company_id": company.id, "company_name": company.name, "source": source,
        "endpoint": company.sources.get(source, ""), "action": action, "confidence": confidence,
        "category": category, "reason": reason, "evidence": evidence,
        "new_source": new_source or None, "new_endpoint": new_endpoint or None,
        "new_board_jobs": new_jobs,
    }


def _search(company: Company, source: str) -> tuple[Optional[bf.Hit], dict]:
    endpoint = company.sources[source]
    hit = bf.probe_workday(endpoint) if source == "workday" else None
    if hit and hit.confidence == "high":
        return hit, {}
    old = endpoint if source != "workday" else (bf._workday_parts(endpoint) or ("", "", ""))[0]
    res = bf.resolve_one({"company_name": company.name, "source": source,
                          "old_endpoint": old, "domain": company.domain or ""})
    if res.get("status") == "relocated":
        other = bf.Hit(res["new_source"], res["new_endpoint"],
                       int(res["jobs"]) if res.get("jobs") else None,
                       evidence=res.get("evidence", ""), confidence=res.get("confidence", "low"))
        hit = bf._better(hit, other)
    return hit, res


def plan_broken(company: Company, source: str, category: str, label: str,
                health, ep_owner: dict[str, str]) -> dict:
    endpoint = company.sources[source]
    if bf.is_junk(company.name, endpoint):
        return _action(company, source, "disable_company", "high", category, label,
                       "test/sandbox/internal/referral board")
    healthy = healthy_other_sources(company, source, health)
    if healthy:
        return _action(company, source, "remove_source", "high", category, label,
                       f"company scrapes fine with jobs on {', '.join(sorted(healthy))}")

    hit, res = _search(company, source)
    if hit and bf.same_board(source, endpoint, hit):
        return _action(company, source, "wait", "high", category, label,
                       f"board answers normally now (jobs={hit.jobs}); failure looks temporary")
    if hit and (hit.jobs is None or hit.jobs > 0):
        owner = ep_owner.get(bf.endpoint_key(hit.source, hit.endpoint))
        if owner and owner != company.id:
            return _action(company, source, "remove_source" if len(company.sources) > 1 else "disable_company",
                           "medium", category, label,
                           f"its new board {hit.source}:{hit.endpoint} is already scraped by another company row")
        return _action(company, source, "relocate", hit.confidence, category, label,
                       hit.evidence, hit.source, hit.endpoint, hit.jobs)
    if res.get("status") == "unsupported_ats":
        return _action(company, source, "remove_source" if len(company.sources) > 1 else "disable_company",
                       res.get("confidence") or "medium", category, label,
                       f"moved to {res.get('new_source')} (not scraped by Harco); {res.get('evidence', '')}")
    if category == "blocked":
        return _action(company, source, "review", "medium", category, label,
                       "site refuses requests; may need headers or be disabled")
    return _action(company, source, "remove_source" if len(company.sources) > 1 else "disable_company",
                   "medium", category, label, "no live board found on any supported ATS or the careers page")


def plan_empty(company: Company, source: str, health, ep_owner) -> Optional[dict]:
    label = f"0 jobs for {LOOKBACK_DAYS} days"
    healthy = healthy_other_sources(company, source, health)
    if bf.is_junk(company.name, company.sources[source]):
        return _action(company, source, "disable_company", "high", "empty_board", label,
                       "test/sandbox/internal/referral board")
    if healthy:
        return _action(company, source, "remove_source", "high", "empty_board", label,
                       f"empty here; company has jobs on {', '.join(sorted(healthy))}")
    hit, _ = _search(company, source)
    if hit and hit.confidence == "high" and (hit.jobs or 0) > 0 \
            and bf.endpoint_key(hit.source, hit.endpoint) != bf.endpoint_key(source, company.sources[source]):
        owner = ep_owner.get(bf.endpoint_key(hit.source, hit.endpoint))
        if owner and owner != company.id:
            return None  # its jobs are already scraped via another row; duplicates handle it
        return _action(company, source, "relocate", "high", "empty_board", label,
                       hit.evidence, hit.source, hit.endpoint, hit.jobs)
    return None  # an empty board with no better alternative: nothing to propose


def _sluggy(name: str) -> bool:
    n = name.strip()
    return (bool(re.search(r"\d$", n)) or "-" in n or n == n.lower()
            or bool(re.search(r"careers?$|jobs$", n, re.I))
            or n.lower() in {"experienced", "external", "careers", "jobs", "corporate", "search"})


def _keeper_score(c: Company) -> tuple:
    return (not _sluggy(c.name), bool(c.domain), len(c.sources), " " in c.name, len(c.name))


def plan_duplicates(companies: dict[str, Company]) -> tuple[list[dict], dict[str, str]]:
    """Company rows that scrape the same board. Returns (actions, board key -> owner id)."""
    owners: dict[str, list[tuple[Company, str]]] = defaultdict(list)
    for c in companies.values():
        for s, e in c.sources.items():
            owners[bf.endpoint_key(s, e)].append((c, s))
    actions, ep_owner = [], {}
    for key, entries in owners.items():
        ranked = sorted({c.id: c for c, _ in entries}.values(), key=_keeper_score, reverse=True)
        keeper = ranked[0]
        ep_owner[key] = keeper.id
        for c, s in entries:
            if c.id == keeper.id:
                continue
            if len(c.sources) > 1:
                actions.append(_action(c, s, "remove_source", "high", "duplicate_row",
                                       "same board scraped by two company rows",
                                       f"'{keeper.name}' already scrapes {s}:{c.sources[s]}"))
            else:
                actions.append(_action(c, s, "disable_company", "medium", "duplicate_row",
                                       "same board scraped by two company rows",
                                       f"duplicate of '{keeper.name}'; review before disabling"))
    return actions, ep_owner


def build_plan(companies: dict[str, Company], health: dict, include_empty: bool) -> list[dict]:
    actions, ep_owner = plan_duplicates(companies)
    done = {(a["company_id"], a["source"]) for a in actions}

    work = []
    for (cid, src), h in health.items():
        c = companies.get(cid)
        if not c or src not in c.sources or (cid, src) in done:
            continue
        broken = broken_reason(src, h)
        if broken:
            work.append(("broken", c, src, broken))
        elif include_empty and is_empty_board(h):
            work.append(("empty", c, src, None))

    def run(item):
        kind, c, src, broken = item
        try:
            if kind == "broken":
                return plan_broken(c, src, broken[0], broken[1], health, ep_owner)
            return plan_empty(c, src, health, ep_owner)
        except Exception as exc:  # noqa: BLE001 - one bad company must not sink the run
            logger.exception("reconcile: planning failed for %s/%s", c.name, src)
            return _action(c, src, "review", "low", "error", "planning error", f"{type(exc).__name__}: {exc}"[:300])

    with ThreadPoolExecutor(max_workers=max(1, SEARCH_WORKERS)) as pool:
        for a in pool.map(run, work):
            if a:
                actions.append(a)
    return actions


# --------------------------------------------------------------------------
# Persistence + queue entry point
# --------------------------------------------------------------------------
_INSERT_ACTION_SQL = """
    INSERT INTO source_reconcile_actions (
        run_id, company_id, action, confidence, source, endpoint,
        new_source, new_endpoint, new_board_jobs, category, reason, evidence
    ) VALUES (
        %(run_id)s, %(company_id)s, %(action)s, %(confidence)s, %(source)s, %(endpoint)s,
        %(new_source)s, %(new_endpoint)s, %(new_board_jobs)s, %(category)s, %(reason)s, %(evidence)s
    )
"""


def _set_run(conn, run_id: str, status: str, summary: Optional[dict] = None, error: Optional[str] = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE source_reconcile_runs
            SET status = %(status)s,
                started_at = CASE WHEN %(status)s = 'running' THEN CURRENT_TIMESTAMP ELSE started_at END,
                finished_at = CASE WHEN %(status)s IN ('done', 'failed') THEN CURRENT_TIMESTAMP ELSE finished_at END,
                summary = COALESCE(%(summary)s::jsonb, summary),
                error = %(error)s
            WHERE id = %(run_id)s
            """,
            {"status": status, "summary": json.dumps(summary) if summary is not None else None,
             "error": error, "run_id": run_id},
        )
    conn.commit()


def run_reconcile(run_id: str) -> dict:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, include_empty_boards FROM source_reconcile_runs WHERE id = %(id)s",
                {"id": run_id},
            )
            row = cur.fetchone()
        if not row:
            raise LookupError(f"source_reconcile_runs {run_id} not found")
        status, include_empty = row
        if status != "queued":
            logger.info("reconcile run %s is %s; skipping redelivery", run_id, status)
            return {}
        _set_run(conn, run_id, "running")
        companies = load_companies(conn)
        health = load_health(conn)
    finally:
        conn.close()

    # Network search happens with no DB connection held (it can take ~30 min).
    actions = build_plan(companies, health, include_empty)

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            for a in actions:
                cur.execute(_INSERT_ACTION_SQL, {**a, "run_id": run_id})
        summary: dict[str, int] = defaultdict(int)
        for a in actions:
            summary[f"{a['action']}:{a['confidence']}"] += 1
        summary = dict(summary)
        conn.commit()
        _set_run(conn, run_id, "done", summary=summary)
        logger.info("reconcile run %s done: %s", run_id, summary)
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_reconcile_body(body: bytes | str) -> MessageDisposition:
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        run_id = str(data["run_id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        logger.warning("source_reconcile_tasks: bad message: %s", exc)
        return MessageDisposition.NACK_NO_REQUEUE
    try:
        run_reconcile(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("reconcile run %s failed", run_id)
        conn = db.get_db_connection()
        try:
            _set_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        except Exception:  # noqa: BLE001
            logger.exception("could not mark reconcile run %s failed", run_id)
        finally:
            conn.close()
    # ACK either way: the run row records the outcome; a retry is a new run.
    return MessageDisposition.ACK
