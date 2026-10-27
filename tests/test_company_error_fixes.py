"""Regression tests for the code bugs behind Scraper health failures.

Each test names the failure it reproduces from the admin Scraper health page.
"""
from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import requests

from models import JobData
from sources.api.workday_source import WorkdaySource, parse_workday_board_url, workday_api_site
from utils import http_retry
from utils.http_retry import retry_call
from utils.job_storage import MAX_LOCATION_CHARS, _clamp_location, _dedupe_by_url, _INSERT_SQL


# ------------------------------------------------------------- Workday URLs
@pytest.mark.parametrize("site,expected", [
    ("External_Career_Site", "External_Career_Site"),
    ("en-US/OSUCareers", "OSUCareers"),          # was 405 Method Not Allowed
    ("en-us/AMD", "AMD"),
    ("fr_CA/Careers", "Careers"),
    ("External/job/Engineer_R123", "External"),
    ("de-DE/Site/job/x", "Site"),
])
def test_workday_api_site_drops_locale_and_job_path(site, expected):
    assert workday_api_site(site) == expected


@pytest.mark.parametrize("site", ["es", "en-US", "en-US/job/x"])
def test_workday_locale_only_path_is_a_clear_error(site):
    # Telpark stored ".../es": the API was called with site "es" and 404'd.
    with pytest.raises(ValueError, match="no site name"):
        workday_api_site(site)


def test_workday_public_urls_keep_stored_locale_but_api_drops_it():
    """Stored job URLs must not change, or every existing job looks new."""
    source = WorkdaySource("workday", "id", {}, rate_limit_per_minute=600)
    board = "https://osu.wd1.myworkdayjobs.com/en-US/OSUCareers"
    posting = {"title": "Analyst", "externalPath": "/job/Analyst_R1",
               "locationsText": "Columbus, OH", "postedOn": "Posted Today"}
    with patch.object(source, "_fetch_all_postings", return_value=[posting]) as fetch, \
         patch("sources.api.workday_source.urls_with_existing_description",
               return_value={"https://osu.wd1.myworkdayjobs.com/en-US/OSUCareers/job/Analyst_R1"}):
        jobs = source.fetch_jobs(board, "Ohio State")
    assert fetch.call_args[0][0] == "https://osu.wd1.myworkdayjobs.com/wday/cxs/osu/OSUCareers"
    assert str(jobs[0].url) == "https://osu.wd1.myworkdayjobs.com/en-US/OSUCareers/job/Analyst_R1"
    assert parse_workday_board_url(board)[2] == "en-US/OSUCareers"


# ---------------------------------------------------------- Workday timeouts
def test_workday_caps_detail_fetches_per_run():
    """146 Workday companies hit the 1200s timeout fetching every detail on run 1."""
    source = WorkdaySource("workday", "id", {"max_detail_fetches_per_run": 3},
                           rate_limit_per_minute=600)
    postings = [{"title": f"Role {i}", "externalPath": f"/job/R{i}",
                 "locationsText": "Remote", "postedOn": "Posted Today"} for i in range(10)]
    with patch.object(source, "_fetch_all_postings", return_value=postings), \
         patch("sources.api.workday_source.urls_with_existing_description", return_value=set()), \
         patch.object(source, "_fetch_detail",
                      return_value={"jobPostingInfo": {"jobDescription": "<p>d</p>"}}) as detail:
        jobs = source.fetch_jobs("https://acme.wd1.myworkdayjobs.com/Careers", "Acme")
    assert len(jobs) == 10                 # every job is still saved
    assert detail.call_count == 3          # but only 3 detail GETs this run
    assert sum(1 for j in jobs if j.job_description) == 3


def test_workday_default_detail_cap_is_50():
    source = WorkdaySource("workday", "id", {}, rate_limit_per_minute=600)
    assert source._max_detail_fetches() == 50
    bad = WorkdaySource("workday", "id", {"max_detail_fetches_per_run": "x"}, 600)
    assert bad._max_detail_fetches() == 50


# ------------------------------------------------------------- DB errors
def _job(url, desc=None, location="Remote"):
    return JobData(url=url, job_title="Eng", company="Acme", location=location,
                   job_description=desc, date_posted=date(2026, 1, 1),
                   source_website="workday", job_id_from_source="1", company_id=uuid4())


def test_duplicate_urls_in_one_fetch_are_collapsed():
    """Target failed with 'duplicate key value violates unique constraint jobs_url_key'."""
    a = _job("https://x.example/1")
    b = _job("https://x.example/1", desc="has description")
    c = _job("https://x.example/2")
    kept = _dedupe_by_url([a, b, c])
    assert [str(j.url) for j in kept] == ["https://x.example/1", "https://x.example/2"]
    assert kept[0].job_description == "has description"


def test_insert_is_idempotent_on_url():
    # Two workers scraping duplicate company rows insert the same job at once.
    assert "ON CONFLICT (url) DO NOTHING" in _INSERT_SQL


def test_long_location_is_clamped_under_index_limit():
    """Scholastic/Laticrete: 'index row size 4088 exceeds btree maximum 2704'."""
    huge = "; ".join(f"City {i}, ST" for i in range(800))
    clamped = _clamp_location(huge)
    assert len(clamped) <= MAX_LOCATION_CHARS + 2
    assert len(clamped.encode("utf-8")) < 2704
    assert clamped.endswith("…")
    assert _clamp_location("Remote") == "Remote"
    assert _clamp_location(None) is None


@patch("utils.job_storage._extract_skills_safe")
@patch("utils.job_storage.db.get_db_connection")
def test_save_jobs_applies_dedupe_and_clamp(get_conn, _extract):
    from utils import job_storage
    seen = []
    with patch.object(job_storage, "_persist_chunk",
                      side_effect=lambda chunk, ep: (seen.extend(chunk), (len(chunk), 0, []))[1]):
        job_storage.save_jobs([
            _job("https://x.example/1", location="Toronto, ON " + "x" * 3000),
            _job("https://x.example/1"),
        ])
    assert len(seen) == 1
    assert len(seen[0].location) <= MAX_LOCATION_CHARS + 2


# ------------------------------------------------------------- retries
@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(http_retry.time, "sleep", lambda s: None)


def _resp(status):
    r = MagicMock()
    r.status_code = status
    return r


def test_retry_recovers_from_dns_blip(no_sleep):
    fn = MagicMock(side_effect=[requests.exceptions.ConnectionError("NameResolutionError"),
                                _resp(200)])
    assert retry_call(fn, "https://boards-api.greenhouse.io/x", timeout=30).status_code == 200
    assert fn.call_count == 2


def test_retry_recovers_from_502_and_read_timeout(no_sleep):
    fn = MagicMock(side_effect=[_resp(502), requests.exceptions.ReadTimeout("slow"), _resp(200)])
    assert retry_call(fn, "u").status_code == 200
    assert fn.call_count == 3


@pytest.mark.parametrize("status", [404, 422, 403, 405, 400])
def test_real_answers_are_not_retried(no_sleep, status):
    fn = MagicMock(return_value=_resp(status))
    assert retry_call(fn, "u").status_code == status
    assert fn.call_count == 1


def test_gives_up_after_three_attempts(no_sleep):
    fn = MagicMock(side_effect=requests.exceptions.ConnectTimeout("down"))
    with pytest.raises(requests.exceptions.ConnectTimeout):
        retry_call(fn, "u")
    assert fn.call_count == 3
    fn = MagicMock(return_value=_resp(503))
    assert retry_call(fn, "u").status_code == 503
    assert fn.call_count == 3
