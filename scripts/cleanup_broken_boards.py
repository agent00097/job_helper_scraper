#!/usr/bin/env python3
"""
Find broken company job boards and clean them up, safely and undoably.

Run from the job_helper_scraper root (DB settings from .env or DB_* env):

    python scripts/cleanup_broken_boards.py plan              # read-only, writes plan.csv
    python scripts/cleanup_broken_boards.py apply             # dry run: shows what would change
    python scripts/cleanup_broken_boards.py apply --commit    # makes the changes, writes an undo log

    python scripts/cleanup_broken_boards.py undo --log applied_<time>.csv --commit

No CSV editing is needed. By default apply takes the safe fixes:
  * remove a dead board from a company that still has a working one
  * move a board to where the company moved (high confidence only)
  * remove/disable duplicate company rows whose board another row still scrapes
Add --include-disable to also disable companies with no working board at all.

A board counts as broken only when it has failed on every run for 24h+, with
no success in 7 days, and its latest failures are "board gone"/403 errors
(timeouts and our own bugs never count). Before each change apply re-checks
the row (services/board_cleanup.py). Nothing is ever deleted.
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
             "category", "reason", "evidence", "other_company_id"]
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


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _marked(r: dict) -> bool:
    return (r.get("apply") or "").strip().lower() in ("yes", "y", "1", "true")


def is_safe_default(r: dict, include_disable: bool = False) -> bool:
    """The rows apply takes when no selection flags are given."""
    action, conf = r.get("action"), r.get("confidence")
    if r.get("category") == "duplicate_row":
        return action in ("remove_source", "disable_company")
    if action == "remove_source":
        return True          # only applied while another source still returns jobs
    if action == "relocate":
        return conf == "high"
    if action == "disable_company":
        return include_disable and conf in ("high", "medium")
    return False


def select_rows(rows: list[dict], high: bool = False, min_confidence: str = "",
                actions: tuple[str, ...] = (), include_disable: bool = False) -> list[dict]:
    """Which plan rows to apply.

    No flags: the safe default set (is_safe_default). --high / --min-confidence /
    --actions switch to explicit selection instead. Rows marked apply=yes are
    always included. wait/review/keep rows are never applied.
    """
    explicit = bool(high or min_confidence or actions)
    floor = CONFIDENCE_RANK.get("high" if high else (min_confidence or "low"), 1)
    allowed = actions or bc.APPLYABLE_ACTIONS
    picked = []
    for r in rows:
        if r.get("action") not in bc.APPLYABLE_ACTIONS:
            continue
        if explicit:
            chosen = r.get("action") in allowed and CONFIDENCE_RANK.get(r.get("confidence", ""), 0) >= floor
        else:
            chosen = is_safe_default(r, include_disable)
        if chosen or _marked(r):
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

    print("\nFailing boards with no success in 7 days, and why:")
    for k, v in sr.diagnose(health).most_common():
        print(f"  {v:6}  {k}")
    print(f"\nPlan: {len(rows)} rows -> {args.output}")
    for k, v in sorted(Counter(f"{r['action']} ({r['confidence']}, {r['category']})" for r in rows).items()):
        print(f"  {v:6}  {k}")
    safe = len(select_rows(rows))
    extra = len(select_rows(rows, include_disable=True)) - safe
    print(f"\napply will take {safe} safe fixes by default"
          + (f" (+{extra} company disables with --include-disable)" if extra else "") + ".")
    print(f"Next: python scripts/cleanup_broken_boards.py apply --plan {args.output}   (dry run)")
    return 0


def cmd_apply(args) -> int:
    actions = tuple(a.strip() for a in args.actions.split(",") if a.strip())
    bad = [a for a in actions if a not in bc.APPLYABLE_ACTIONS]
    if bad:
        print(f"Unknown --actions {bad}; choose from {', '.join(bc.APPLYABLE_ACTIONS)}")
        return 2
    rows = select_rows(_read(args.plan), args.high, args.min_confidence, actions, args.include_disable)
    if not rows:
        print("Nothing to apply: the plan has no fixes in the selected set.")
        return 0
    log = args.log or (f"applied_{datetime.now():%Y%m%d_%H%M%S}.csv" if args.commit else "apply_dry_run.csv")
    if args.commit and os.path.exists(log):
        print(f"{log} already exists; it may hold undo info. Pass a new --log name.")
        return 2
    now = datetime.now(timezone.utc)
    conn = db.get_db_connection()
    out = []
    try:
        for r in rows:
            out.append(bc.apply_row(conn, r, _parse_time(r["planned_at"]), now, args.commit))
    finally:
        conn.close()
    _write(log, LOG_COLS, out)
    print(("APPLIED. Keep this file, it is the undo log" if args.commit
           else "DRY RUN, nothing changed. Run again with --commit to apply") + f" -> {log}")
    for k, v in sorted(Counter(r["result"] for r in out).items()):
        print(f"  {k:12} {v}")
    for k, v in Counter(r["note"] for r in out if r["result"] == "skipped").most_common(8):
        print(f"    skipped: {v:4}  {k}")
    done = Counter(f"{r['action']} ({r['category']})" for r in out if r["result"] in ("applied", "would_apply"))
    for k, v in done.most_common():
        print(f"    {'applied' if args.commit else 'would apply'}: {v:4}  {k}")
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
    ap.add_argument("--plan", default="plan.csv")
    ap.add_argument("--commit", action="store_true", help="actually write the changes (default: dry run)")
    ap.add_argument("--include-disable", action="store_true",
                    help="also disable companies with no working board found anywhere")
    ap.add_argument("--log", help="undo log to write (default applied_<time>.csv)")
    ap.add_argument("--high", action="store_true", help="explicit: every high-confidence fix")
    ap.add_argument("--min-confidence", choices=["high", "medium"], default="",
                    help="explicit: every fix at or above this confidence")
    ap.add_argument("--actions", default="",
                    help="explicit: only these actions, comma-separated: remove_source,relocate,disable_company")
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
