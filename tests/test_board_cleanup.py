"""Apply/undo rules and SQL guards for scripts/cleanup_broken_boards.py (no database)."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import board_cleanup as bc
from services.board_cleanup import (
    ApplyContext, SourceResults, UndoContext, decide_apply, decide_undo, summarize_results,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
PLAN = NOW - timedelta(hours=2)


def healthy():
    return SourceResults(last_status="success", healthy=True)


def ctx(**kw) -> ApplyContext:
    base = dict(
        action="remove_source", category="board_gone", source="workday",
        endpoint="acme.wd1/External", new_source=None, new_endpoint=None, planned_at=PLAN,
        company_endpoints={"workday": "acme.wd1/External", "ashby": "acme"}, company_enabled=True,
        results={"workday": SourceResults(last_status="failed"), "ashby": healthy()},
    )
    base.update(kw)
    return ApplyContext(**base)


def test_summarize_results_tracks_since_plan_and_health():
    rows = [
        ("ashby", "success", 12, NOW - timedelta(hours=1)),
        ("workday", "failed", 0, NOW - timedelta(hours=1)),
        ("workday", "success", 0, NOW - timedelta(minutes=90)),
        ("workday", "success", 40, NOW - timedelta(days=3)),
    ]
    r = summarize_results(rows, PLAN)
    assert r["ashby"].healthy and r["ashby"].jobs_since_plan
    assert r["workday"].last_status == "failed" and not r["workday"].healthy
    assert r["workday"].success_since_plan and not r["workday"].jobs_since_plan


# ---------------------------------------------------------------- remove
def test_remove_when_other_source_healthy():
    d = decide_apply(ctx(), NOW)
    assert d.change.kind == "remove" and d.change.source == "workday"


def test_never_removes_the_only_source():
    d = decide_apply(ctx(company_endpoints={"workday": "acme.wd1/External"}), NOW)
    assert d.change is None and "only source" in d.note


def test_never_removes_when_other_source_not_working():
    d = decide_apply(ctx(results={"ashby": SourceResults(last_status="failed")}), NOW)
    assert d.change is None and "no other source" in d.note


def test_board_that_worked_since_plan_is_left_alone():
    res = {"workday": SourceResults(last_status="success", success_since_plan=True), "ashby": healthy()}
    d = decide_apply(ctx(results=res), NOW)
    assert d.change is None and "since the plan" in d.note


def test_empty_board_counts_as_recovered_only_with_jobs():
    empty_again = {"workday": SourceResults(last_status="success", success_since_plan=True), "ashby": healthy()}
    assert decide_apply(ctx(category="empty_board", results=empty_again), NOW).change is not None
    jobs = {"workday": SourceResults(last_status="success", success_since_plan=True, jobs_since_plan=True),
            "ashby": healthy()}
    assert decide_apply(ctx(category="empty_board", results=jobs), NOW).change is None


def test_edited_endpoint_is_skipped():
    d = decide_apply(ctx(company_endpoints={"workday": "acme.wd5/Careers", "ashby": "acme"}), NOW)
    assert d.change is None and "endpoint changed" in d.note


def test_stale_plan_is_skipped():
    d = decide_apply(ctx(planned_at=NOW - timedelta(hours=bc.PLAN_MAX_AGE_HOURS + 1)), NOW)
    assert d.change is None and "older than" in d.note


@pytest.mark.parametrize("action", ["wait", "review", "keep"])
def test_informational_rows_change_nothing(action):
    assert decide_apply(ctx(action=action), NOW).change is None


# ---------------------------------------------------------------- relocate
def rctx(**kw):
    base = dict(action="relocate", new_source="greenhouse", new_endpoint="acme",
                company_endpoints={"workday": "acme.wd1/External"},
                results={"workday": SourceResults(last_status="failed")})
    base.update(kw)
    return ctx(**base)


def test_relocate_swaps_source():
    d = decide_apply(rctx(), NOW)
    assert d.change.kind == "relocate" and (d.change.new_source, d.change.new_endpoint) == ("greenhouse", "acme")


def test_relocate_refused_if_company_already_on_new_source():
    d = decide_apply(rctx(company_endpoints={"workday": "acme.wd1/External", "greenhouse": "acmeinc"}), NOW)
    assert d.change is None and "already has" in d.note


def test_relocate_refused_if_board_owned_by_other_company():
    d = decide_apply(rctx(new_endpoint_owner="9f1c"), NOW)
    assert d.change is None and "already belongs" in d.note


# ---------------------------------------------------------------- disable
def test_disable_only_when_nothing_returned_jobs_since_plan():
    base = dict(action="disable_company", company_endpoints={"workday": "x"}, endpoint="x",
                results={"workday": SourceResults(last_status="failed")})
    assert decide_apply(ctx(**base), NOW).change.kind == "disable"
    res = {"workday": SourceResults(last_status="success", jobs_since_plan=True, success_since_plan=True)}
    assert decide_apply(ctx(**{**base, "results": res, "category": "empty_board"}), NOW).change is None


def test_disable_skipped_if_already_disabled():
    assert decide_apply(ctx(action="disable_company", company_enabled=False), NOW).change is None


# ---------------------------------------------------------------- undo
def uctx(**kw):
    base = dict(action="remove_source", source="workday", endpoint="acme-wd", new_source=None,
                new_endpoint=None, previous_enabled=True, company_endpoints={"ashby": "acme"},
                company_enabled=True)
    base.update(kw)
    return UndoContext(**base)


def test_undo_remove_restores_key_but_never_overwrites():
    assert decide_undo(uctx()).change.kind == "restore_source"
    assert decide_undo(uctx(company_endpoints={"workday": "other"})).change is None


def test_undo_relocate_requires_untouched_new_endpoint():
    ok = uctx(action="relocate", new_source="greenhouse", new_endpoint="acme",
              company_endpoints={"greenhouse": "acme"})
    assert decide_undo(ok).change.kind == "undo_relocate"
    edited = uctx(action="relocate", new_source="greenhouse", new_endpoint="acme",
                  company_endpoints={"greenhouse": "acme2"})
    assert decide_undo(edited).change is None


def test_undo_disable():
    assert decide_undo(uctx(action="disable_company", company_enabled=False)).change.kind == "enable"
    assert decide_undo(uctx(action="disable_company", company_enabled=False,
                            previous_enabled=False)).change is None


# ---------------------------------------------------------------- SQL guards
def test_never_deletes():
    root = Path(__file__).parents[1]
    for name in ("services/board_cleanup.py", "scripts/cleanup_broken_boards.py", "services/source_reconcile.py"):
        assert not re.search(r"\bDELETE\s+FROM\b", (root / name).read_text(), re.I), name


def test_every_change_repeats_its_precondition():
    s = bc.CHANGE_SQL
    assert "source_endpoints ->> %(source)s::text = %(endpoint)s::text" in s["remove"]
    assert "jsonb_object_keys" in s["remove"]
    assert "source_endpoints ->> %(source)s::text = %(endpoint)s::text" in s["relocate"]
    assert "enabled = TRUE" in s["disable"].split("WHERE")[1]
    assert "NOT (source_endpoints ? %(source)s::text)" in s["restore_source"]
    assert "%(new_source)s::text = %(new_endpoint)s::text" in s["undo_relocate"]
    for sql in s.values():
        assert "WHERE id = %(company_id)s" in sql


def test_bound_values_are_cast_to_text():
    for sql in bc.CHANGE_SQL.values():
        for m in re.finditer(r"%\((\w+)\)s(::text)?", sql):
            if m.group(1) != "company_id":
                assert m.group(2), f"{m.group(0)} not cast"


# ---------------------------------------------------------------- CLI selection
def test_select_rows_marked_or_high():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cleanup_cli", Path(__file__).parents[1] / "scripts" / "cleanup_broken_boards.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    rows = [
        {"action": "remove_source", "confidence": "high", "apply": ""},
        {"action": "disable_company", "confidence": "medium", "apply": "yes"},
        {"action": "disable_company", "confidence": "medium", "apply": ""},
        {"action": "wait", "confidence": "high", "apply": "yes"},
    ]
    assert len(cli.select_rows(rows, high=False)) == 1
    assert len(cli.select_rows(rows, high=True)) == 2


def test_plan_writes_csv_without_holding_db_during_search(tmp_path, monkeypatch):
    import csv
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cleanup_cli2", Path(__file__).parents[1] / "scripts" / "cleanup_broken_boards.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    events = []

    class Conn:
        def close(self):
            events.append("closed")

    monkeypatch.setattr(cli.db, "get_db_connection", lambda: Conn())
    monkeypatch.setattr(cli.sr, "load_companies", lambda conn: {"c1": object()})
    monkeypatch.setattr(cli.sr, "load_health", lambda conn: {})

    def build(companies, health, include_empty):
        events.append("search")
        return [{"company_id": "c1", "company_name": "Acme", "source": "workday", "endpoint": "x",
                 "action": "remove_source", "confidence": "high", "category": "board_gone",
                 "reason": "404", "evidence": "", "new_source": None, "new_endpoint": None,
                 "new_board_jobs": None}]
    monkeypatch.setattr(cli.sr, "build_plan", build)
    out = tmp_path / "plan.csv"
    assert cli.main(["plan", "--output", str(out), "--workers", "4"]) == 0
    assert events == ["closed", "search"] and cli.sr.SEARCH_WORKERS == 4
    rows = list(csv.DictReader(open(out)))
    assert rows[0]["company_name"] == "Acme" and rows[0]["apply"] == "" and rows[0]["planned_at"]
