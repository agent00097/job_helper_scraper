"""services.source_reconcile: safety rules first, then planning and the queue job.

Rushabh's requirement: never remove or disable anything that still works.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services import source_reconcile as sr
from utils import board_finder as bf

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
GH404 = "404 Client Error: Not Found for url: https://boards-api.greenhouse.io/v1/boards/x/jobs"


def rows_for(cid, src, *results):
    """results newest-first: ('failed', http_status, msg, hours_ago) or ('success', jobs, hours_ago)."""
    out = []
    for r in results:
        if r[0] == "success":
            out.append((cid, src, "success", None, None, r[1], NOW - timedelta(hours=r[2])))
        else:
            out.append((cid, src, "failed", r[1], r[2], 0, NOW - timedelta(hours=r[3])))
    return out


def health_from(rows):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = rows
    return sr.load_health(conn)


def company(cid="c1", name="Sparrow", **sources):
    return sr.Company(cid, name, sources, None, True)


# ------------------------------------------------------------ safety rules
def test_three_board_gone_failures_over_a_day_is_broken():
    h = health_from(rows_for("c1", "greenhouse",
                             ("failed", 404, GH404, 1), ("failed", 404, GH404, 13), ("failed", 404, GH404, 30)))
    assert sr.broken_reason("greenhouse", h[("c1", "greenhouse")])[0] == "board_gone"


def test_any_success_in_window_means_not_broken():
    h = health_from(rows_for("c1", "greenhouse",
                             ("failed", 404, GH404, 1), ("failed", 404, GH404, 13),
                             ("failed", 404, GH404, 30), ("success", 12, 50)))
    assert sr.broken_reason("greenhouse", h[("c1", "greenhouse")]) is None


def test_frequent_runs_use_the_whole_failing_streak_for_the_span():
    # Scraper runs every 4h: the newest 3 failures are within 8h, but the board
    # has been failing for 2 days with no success.
    gone = ("failed", 404, "404 Client Error: Not Found for url: https://x")
    rows = rows_for("c1", "lever", *[(*gone, 4 * i + 1) for i in range(12)])
    h = health_from(rows)[("c1", "lever")]
    assert h.streak_failures == 12 and len(h.last_failures) == 3
    assert sr.broken_reason("lever", h) is not None


def test_failures_within_a_few_hours_are_not_enough():
    h = health_from(rows_for("c1", "greenhouse",
                             ("failed", 404, GH404, 1), ("failed", 404, GH404, 3), ("failed", 404, GH404, 5)))
    assert sr.broken_reason("greenhouse", h[("c1", "greenhouse")]) is None


def test_fewer_than_three_failures_is_not_enough():
    h = health_from(rows_for("c1", "greenhouse", ("failed", 404, GH404, 1), ("failed", 404, GH404, 40)))
    assert sr.broken_reason("greenhouse", h[("c1", "greenhouse")]) is None


@pytest.mark.parametrize("msg,status", [
    ("workday/Acme: exceeded 1200s company scrape timeout", None),
    ("couldn't get a connection after 30.00 sec", None),
    ("502 Server Error: Bad Gateway for url: https://api.lever.co/v0/postings/x", 502),
    ("HTTPSConnectionPool(host='boards-api.greenhouse.io', port=443): Read timed out.", None),
    ("index row size 4088 exceeds btree version 4 maximum 2704", None),
])
def test_temporary_or_our_bug_failures_never_qualify(msg, status):
    h = health_from(rows_for("c1", "workday", ("failed", status, msg, 1),
                             ("failed", status, msg, 20), ("failed", status, msg, 40)))
    assert sr.broken_reason("workday", h[("c1", "workday")]) is None


def test_one_temporary_failure_in_the_streak_disqualifies():
    h = health_from(rows_for("c1", "greenhouse", ("failed", 404, GH404, 1),
                             ("failed", 502, "502 Server Error: Bad Gateway", 20), ("failed", 404, GH404, 40)))
    assert sr.broken_reason("greenhouse", h[("c1", "greenhouse")]) is None


def test_only_the_latest_unbroken_streak_counts():
    # fail, fail, success, fail: the success breaks the streak -> not broken
    h = health_from(rows_for("c1", "lever", ("failed", 404, GH404, 1), ("failed", 404, GH404, 20),
                             ("success", 3, 30), ("failed", 404, GH404, 40)))
    assert sr.broken_reason("lever", h[("c1", "lever")]) is None


def test_healthy_other_source_requires_recent_success_with_jobs():
    rows = rows_for("c1", "ashby", ("success", 12, 2)) + rows_for("c1", "lever", ("success", 0, 2))
    h = health_from(rows)
    c = company(ashby="sparrow", lever="sparrow", greenhouse="sparrow")
    assert sr.healthy_other_sources(c, "greenhouse", h) == {"ashby": "sparrow"}   # empty lever doesn't count


# ------------------------------------------------------------ planning
def broken_health(cid, src):
    return health_from(rows_for(cid, src, ("failed", 404, GH404, 1),
                                ("failed", 404, GH404, 20), ("failed", 404, GH404, 40)))


def test_dead_source_removed_only_when_company_works_elsewhere():
    h = {**broken_health("c1", "greenhouse"), **health_from(rows_for("c1", "ashby", ("success", 12, 2)))}
    c = company(greenhouse="sparrow", ashby="sparrow")
    plan = sr.build_plan({"c1": c}, h, include_empty=False)
    assert [(a["action"], a["confidence"], a["source"]) for a in plan] == [("remove_source", "high", "greenhouse")]


def test_board_that_answers_again_is_never_changed():
    c = company(cid="c2", name="TJX", workday="https://tjx.wd1.myworkdayjobs.com/TJX_EXTERNAL")
    dns = "HTTPSConnectionPool(host='tjx.wd1.myworkdayjobs.com', port=443): (Caused by NameResolutionError(\"Failed to resolve\"))"
    h = health_from(rows_for("c2", "workday", ("failed", None, dns, 1), ("failed", None, dns, 20), ("failed", None, dns, 40)))
    alive = bf.Hit("workday", c.sources["workday"], 11350, confidence="high")
    with patch.object(sr, "_search", return_value=(alive, {})):
        plan = sr.build_plan({"c2": c}, h, include_empty=False)
    assert plan[0]["action"] == "wait"


def test_relocation_proposed_with_new_board():
    c = company(cid="c3", name="Wayve", greenhouse="wayve")
    hit = bf.Hit("ashby", "wayve", 175, evidence="company named in job posts", confidence="high")
    with patch.object(sr, "_search", return_value=(hit, {})):
        plan = sr.build_plan({"c3": c}, broken_health("c3", "greenhouse"), include_empty=False)
    a = plan[0]
    assert (a["action"], a["new_source"], a["new_endpoint"], a["new_board_jobs"]) == ("relocate", "ashby", "wayve", 175)


def test_relocation_onto_another_rows_board_is_not_a_relocate():
    c4 = company(cid="c4", name="Temporal Technologies", greenhouse="temporaltechnologies")
    c5 = company(cid="c5", name="Temporal", ashby="temporal")
    hit = bf.Hit("ashby", "temporal", 70, confidence="high")
    with patch.object(sr, "_search", return_value=(hit, {})):
        plan = sr.build_plan({"c4": c4, "c5": c5}, broken_health("c4", "greenhouse"), include_empty=False)
    a = [x for x in plan if x["company_id"] == "c4"][0]
    assert a["action"] == "disable_company" and a["confidence"] == "medium"


def test_nothing_found_is_medium_so_never_auto_applied():
    c = company(cid="c6", name="Ghost", lever="ghost")
    with patch.object(sr, "_search", return_value=(None, {"status": "not_found"})):
        plan = sr.build_plan({"c6": c}, broken_health("c6", "lever"), include_empty=False)
    assert (plan[0]["action"], plan[0]["confidence"]) == ("disable_company", "medium")


def test_blocked_is_review():
    c = company(cid="c7", name="Socure", workday="https://socure.wd1.myworkdayjobs.com/SocureCareers")
    m = "403 Client Error: Forbidden for url: https://socure.wd1.myworkdayjobs.com/wday/cxs/socure/SocureCareers/jobs"
    h = health_from(rows_for("c7", "workday", ("failed", 403, m, 1), ("failed", 403, m, 20), ("failed", 403, m, 40)))
    with patch.object(sr, "_search", return_value=(None, {"status": "not_found"})):
        plan = sr.build_plan({"c7": c}, h, include_empty=False)
    assert plan[0]["action"] == "review"


def test_healthy_company_gets_no_action_at_all():
    c = company(cid="c8", name="Fine Co", greenhouse="fineco")
    h = health_from(rows_for("c8", "greenhouse", ("success", 40, 1), ("success", 38, 13)))
    with patch.object(sr, "_search") as search:
        assert sr.build_plan({"c8": c}, h, include_empty=True) == []
    search.assert_not_called()


def test_empty_board_only_considered_when_asked():
    c = company(cid="c9", name="Quiet", greenhouse="quiet", ashby="quiet")
    h = {**health_from(rows_for("c9", "greenhouse", ("success", 0, 1), ("success", 0, 20), ("success", 0, 40))),
         **health_from(rows_for("c9", "ashby", ("success", 9, 1)))}
    assert sr.build_plan({"c9": c}, h, include_empty=False) == []
    plan = sr.build_plan({"c9": c}, h, include_empty=True)
    assert [(a["action"], a["source"], a["category"]) for a in plan] == [("remove_source", "greenhouse", "empty_board")]


def test_duplicate_rows_keep_the_real_name():
    t = sr.Company("t1", "Target", {"workday": "https://target.wd5.myworkdayjobs.com/targetcareers"}, "target.com", True)
    tc = sr.Company("t2", "Targetcareers", {"workday": "https://target.wd5.myworkdayjobs.com/en-US/targetcareers"}, None, True)
    plan, owners = sr.plan_duplicates({"t1": t, "t2": tc})
    assert [(a["company_id"], a["action"], a["confidence"]) for a in plan] == [("t2", "disable_company", "medium")]


def test_planning_error_for_one_company_does_not_sink_the_run():
    c = company(cid="e1", name="Boom", greenhouse="boom")
    with patch.object(sr, "_search", side_effect=RuntimeError("network down")):
        plan = sr.build_plan({"e1": c}, broken_health("e1", "greenhouse"), include_empty=False)
    assert (plan[0]["action"], plan[0]["category"]) == ("review", "error")


# ------------------------------------------------------------ queue job


def test_diagnose_explains_every_failing_board():
    gone = ("failed", 404, "404 Client Error: Not Found for url: https://x")
    pool = ("failed", None, "couldn't get a connection after 30.00 sec")
    rows = (rows_for("c1", "lever", *[(*gone, 4 * i + 1) for i in range(8)])       # broken
            + rows_for("c2", "lever", (*gone, 1), (*gone, 2))                     # too few
            + rows_for("c3", "lever", (*gone, 1), (*gone, 2), (*gone, 3))         # under 24h
            + rows_for("c4", "lever", *[(*pool, 4 * i + 1) for i in range(8)])    # our side
            + rows_for("c5", "lever", ("success", 10, 1)))                         # healthy
    d = sr.diagnose(health_from(rows))
    assert sum(d.values()) == 4
    assert d["broken: HTTP 404"] == 1
    assert d["not yet: fewer than 3 failures"] == 1
    assert d["not yet: failing for under 24h"] == 1
    assert any(k.startswith("ignored") and "DB pool" in k for k in d)
