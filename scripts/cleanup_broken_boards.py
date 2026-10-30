#!/usr/bin/env python3
"""
Find broken company job boards, review the plan, then apply or undo it.

Three steps, run from the job_helper_scraper root with DB_* env set:

  1. plan   (read-only; network search, about 10-35 min depending on --workers)
       python scripts/cleanup_broken_boards.py plan --output plan.csv

     Open plan.csv. Put "yes" in the apply column of the rows you want, or use
     --high in step 2 to take every high-confidence fix.

  2. apply  (dry run unless --commit)
       python scripts/cleanup_broken_boards.py apply --plan plan.csv --high
       python scripts/cleanup_broken_boards.py apply --plan plan.csv --high --commit --log applied.csv

  3. undo   (only if needed; dry run unless --commit)
       python scripts/cleanup_broken_boards.py undo --log applied.csv --commit --output undone.csv

A source is only planned for removal/relocation when its last 3+ runs over
24h+ all failed with "board gone"/403 errors and it had no success in 7 days.
Apply re-checks every row first (see services/board_cleanup.py) and never
deletes anything: it drops one source, swaps one source for another, or sets
enabled = false. The apply log holds the previous values for undo.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from services import board_cleanup as bc  # noqa: E402
from services import source_reconcile as sr  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cleanup_broken_boards")

PLAN_COLS = ["apply", "planned_at", "company_id", "company_name", "action", "confidence",
             "source", "endpoint", "new_source", "new_endpoint", "new_board_jobs",
             "category", "reason", "evidence"]
LOG_COLS = PLAN_COLS + ["result", "note", "previous_endpoints", "previous_enabled"]
UNDO_COLS = LOG_COLS + ["undo_result", "undo_note"]


def _write(path: str, cols: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})


def _read(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def select_rows(rows: list[dict], high: bool) -> list[dict]:
    """Rows marked apply=yes, or every high-confidence fix with --high."""
    picked = []
    for r in rows:
        if r.get("action") not in bc.APPLYABLE_ACTIONS:
            continue
        marked = (r.get("apply") or "").strip().lower() in ("yes", "y", "1", "true")
        if marked or (high and r.get("confidence") == "high"):
            picked.append(r)
    return picked


def cmd_plan(args) -> int:
    sr.SEARCH_WORKERS = args.workers
    planned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db.get_db_connection()
    try:
        companies = sr.load_companies(conn)
        health = sr.load_health(conn)
    finally:
        conn.close()   # nothing is held open during the network search
    logger.info("loaded %d companies, %d (company, source) histories; searching...",
                len(companies), len(health))
    actions = sr.build_plan(companies, health, args.include_empty)
    rows = [{**a, "apply": "", "planned_at": planned_at} for a in actions]
    rows.sort(key=lambda r: (r["action"] not in bc.APPLYABLE_ACTIONS, r["confidence"] != "high",
                             r["action"], r["company_name"]))
    _write(args.output, PLAN_COLS, rows)
    counts = Counter(f"{r['action']}:{r['confidence']}" for r in rows)
    logger.info("wrote %d rows to %s", len(rows), args.output)
    for k, v in sorted(counts.items()):
        print(f"  {k:28} {v}")
    return 0


def cmd_apply(args) -> int:
    rows = select_rows(_read(args.plan), args.high)
    if not rows:
        print("Nothing selected. Mark rows apply=yes or pass --high.")
        return 1
    now = datetime.now(timezone.utc)
    conn = db.get_db_connection()
    out = []
    try:
        for r in rows:
            out.append(bc.apply_row(conn, r, _parse_time(r["planned_at"]), now, args.commit))
    finally:
        conn.close()
    log = args.log or ("applied.csv" if args.commit else "apply_dry_run.csv")
    _write(log, LOG_COLS, out)
    print(("APPLIED" if args.commit else "DRY RUN (nothing changed; add --commit)") + f" -> {log}")
    for k, v in sorted(Counter(r["result"] for r in out).items()):
        print(f"  {k:12} {v}")
    for k, v in Counter(r["note"] for r in out if r["result"] == "skipped").most_common(8):
        print(f"    skipped: {v:4}  {k}")
    return 0


def cmd_undo(args) -> int:
    rows = [r for r in _read(args.log) if r.get("result") == "applied"]
    if not rows:
        print("No applied rows in that log.")
        return 1
    conn = db.get_db_connection()
    out = []
    try:
        for r in rows:
            out.append(bc.undo_row(conn, r, args.commit))
    finally:
        conn.close()
    dest = args.output or ("undone.csv" if args.commit else "undo_dry_run.csv")
    _write(dest, UNDO_COLS, out)
    print(("UNDONE" if args.commit else "DRY RUN (nothing changed; add --commit)") + f" -> {dest}")
    for k, v in sorted(Counter(r["undo_result"] for r in out).items()):
        print(f"  {k:12} {v}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="build the plan (read-only)")
    pl.add_argument("--output", default="plan.csv")
    pl.add_argument("--include-empty", action="store_true",
                    help="also plan for boards that returned 0 jobs on every run for 7 days")
    pl.add_argument("--workers", type=int, default=16, help="parallel board searches (default 16)")
    pl.set_defaults(fn=cmd_plan)

    ap = sub.add_parser("apply", help="apply selected rows (dry run unless --commit)")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--high", action="store_true", help="select every high-confidence fix")
    ap.add_argument("--commit", action="store_true", help="actually write the changes")
    ap.add_argument("--log", help="where to write results / undo info (default applied.csv)")
    ap.set_defaults(fn=cmd_apply)

    un = sub.add_parser("undo", help="undo an apply log (dry run unless --commit)")
    un.add_argument("--log", required=True)
    un.add_argument("--commit", action="store_true")
    un.add_argument("--output")
    un.set_defaults(fn=cmd_undo)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
