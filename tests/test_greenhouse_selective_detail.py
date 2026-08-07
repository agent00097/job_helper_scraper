"""Unit tests for selective Greenhouse per-job detail fetching."""
from unittest.mock import MagicMock, patch

from sources.api.greenhouse_source import GreenhouseSource, _company_domain_hint
from utils.deduplication import urls_with_existing_description


def _source() -> GreenhouseSource:
    return GreenhouseSource(
        name="greenhouse",
        source_id="test-id",
        config={},
        rate_limit_per_minute=600,
    )


def _list_payload(*job_ids: int) -> dict:
    return {
        "jobs": [
            {
                "id": jid,
                "title": f"Role {jid}",
                "updated_at": "2026-01-01T00:00:00Z",
                "location": {"name": "Remote"},
                "metadata": [],
            }
            for jid in job_ids
        ]
    }


def test_company_domain_hint_uses_greenhouse_absolute_url():
    assert (
        _company_domain_hint(
            "https://careers.airbnb.com/positions/123?gh_jid=123"
        )
        == "airbnb.com"
    )
    assert (
        _company_domain_hint("https://boards.greenhouse.io/acme/jobs/123")
        is None
    )


def test_parse_job_carries_greenhouse_company_signals():
    source = _source()
    job = source._parse_job(
        {
            "id": 123,
            "title": "Engineer",
            "company_name": "Acme API Name",
            "absolute_url": "https://jobs.acme.example/openings/123",
            "metadata": [],
        },
        "Configured Name",
        "acme",
    )

    assert job.company == "Acme API Name"
    assert job.company_domain_hint == "acme.example"


def test_urls_with_existing_description_queries_nonempty_only():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [("https://boards.greenhouse.io/acme/jobs/1",)]

    with patch("utils.deduplication.db.get_db_connection", return_value=conn):
        result = urls_with_existing_description(
            [
                "https://boards.greenhouse.io/acme/jobs/1",
                "https://boards.greenhouse.io/acme/jobs/2",
                "  ",
            ]
        )

    assert result == {"https://boards.greenhouse.io/acme/jobs/1"}
    sql = cur.execute.call_args[0][0]
    assert "job_description IS NOT NULL" in sql
    assert "btrim(job_description)" in sql
    conn.close.assert_called_once()


def test_urls_with_existing_description_fails_open_on_db_error():
    with patch(
        "utils.deduplication.db.get_db_connection",
        side_effect=RuntimeError("db down"),
    ):
        assert urls_with_existing_description(["https://example.com/1"]) == set()


def test_fetch_jobs_skips_detail_when_description_already_stored():
    source = _source()
    list_resp = MagicMock()
    list_resp.raise_for_status = MagicMock()
    list_resp.json.return_value = _list_payload(101, 102)

    known = {"https://boards.greenhouse.io/airbnb/jobs/101"}

    with patch("sources.api.greenhouse_source.requests.get", return_value=list_resp) as mock_get, patch(
        "sources.api.greenhouse_source.urls_with_existing_description",
        return_value=known,
    ), patch.object(
        source,
        "_fetch_job_description",
        return_value="Full description for 102",
    ) as mock_detail:
        jobs = source.fetch_jobs("airbnb", "Airbnb")

    assert len(jobs) == 2
    by_id = {j.job_id_from_source: j for j in jobs}
    assert by_id["101"].job_description is None
    assert by_id["102"].job_description == "Full description for 102"
    mock_detail.assert_called_once_with("airbnb", 102)
    # Board list only — no direct detail GETs via requests.get
    assert mock_get.call_count == 1


def test_fetch_jobs_details_all_when_none_described():
    source = _source()
    list_resp = MagicMock()
    list_resp.raise_for_status = MagicMock()
    list_resp.json.return_value = _list_payload(201, 202)

    with patch("sources.api.greenhouse_source.requests.get", return_value=list_resp), patch(
        "sources.api.greenhouse_source.urls_with_existing_description",
        return_value=set(),
    ), patch.object(
        source,
        "_fetch_job_description",
        side_effect=lambda _ep, jid: f"desc-{jid}",
    ) as mock_detail:
        jobs = source.fetch_jobs("airbnb", "Airbnb")

    assert len(jobs) == 2
    assert {j.job_description for j in jobs} == {"desc-201", "desc-202"}
    assert mock_detail.call_count == 2
