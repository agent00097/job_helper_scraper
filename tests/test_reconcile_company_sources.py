"""reconcile_company_sources: plan actions for every failing (company, source).

Network calls are mocked; reasons are real Scraper health strings.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts import reconcile_company_sources as rc

rdb = rc.rdb  # the exact module object the script uses (patch this one)


# ------------------------------------------------------------ classification
@pytest.mark.parametrize("source,reason,category", [
    ("greenhouse", "404 Client Error: Not Found for url: https://boards-api.greenhouse.io/v1/boards/glassdoor/jobs", "board_gone"),
    ("workday", "422 Client Error: Unprocessable Entity for url: https://zappos.wd1.myworkdayjobs.com/wday/cxs/zappos/Zappos/jobs", "board_gone"),
    ("workday", "workday/Wynd: exceeded 1200s company scrape timeout", "code_fixed"),
    ("workday", "couldn't get a connection after 30.00 sec", "transient"),
    ("workday", "405 Client Error: Method Not Allowed for url: https://osu.wd1.myworkdayjobs.com/wday/cxs/osu/en-US/OSUCareers/jobs", "code_fixed"),
    ("workday", "404 Client Error: Not Found for url: https://telpark.wd103.myworkdayjobs.com/wday/cxs/telpark/es/jobs", "code_fixed"),
    ("workday", "duplicate key value violates unique constraint \"jobs_url_key\"", "code_fixed"),
    ("workday", "index row size 4088 exceeds btree version 4 maximum 2704", "code_fixed"),
    ("lever", "502 Server Error: Bad Gateway for url: https://api.lever.co/v0/postings/InsightM?mode=json", "transient"),
    ("greenhouse", "HTTPSConnectionPool(host='boards-api.greenhouse.io', port=443): Read timed out. (read timeout=30)", "transient"),
    ("greenhouse", "HTTPSConnectionPool(host='boards-api.greenhouse.io', port=443): Max retries exceeded (Caused by NameResolutionError(\"Failed to resolve 'boards-api.greenhouse.io'\"))", "transient"),
    ("workday", "HTTPSConnectionPool(host='whataburger.wd5.myworkdayjobs.com', port=443): Max retries exceeded (Caused by NameResolutionError(\"Failed to resolve\"))", "board_gone"),
    ("successfactors", "HTTPSConnectionPool(host='jobs.l3harris.com', port=443): Max retries exceeded with url: /tile-search-results/?startrow=1100 (Caused by NameResolutionError(\"x\"))", "transient"),
    ("successfactors", "404 Client Error: Not Found for url: https://jobs.sap.com/en/tile-search-results/?startrow=0", "code_fixed"),
    ("successfactors", "SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired'))", "external"),
    ("workday", "403 Client Error: Forbidden for url: https://socure.wd1.myworkdayjobs.com/wday/cxs/socure/SocureCareers/jobs", "blocked"),
])
def test_classify_failure(source, reason, category):
    assert rc.classify_failure(source, reason)[0] == category


# ------------------------------------------------------------ fixtures
def companies_json(tmp_path, rows):
    p = tmp_path / "companies.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return rc.load_companies(str(p))


def C(name, *sources, d="—", st="Enabled"):
    return {"n": name, "nn": name.lower(), "s": list(sources), "d": d, "st": st, "sc": ""}


GH404 = "404 Client Error: Not Found for url: https://boards-api.greenhouse.io/v1/boards/x/jobs"


def plan(by_name, failing=(), zero=(), offline=False, probe_zero=False):
    health = {"failing": [{"n": n, "src": s, "reason": r} for n, s, r in failing],
              "zero": [{"n": n, "src": s} for n, s in zero]}
    return rc.build_plan(by_name, health, probe_zero=probe_zero, offline=offline, workers=1)


# ------------------------------------------------------------ rules
def test_dead_source_removed_when_company_healthy_elsewhere(tmp_path):
    """User's ABC case: Sparrow fails on Greenhouse but scrapes fine on Ashby."""
    by = companies_json(tmp_path, [C("Sparrow", "ashby:sparrow", "greenhouse:sparrow")])
    rows = plan(by, failing=[("Sparrow", "greenhouse", GH404)], offline=True)
    assert len(rows) == 1
    r = rows[0]
    assert (r["action"], r["confidence"], r["source"]) == ("remove_source", "high", "greenhouse")
    assert "ashby" in r["evidence"]


def test_not_removed_when_other_source_also_failing(tmp_path):
    by = companies_json(tmp_path, [C("Acme", "ashby:acme", "greenhouse:acme")])
    rows = plan(by, failing=[("Acme", "greenhouse", GH404), ("Acme", "ashby", GH404)], offline=True)
    assert all(r["action"] == "review" for r in rows)


def test_transient_and_code_fixed_failures_wait(tmp_path):
    by = companies_json(tmp_path, [C("Wynd", "workday:https://wynd.wd1.myworkdayjobs.com/Careers")])
    rows = plan(by, failing=[("Wynd", "workday", "workday/Wynd: exceeded 1200s company scrape timeout")])
    assert rows[0]["action"] == "wait"


def test_junk_board_is_disabled(tmp_path):
    by = companies_json(tmp_path, [C("LI Test Company", "greenhouse:linkedin")])
    rows = plan(by, failing=[("LI Test Company", "greenhouse", GH404)], offline=True)
    assert rows[0]["action"] == "disable_company" and rows[0]["confidence"] == "high"


def test_relocated_board_found_online(tmp_path):
    by = companies_json(tmp_path, [C("Wayve", "greenhouse:wayve")])
    hit = rdb.Hit("ashby", "wayve", 175, evidence="company named in job posts", confidence="high")
    with patch.object(rc, "find_relocation", return_value=(hit, {})):
        rows = plan(by, failing=[("Wayve", "greenhouse", GH404)])
    r = rows[0]
    assert (r["action"], r["new_source"], r["new_endpoint"], r["confidence"]) == ("relocate", "ashby", "wayve", "high")


def test_relocation_to_board_owned_by_another_row_removes_instead(tmp_path):
    by = companies_json(tmp_path, [C("Temporal Technologies", "greenhouse:temporaltechnologies"),
                                   C("Temporal", "ashby:temporal")])
    hit = rdb.Hit("ashby", "temporal", 70, confidence="high")
    with patch.object(rc, "find_relocation", return_value=(hit, {})):
        rows = plan(by, failing=[("Temporal Technologies", "greenhouse", GH404)])
    r = [x for x in rows if x["company_name"] == "Temporal Technologies"][0]
    assert r["action"] == "remove_source"
    assert "'Temporal'" in r["evidence"]


def test_board_alive_again_means_wait_not_relocate(tmp_path):
    ep = "https://tjx.wd1.myworkdayjobs.com/TJX_EXTERNAL"
    by = companies_json(tmp_path, [C("TJX", f"workday:{ep}")])
    hit = rdb.Hit("workday", ep, 900, confidence="high")
    dns = "HTTPSConnectionPool(host='tjx.wd1.myworkdayjobs.com', port=443): (Caused by NameResolutionError(\"Failed to resolve\"))"
    with patch.object(rc, "find_relocation", return_value=(hit, {})):
        rows = plan(by, failing=[("TJX", "workday", dns)])
    assert rows[0]["action"] == "wait"
    assert "temporary" in rows[0]["evidence"]


def test_nothing_found_disables_single_source_company(tmp_path):
    by = companies_json(tmp_path, [C("Ghost", "lever:ghost")])
    with patch.object(rc, "find_relocation", return_value=(None, {"status": "not_found"})):
        rows = plan(by, failing=[("Ghost", "lever", "404 Client Error: Not Found for url: https://api.lever.co/v0/postings/ghost")])
    assert rows[0]["action"] == "disable_company"
    assert rows[0]["confidence"] == "medium"   # never auto-applied


def test_unsupported_ats(tmp_path):
    by = companies_json(tmp_path, [C("GitHub", "greenhouse:github")])
    with patch.object(rc, "find_relocation", return_value=(None, {"status": "unsupported_ats", "new_source": "icims"})):
        rows = plan(by, failing=[("GitHub", "greenhouse", GH404)])
    assert rows[0]["action"] == "disable_company" and "icims" in rows[0]["evidence"]


def test_empty_board_removed_when_company_has_jobs_elsewhere(tmp_path):
    by = companies_json(tmp_path, [C("Acme", "ashby:acme", "greenhouse:acme")])
    rows = plan(by, zero=[("Acme", "greenhouse")], probe_zero=True, offline=True)
    assert rows[0]["action"] == "remove_source" and rows[0]["category"] == "empty_board"


def test_duplicate_rows_on_same_board(tmp_path):
    by = companies_json(tmp_path, [
        C("Target", "workday:https://target.wd5.myworkdayjobs.com/targetcareers", d="target.com"),
        C("Targetcareers", "workday:https://target.wd5.myworkdayjobs.com/en-US/targetcareers"),
    ])
    rows = plan(by)
    assert len(rows) == 1
    r = rows[0]
    assert (r["company_name"], r["action"], r["category"]) == ("Targetcareers", "disable_company", "duplicate_row")
    assert "'Target'" in r["evidence"]


# ------------------------------------------------------------ Workday probe
def _resp(status, data=None, url=""):
    r = MagicMock(status_code=status, url=url)
    r.json.return_value = data
    return r


def test_workday_probe_finds_tenant_on_new_data_center():
    def post(url, **_kw):
        if url.startswith("https://whataburger.wd5."):
            raise requests.exceptions.ConnectionError("NameResolutionError")
        if url.startswith("https://whataburger.wd1."):
            return _resp(200, {"total": 120, "jobPostings": [{}]})
        raise requests.exceptions.ConnectionError("no host")
    with patch.object(rdb.SESSION, "post", side_effect=post):
        hit = rc.probe_workday("https://whataburger.wd5.myworkdayjobs.com/WAB_CAREERS")
    assert hit.endpoint == "https://whataburger.wd1.myworkdayjobs.com/WAB_CAREERS"
    assert "moved from wd5 to wd1" in hit.evidence and hit.jobs == 120


def test_workday_probe_finds_new_site_name_after_422():
    def post(url, **_kw):
        if not url.startswith("https://zappos.wd1."):
            raise requests.exceptions.ConnectionError("no host")
        if "/zappos/Zappos/jobs" in url:
            return _resp(422)
        if "/zappos/External/jobs" in url:
            return _resp(200, {"total": 40, "jobPostings": []})
        return _resp(404)
    get = MagicMock(return_value=_resp(200, url="https://zappos.wd1.myworkdayjobs.com/en-US/External"))
    with patch.object(rdb.SESSION, "post", side_effect=post), patch.object(rdb.SESSION, "get", get):
        hit = rc.probe_workday("https://zappos.wd1.myworkdayjobs.com/Zappos")
    assert hit.endpoint == "https://zappos.wd1.myworkdayjobs.com/External"
    assert hit.confidence == "high" and "default site" in hit.evidence


def test_apply_sql_is_safe_and_high_confidence_only():
    sql = rc.APPLY_SQL
    assert sql.count("p.confidence = 'high'") == 3
    assert "jsonb_build_object(p.new_source::text, p.new_endpoint::text)" in sql
    assert "sc.source_endpoints - p.source" in sql
    assert "(sc.source_endpoints - p.source) <> '{}'::jsonb" in sql   # never leave a company with no source
    assert "COMMIT;" not in sql.split("-- Check the RETURNING")[0]  # reviewer commits manually
