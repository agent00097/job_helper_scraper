"""
Apply (and undo) a reviewed broken-board plan, row by row, safely.

Used by scripts/cleanup_broken_boards.py. Each row is re-checked inside its own
transaction with the company row locked, and is skipped unless ALL hold:

  * the company's endpoint for that source is still the one the plan saw;
  * that board has not scraped successfully since the plan was made;
  * remove_source: another source of the company is still returning jobs, and
    the company keeps at least one source;
  * relocate: the company has no other endpoint on the new source, and no
    other company already scrapes the new board;
  * disable_company: none of the company's sources returned jobs since the
    plan.

Nothing is ever deleted. The only writes to source_companies are: drop one key
from source_endpoints, swap one key for another, or set enabled = false. Each
UPDATE repeats its precondition in WHERE, so a concurrent edit makes it match
0 rows instead of overwriting anything. The previous endpoints/enabled flag of
every applied row is returned so it can be written to an undo file.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

APPLYABLE_ACTIONS = ("remove_source", "relocate", "disable_company")
PLAN_MAX_AGE_HOURS = 72
HEALTHY_LOOKBACK_DAYS = 7


# --------------------------------------------------------------------------
# Pure decision logic
# --------------------------------------------------------------------------
@dataclass
class SourceResults:
    """Recent scrape results for one (company, source)."""
    last_status: Optional[str] = None
    success_since_plan: bool = False
    jobs_since_plan: bool = False
    healthy: bool = False   # latest run succeeded and some run in the window had jobs


def _utc(t: Optional[datetime]) -> Optional[datetime]:
    """scrape_company_results.finished_at is a naive UTC timestamp; make it comparable."""
    if t is None or t.tzinfo is not None:
        return t
    return t.replace(tzinfo=timezone.utc)


def summarize_results(rows: Iterable[tuple], planned_at: datetime) -> dict[str, SourceResults]:
    """rows: (source_name, status, jobs_fetched, finished_at), newest first."""
    planned_at = _utc(planned_at)
    out: dict[str, SourceResults] = {}
    had_jobs: dict[str, bool] = {}
    for source, status, jobs, finished in rows:
        r = out.get(source)
        if r is None:
            r = out[source] = SourceResults(last_status=status)
        ok = status == "success"
        with_jobs = ok and (jobs or 0) > 0
        finished = _utc(finished)
        if finished is not None and finished > planned_at:
            r.success_since_plan |= ok
            r.jobs_since_plan |= with_jobs
        had_jobs[source] = had_jobs.get(source, False) or with_jobs
    for source, r in out.items():
        r.healthy = r.last_status == "success" and had_jobs.get(source, False)
    return out


@dataclass
class Change:
    kind: str   # remove | relocate | disable | restore_source | undo_relocate | enable
    source: Optional[str] = None
    endpoint: Optional[str] = None
    new_source: Optional[str] = None
    new_endpoint: Optional[str] = None


@dataclass
class Decision:
    change: Optional[Change]
    note: str


@dataclass
class ApplyContext:
    action: str
    category: str
    source: str
    endpoint: str
    new_source: Optional[str]
    new_endpoint: Optional[str]
    planned_at: datetime
    company_endpoints: dict
    company_enabled: bool
    results: dict[str, SourceResults] = field(default_factory=dict)
    new_endpoint_owner: Optional[str] = None


def _recovered(ctx: ApplyContext) -> bool:
    r = ctx.results.get(ctx.source)
    if r is None:
        return False
    return r.jobs_since_plan if ctx.category == "empty_board" else r.success_since_plan


def decide_apply(ctx: ApplyContext, now: datetime) -> Decision:
    if ctx.action not in APPLYABLE_ACTIONS:
        return Decision(None, f"'{ctx.action}' is informational; nothing to apply")
    if now - ctx.planned_at > timedelta(hours=PLAN_MAX_AGE_HOURS):
        return Decision(None, f"plan is older than {PLAN_MAX_AGE_HOURS}h; make a new one")
    current = ctx.company_endpoints.get(ctx.source)
    if current != ctx.endpoint:
        return Decision(None, f"endpoint changed since plan (now {current!r})")
    if _recovered(ctx):
        return Decision(None, "board scraped successfully since the plan; left as is")

    if ctx.action == "remove_source":
        others = [s for s in ctx.company_endpoints if s != ctx.source]
        if not others:
            return Decision(None, "it is the company's only source")
        if not any(ctx.results.get(s) and ctx.results[s].healthy for s in others):
            return Decision(None, "no other source of this company is currently returning jobs")
        return Decision(Change("remove", ctx.source, ctx.endpoint), "removed")

    if ctx.action == "relocate":
        if not ctx.new_source or not ctx.new_endpoint:
            return Decision(None, "plan has no new board")
        existing = ctx.company_endpoints.get(ctx.new_source)
        if ctx.new_source != ctx.source and existing is not None:
            return Decision(None, f"company already has a {ctx.new_source} endpoint ({existing!r})")
        if ctx.new_endpoint_owner:
            return Decision(None, f"{ctx.new_source}:{ctx.new_endpoint} already belongs to company {ctx.new_endpoint_owner}")
        return Decision(Change("relocate", ctx.source, ctx.endpoint, ctx.new_source, ctx.new_endpoint),
                        f"moved to {ctx.new_source}:{ctx.new_endpoint}")

    # disable_company
    if not ctx.company_enabled:
        return Decision(None, "company is already disabled")
    working = [s for s in ctx.company_endpoints if ctx.results.get(s) and ctx.results[s].jobs_since_plan]
    if working:
        return Decision(None, f"{', '.join(sorted(working))} returned jobs since the plan")
    return Decision(Change("disable"), "disabled")


@dataclass
class UndoContext:
    action: str
    source: str
    endpoint: str
    new_source: Optional[str]
    new_endpoint: Optional[str]
    previous_enabled: Optional[bool]
    company_endpoints: dict
    company_enabled: bool


def decide_undo(ctx: UndoContext) -> Decision:
    """Reverse exactly what apply did, only if nobody changed it since."""
    eps = ctx.company_endpoints
    if ctx.action == "remove_source":
        if ctx.source in eps:
            return Decision(None, f"company has a {ctx.source} endpoint again ({eps[ctx.source]!r}); not overwriting")
        return Decision(Change("restore_source", ctx.source, ctx.endpoint), f"restored {ctx.source}:{ctx.endpoint}")
    if ctx.action == "relocate":
        if eps.get(ctx.new_source) != ctx.new_endpoint:
            return Decision(None, f"{ctx.new_source} endpoint was changed after apply; undo by hand")
        if ctx.new_source != ctx.source and ctx.source in eps:
            return Decision(None, f"company has a {ctx.source} endpoint again; not overwriting")
        return Decision(Change("undo_relocate", ctx.source, ctx.endpoint, ctx.new_source, ctx.new_endpoint),
                        f"moved back to {ctx.source}:{ctx.endpoint}")
    if ctx.action == "disable_company":
        if ctx.company_enabled:
            return Decision(None, "company is already enabled")
        if ctx.previous_enabled is False:
            return Decision(None, "company was already disabled before apply")
        return Decision(Change("enable"), "re-enabled")
    return Decision(None, f"cannot undo '{ctx.action}'")


# --------------------------------------------------------------------------
# SQL — every UPDATE repeats its precondition in WHERE
# --------------------------------------------------------------------------
CHANGE_SQL = {
    "remove": """
        UPDATE source_companies
        SET source_endpoints = source_endpoints - %(source)s::text, updated_at = NOW()
        WHERE id = %(company_id)s AND source_endpoints ->> %(source)s::text = %(endpoint)s::text
          AND (SELECT COUNT(*) FROM jsonb_object_keys(source_endpoints)) > 1
    """,
    "relocate": """
        UPDATE source_companies
        SET source_endpoints = (source_endpoints - %(source)s::text)
                               || jsonb_build_object(%(new_source)s::text, %(new_endpoint)s::text),
            updated_at = NOW()
        WHERE id = %(company_id)s AND source_endpoints ->> %(source)s::text = %(endpoint)s::text
    """,
    "disable": """
        UPDATE source_companies SET enabled = FALSE, updated_at = NOW()
        WHERE id = %(company_id)s AND enabled = TRUE
    """,
    "restore_source": """
        UPDATE source_companies
        SET source_endpoints = source_endpoints || jsonb_build_object(%(source)s::text, %(endpoint)s::text),
            updated_at = NOW()
        WHERE id = %(company_id)s AND NOT (source_endpoints ? %(source)s::text)
    """,
    "undo_relocate": """
        UPDATE source_companies
        SET source_endpoints = (source_endpoints - %(new_source)s::text)
                               || jsonb_build_object(%(source)s::text, %(endpoint)s::text),
            updated_at = NOW()
        WHERE id = %(company_id)s AND source_endpoints ->> %(new_source)s::text = %(new_endpoint)s::text
    """,
    "enable": """
        UPDATE source_companies SET enabled = TRUE, updated_at = NOW()
        WHERE id = %(company_id)s AND enabled = FALSE
    """,
}


def _params(company_id: str, ch: Change) -> dict:
    return {"company_id": company_id, "source": ch.source, "endpoint": ch.endpoint,
            "new_source": ch.new_source, "new_endpoint": ch.new_endpoint}


def _endpoints(v) -> dict:
    if v is None:
        return {}
    return v if isinstance(v, dict) else json.loads(v)


def _lock_company(cur, company_id: str):
    cur.execute("SELECT source_endpoints, enabled FROM source_companies WHERE id = %(id)s FOR UPDATE",
                {"id": company_id})
    return cur.fetchone()


def _load_results(cur, company_id: str, planned_at: datetime) -> dict[str, SourceResults]:
    cur.execute(
        """
        SELECT source_name, status, jobs_fetched, finished_at
        FROM scrape_company_results
        WHERE company_id = %(cid)s
          AND finished_at > NOW() - make_interval(days => %(days)s)
        ORDER BY finished_at DESC
        """,
        {"cid": company_id, "days": HEALTHY_LOOKBACK_DAYS},
    )
    return summarize_results(cur.fetchall(), planned_at)


def apply_row(conn, row: dict, planned_at: datetime, now: datetime, commit: bool) -> dict:
    """Apply one plan row in its own transaction. With commit=False it rolls back (dry run).

    Returns the row plus result (applied / would_apply / skipped / failed), note,
    previous_endpoints and previous_enabled.
    """
    out = {**row, "result": "", "note": "", "previous_endpoints": "", "previous_enabled": ""}
    cid = row["company_id"]
    try:
        with conn.cursor() as cur:
            crow = _lock_company(cur, cid)
            if not crow:
                conn.rollback()
                return {**out, "result": "skipped", "note": "company no longer exists"}
            endpoints, enabled = _endpoints(crow[0]), crow[1]

            owner = None
            if row["action"] == "relocate" and row.get("new_source") and row.get("new_endpoint"):
                cur.execute(
                    """
                    SELECT id::text FROM source_companies
                    WHERE id <> %(id)s AND source_endpoints ->> %(src)s::text = %(ep)s::text
                    LIMIT 1
                    """,
                    {"id": cid, "src": row["new_source"], "ep": row["new_endpoint"]},
                )
                o = cur.fetchone()
                owner = o[0] if o else None

            decision = decide_apply(ApplyContext(
                action=row["action"], category=row.get("category", ""), source=row["source"],
                endpoint=row["endpoint"], new_source=row.get("new_source") or None,
                new_endpoint=row.get("new_endpoint") or None, planned_at=planned_at,
                company_endpoints=endpoints, company_enabled=enabled,
                results=_load_results(cur, cid, planned_at), new_endpoint_owner=owner,
            ), now)
            if decision.change is None:
                conn.rollback()
                return {**out, "result": "skipped", "note": decision.note}

            cur.execute(CHANGE_SQL[decision.change.kind], _params(cid, decision.change))
            if cur.rowcount != 1:
                conn.rollback()
                return {**out, "result": "skipped", "note": "company changed while applying"}
        prev = {"previous_endpoints": json.dumps(endpoints, sort_keys=True), "previous_enabled": enabled}
        if not commit:
            conn.rollback()
            return {**out, **prev, "result": "would_apply", "note": decision.note}
        conn.commit()
        return {**out, **prev, "result": "applied", "note": decision.note}
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
        conn.rollback()
        logger.exception("cleanup: failed to apply %s/%s", row.get("company_name"), row.get("source"))
        return {**out, "result": "failed", "note": f"{type(exc).__name__}: {exc}"[:300]}


def undo_row(conn, row: dict, commit: bool) -> dict:
    """Undo one row from an apply log (result == applied)."""
    out = {**row, "undo_result": "", "undo_note": ""}
    cid = row["company_id"]
    try:
        with conn.cursor() as cur:
            crow = _lock_company(cur, cid)
            if not crow:
                conn.rollback()
                return {**out, "undo_result": "skipped", "undo_note": "company no longer exists"}
            prev_enabled = {"true": True, "false": False}.get(str(row.get("previous_enabled", "")).lower())
            decision = decide_undo(UndoContext(
                action=row["action"], source=row["source"], endpoint=row["endpoint"],
                new_source=row.get("new_source") or None, new_endpoint=row.get("new_endpoint") or None,
                previous_enabled=prev_enabled, company_endpoints=_endpoints(crow[0]),
                company_enabled=crow[1],
            ))
            if decision.change is None:
                conn.rollback()
                return {**out, "undo_result": "skipped", "undo_note": decision.note}
            cur.execute(CHANGE_SQL[decision.change.kind], _params(cid, decision.change))
            if cur.rowcount != 1:
                conn.rollback()
                return {**out, "undo_result": "skipped", "undo_note": "company changed while undoing"}
        if not commit:
            conn.rollback()
            return {**out, "undo_result": "would_undo", "undo_note": decision.note}
        conn.commit()
        return {**out, "undo_result": "undone", "undo_note": decision.note}
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        logger.exception("cleanup: failed to undo %s/%s", row.get("company_name"), row.get("source"))
        return {**out, "undo_result": "failed", "undo_note": f"{type(exc).__name__}: {exc}"[:300]}
