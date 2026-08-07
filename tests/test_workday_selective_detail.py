"""Unit tests for selective Workday per-job detail fetching."""
from unittest.mock import MagicMock, patch

from sources.api.workday_source import WorkdaySource

BOARD = "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"
PUBLIC_BASE = "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"
API_BASE = (
    "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/"
    "salesforce/External_Career_Site"
)


def _source() -> WorkdaySource:
    return WorkdaySource(
        name="workday",
        source_id="test-id",
        config={},
        rate_limit_per_minute=600,
    )


def _posting(path: str, title: str) -> dict:
    return {
        "title": title,
        "externalPath": path,
        "locationsText": "San Francisco, CA",
        "postedOn": "01/15/2026",
    }


def _detail(description_html: str) -> dict:
    return {
        "jobPostingInfo": {
            "jobDescription": description_html,
            "timeType": "Full_time",
            "remoteType": "Remote",
            "title": "ignored-for-list-title",
        }
    }


def test_fetch_jobs_skips_detail_when_description_already_stored():
    source = _source()
    postings = [
        _posting("/job/Keep_JR1", "Keep Me"),
        _posting("/job/New_JR2", "New Role"),
    ]
    known = {f"{PUBLIC_BASE}/job/Keep_JR1"}

    with patch.object(source, "_fetch_all_postings", return_value=postings), patch(
        "sources.api.workday_source.urls_with_existing_description",
        return_value=known,
    ), patch.object(
        source,
        "_fetch_detail",
        return_value=_detail("<p>New role description</p>"),
    ) as mock_detail:
        jobs = source.fetch_jobs(BOARD, "Salesforce")

    assert len(jobs) == 2
    by_id = {j.job_id_from_source: j for j in jobs}
    assert by_id["job_Keep_JR1"].job_description is None
    assert by_id["job_New_JR2"].job_description == "New role description"
    assert by_id["job_New_JR2"].employment_type == "Full-time"
    assert by_id["job_New_JR2"].remote_allowed is True
    mock_detail.assert_called_once_with(API_BASE, "/job/New_JR2")


def test_fetch_jobs_details_all_when_none_described():
    source = _source()
    postings = [
        _posting("/job/A_JR1", "Role A"),
        _posting("/job/B_JR2", "Role B"),
    ]

    with patch.object(source, "_fetch_all_postings", return_value=postings), patch(
        "sources.api.workday_source.urls_with_existing_description",
        return_value=set(),
    ), patch.object(
        source,
        "_fetch_detail",
        side_effect=lambda _api, path: _detail(f"<p>desc {path}</p>"),
    ) as mock_detail:
        jobs = source.fetch_jobs(BOARD, "Salesforce")

    assert len(jobs) == 2
    assert all(j.job_description for j in jobs)
    assert mock_detail.call_count == 2


def test_parse_job_list_only_skips_http():
    source = _source()
    posting = _posting("/job/ListOnly_JR9", "List Only")

    with patch.object(source, "_fetch_detail") as mock_detail:
        job = source._parse_job(posting, "Salesforce", PUBLIC_BASE, detail=None)

    assert job is not None
    assert job.job_title == "List Only"
    assert job.job_description is None
    assert job.location == "San Francisco, CA"
    mock_detail.assert_not_called()
