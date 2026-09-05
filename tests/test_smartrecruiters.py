"""Unit tests for SmartRecruitersSource — URL parse, list pagination, selective detail."""
from unittest.mock import MagicMock, patch

import requests

from sources.api.smartrecruiters_source import (
    SmartRecruitersSource,
    parse_smartrecruiters_job_url,
)


def _source() -> SmartRecruitersSource:
    return SmartRecruitersSource(
        name="smartrecruiters",
        source_id="test-id",
        config={},
        rate_limit_per_minute=600,
    )


def _list_posting(posting_id: str, title: str, remote: bool = False) -> dict:
    return {
        "id": posting_id,
        "uuid": f"uuid-{posting_id}",
        "name": title,
        "company": {"identifier": "Visa", "name": "Visa Inc"},
        "releasedDate": "2026-01-15T12:00:00.000Z",
        "location": {
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "remote": remote,
        },
        "typeOfEmployment": {"id": "perm", "label": "Full-time"},
    }


def _detail(posting_id: str, title: str, description: str) -> dict:
    payload = _list_posting(posting_id, title)
    payload["applyUrl"] = f"https://jobs.smartrecruiters.com/Visa/{posting_id}?oga=true"
    payload["jobAd"] = {
        "sections": {
            "companyDescription": {"title": "Company", "text": "We hire people."},
            "jobDescription": {"title": "Job Description", "text": f"<p>{description}</p>"},
            "qualifications": {"title": "Qualifications", "text": "<li>Python</li>"},
        }
    }
    return payload


def test_parse_numeric_slug_url():
    assert parse_smartrecruiters_job_url(
        "https://jobs.smartrecruiters.com/Visa/743999999-software-engineer"
    ) == ("Visa", "743999999")


def test_parse_bare_id_url():
    assert parse_smartrecruiters_job_url(
        "https://jobs.smartrecruiters.com/smartrecruiters/74983486"
    ) == ("smartrecruiters", "74983486")


def test_parse_uuid_url():
    uuid = "34225731-e7cf-4584-b0b7-78098fe1a66b"
    assert parse_smartrecruiters_job_url(
        f"https://jobs.smartrecruiters.com/Visa/{uuid}"
    ) == ("Visa", uuid)


def test_parse_api_and_careers_hosts():
    assert parse_smartrecruiters_job_url(
        "https://api.smartrecruiters.com/v1/companies/Visa/postings/12345"
    ) == ("Visa", "12345")
    assert parse_smartrecruiters_job_url(
        "https://careers.smartrecruiters.com/IKEA/999"
    ) == ("IKEA", "999")


def test_parse_external_referrals_url():
    uuid = "7d344d4d-95ad-432f-8fb8-a97b292bbf68"
    assert parse_smartrecruiters_job_url(
        "https://jobs.smartrecruiters.com/external-referrals/company/Visa/"
        f"publication/{uuid}"
    ) == ("Visa", uuid)


def test_parse_rejects_unknown_host_and_board_only():
    assert parse_smartrecruiters_job_url("https://example.com/Visa/123") is None
    assert parse_smartrecruiters_job_url("https://jobs.smartrecruiters.com/Visa") is None


def test_parse_job_list_only_skips_description():
    source = _source()
    job = source._parse_job(
        _list_posting("101", "Engineer", remote=True),
        "Configured",
        "Visa",
        detail=None,
    )
    assert job is not None
    assert job.job_title == "Engineer"
    assert job.company == "Visa Inc"
    assert job.location == "San Francisco, CA, US"
    assert job.remote_allowed is True
    assert job.employment_type == "Full-time"
    assert job.job_description is None
    assert str(job.url) == "https://jobs.smartrecruiters.com/Visa/101"
    assert job.job_id_from_source == "101"


def test_parse_job_detail_builds_description_and_apply_url():
    source = _source()
    posting = _list_posting("101", "Engineer")
    job = source._parse_job(posting, "Configured", "Visa", detail=_detail("101", "Engineer", "Build APIs"))
    assert job is not None
    assert "Job Description" in (job.job_description or "")
    assert "Build APIs" in (job.job_description or "")
    assert "Python" in (job.job_description or "")
    assert "We hire people." not in (job.job_description or "")
    assert "oga=true" in str(job.application_url)


def test_fetch_jobs_skips_detail_when_description_already_stored():
    source = _source()
    known = {"https://jobs.smartrecruiters.com/Visa/101"}

    with patch.object(
        source,
        "_fetch_all_postings",
        return_value=[
            _list_posting("101", "Keep Me"),
            _list_posting("102", "New Role"),
        ],
    ), patch(
        "sources.api.smartrecruiters_source.urls_with_existing_description",
        return_value=known,
    ), patch.object(
        source,
        "_fetch_detail",
        return_value=_detail("102", "New Role", "New role description"),
    ) as mock_detail:
        jobs = source.fetch_jobs("Visa", "Visa")

    assert len(jobs) == 2
    by_id = {j.job_id_from_source: j for j in jobs}
    assert by_id["101"].job_description is None
    assert "New role description" in (by_id["102"].job_description or "")
    mock_detail.assert_called_once_with("Visa", "102")


def test_fetch_jobs_details_all_when_none_described():
    source = _source()
    with patch.object(
        source,
        "_fetch_all_postings",
        return_value=[
            _list_posting("201", "Role A"),
            _list_posting("202", "Role B"),
        ],
    ), patch(
        "sources.api.smartrecruiters_source.urls_with_existing_description",
        return_value=set(),
    ), patch.object(
        source,
        "_fetch_detail",
        side_effect=lambda _ep, jid: _detail(jid, f"Role {jid}", f"desc-{jid}"),
    ) as mock_detail:
        jobs = source.fetch_jobs("Visa", "Visa")

    assert len(jobs) == 2
    assert mock_detail.call_count == 2
    assert all("desc-" in (j.job_description or "") for j in jobs)


def test_fetch_jobs_caps_detail_fetches_per_run():
    source = SmartRecruitersSource(
        name="smartrecruiters",
        source_id="test-id",
        config={"max_detail_fetches_per_run": 1},
        rate_limit_per_minute=600,
    )
    with patch.object(
        source,
        "_fetch_all_postings",
        return_value=[
            _list_posting("201", "Role A"),
            _list_posting("202", "Role B"),
            _list_posting("203", "Role C"),
        ],
    ), patch(
        "sources.api.smartrecruiters_source.urls_with_existing_description",
        return_value=set(),
    ), patch.object(
        source,
        "_fetch_detail",
        side_effect=lambda _ep, jid: _detail(jid, f"Role {jid}", f"desc-{jid}"),
    ) as mock_detail:
        jobs = source.fetch_jobs("Visa", "Visa")

    assert len(jobs) == 3
    mock_detail.assert_called_once_with("Visa", "201")
    by_id = {j.job_id_from_source: j for j in jobs}
    assert "desc-201" in (by_id["201"].job_description or "")
    assert by_id["202"].job_description is None
    assert by_id["203"].job_description is None


def test_fetch_all_postings_paginates_until_total():
    source = _source()
    page1 = MagicMock()
    page1.raise_for_status = MagicMock()
    page1.json.return_value = {
        "totalFound": 2,
        "content": [_list_posting("1", "A")],
    }
    page2 = MagicMock()
    page2.raise_for_status = MagicMock()
    page2.json.return_value = {
        "totalFound": 2,
        "content": [_list_posting("2", "B")],
    }

    with patch("sources.api.smartrecruiters_source.PAGE_SIZE", 1), patch(
        "sources.api.smartrecruiters_source.requests.get",
        side_effect=[page1, page2],
    ) as mock_get:
        postings = source._fetch_all_postings("Visa", "Visa")

    assert [p["id"] for p in postings] == ["1", "2"]
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[0].kwargs["params"] == {"limit": 1, "offset": 0}
    assert mock_get.call_args_list[1].kwargs["params"] == {"limit": 1, "offset": 1}


def test_fetch_all_postings_raises_on_http_error():
    source = _source()
    with patch(
        "sources.api.smartrecruiters_source.requests.get",
        side_effect=requests.exceptions.HTTPError("404"),
    ):
        try:
            source._fetch_all_postings("missing", "Missing")
            raise AssertionError("expected HTTPError")
        except requests.exceptions.HTTPError:
            pass


def test_fetch_job_by_url_uses_detail():
    source = _source()
    with patch.object(
        source,
        "_fetch_detail",
        return_value=_detail("74983486", "SEO Manager", "Run SEO"),
    ) as mock_detail:
        job = source.fetch_job_by_url(
            "https://jobs.smartrecruiters.com/Visa/74983486-seo-manager"
        )

    mock_detail.assert_called_once_with("Visa", "74983486")
    assert job is not None
    assert job.job_title == "SEO Manager"
    assert "Run SEO" in (job.job_description or "")
