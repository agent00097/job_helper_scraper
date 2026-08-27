from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4

from models import JobData
from utils.job_storage import save_jobs
from utils.scrape_stats import ErrorBucket, classify_exception


def _job(url: str, title: str = "Eng") -> JobData:
    return JobData(
        url=url,
        job_title=title,
        company="Acme",
        location="Remote",
        job_description="Build things",
        date_posted=date(2026, 1, 1),
        source_website="lever",
        job_id_from_source="abc",
        company_id=uuid4(),
    )


def _cursor_for_existing(rows):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchall.side_effect = [rows, []]
    return conn, cur


@patch("utils.job_storage._extract_skills_safe")
@patch("utils.job_storage.db.get_db_connection")
def test_save_jobs_batch_lookup_skips_existing(get_conn, extract):
    conn, cur = _cursor_for_existing(
        [
            ("https://jobs.example/1", "id-1", False),
            ("https://jobs.example/2", "id-2", False),
        ]
    )
    get_conn.return_value = conn

    saved, dupes = save_jobs(
        [
            _job("https://jobs.example/1"),
            _job("https://jobs.example/2"),
        ]
    )

    assert saved == 0
    assert dupes == 2
    sql = cur.execute.call_args_list[0][0][0]
    assert "url = ANY(%s)" in sql
    assert "needs_description" in sql
    conn.commit.assert_called_once()
    conn.close.assert_called_once()
    extract.assert_not_called()


@patch("utils.job_storage._extract_skills_safe")
@patch("utils.job_storage.db.get_db_connection")
def test_save_jobs_inserts_missing_url_on_same_connection(get_conn, extract):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchall.side_effect = [[], []]
    cur.fetchone.return_value = ("new-id",)
    get_conn.return_value = conn

    saved, dupes = save_jobs([_job("https://jobs.example/new")])

    assert saved == 1
    assert dupes == 0
    insert_sql = cur.execute.call_args_list[-1][0][0]
    assert "INSERT INTO jobs" in insert_sql
    conn.commit.assert_called_once()
    extract.assert_called_once()


def test_psycopg_connection_timeout_is_storage_not_network():
    class ConnectionTimeout(Exception):
        pass

    ConnectionTimeout.__module__ = "psycopg.errors"
    ConnectionTimeout.__name__ = "ConnectionTimeout"

    bucket, _status = classify_exception(ConnectionTimeout("connection timeout expired"))
    assert bucket == ErrorBucket.STORAGE
