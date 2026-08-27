"""Unit tests for presence-based job archival helpers."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from utils.job_archive import (
    archive_jobs_missing_from_fetch,
    seen_keys_from_jobs,
    supports_presence_reconcile,
)


def test_supports_presence_reconcile_ats_only():
    assert supports_presence_reconcile("greenhouse")
    assert supports_presence_reconcile("Ashby")
    assert supports_presence_reconcile("lever")
    assert supports_presence_reconcile("workday")
    assert supports_presence_reconcile("smartrecruiters")
    assert not supports_presence_reconcile("jobbank")
    assert not supports_presence_reconcile("")


def test_seen_keys_from_jobs():
    jobs = [
        SimpleNamespace(job_id_from_source="1", url="https://example.com/1"),
        SimpleNamespace(job_id_from_source=None, url="https://example.com/2"),
        SimpleNamespace(job_id_from_source=" 3 ", url="  "),
    ]
    ids, urls = seen_keys_from_jobs(jobs)
    assert ids == {"1", "3"}
    assert urls == {"https://example.com/1", "https://example.com/2"}


def test_archive_jobs_missing_from_fetch_updates_and_clears_matches():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",)]
    cur.rowcount = 1

    with patch("utils.job_archive.db.get_db_connection", return_value=conn):
        n = archive_jobs_missing_from_fetch(
            source_website="greenhouse",
            company_name="Acme",
            seen_source_ids={"keep-1"},
            seen_urls={"https://boards.greenhouse.io/acme/jobs/1"},
        )

    assert n == 1
    assert cur.execute.call_count == 3
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_archive_skips_on_empty_company():
    with patch("utils.job_archive.db.get_db_connection") as mock_db:
        n = archive_jobs_missing_from_fetch(
            source_website="greenhouse",
            company_name="  ",
            seen_source_ids=["1"],
            seen_urls=[],
        )
    assert n == 0
    mock_db.assert_not_called()
