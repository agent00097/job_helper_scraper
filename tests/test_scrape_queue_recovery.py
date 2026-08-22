"""Tests for queue scrape_run finalize + stale abandon."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import utils.scrape_stats as scrape_stats


def _conn_with_rows(running_rows, count_row):
    """Build a fake DB connection for finalize_completed_queue_runs."""
    conn = MagicMock()
    select_cur = MagicMock()
    select_cur.fetchall.return_value = running_rows
    count_cur = MagicMock()
    count_cur.fetchone.return_value = count_row
    update_cur = MagicMock()
    update_cur.rowcount = 1

    # select running → counts → close (scrape_runs + job_sources on same cursor)
    cursors = [select_cur, count_cur, update_cur]

    def cursor_cm():
        cm = MagicMock()
        cm.__enter__.return_value = cursors.pop(0) if cursors else MagicMock()
        cm.__exit__.return_value = False
        return cm

    conn.cursor.side_effect = cursor_cm
    return conn


@patch.dict(
    "os.environ",
    {"SCRAPE_QUEUE_STALE_AFTER_HOURS": "4", "SCRAPE_QUEUE_STALL_MINUTES": "60"},
    clear=False,
)
@patch("utils.scrape_stats.db.get_db_connection")
def test_finalize_completes_when_done_reaches_queued(get_conn):
    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    source_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    started = datetime.utcnow() - timedelta(hours=1)
    running = [
        (run_id, source_id, "ashby", '{"mode":"queue","queued":2}', started),
    ]
    # done=2, ok=2, failed=0, fetched/saved/dupes/archived, last_finished
    counts = (2, 2, 0, 10, 5, 5, 0, datetime.utcnow())
    get_conn.return_value = _conn_with_rows(running, counts)

    assert scrape_stats.finalize_completed_queue_runs() == 1


@patch.dict(
    "os.environ",
    {"SCRAPE_QUEUE_STALE_AFTER_HOURS": "4", "SCRAPE_QUEUE_STALL_MINUTES": "60"},
    clear=False,
)
@patch("utils.scrape_stats.db.get_db_connection")
def test_finalize_abandons_stale_incomplete_run(get_conn):
    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    source_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    started = datetime.utcnow() - timedelta(hours=6)
    last_finished = datetime.utcnow() - timedelta(hours=2)
    running = [
        (run_id, source_id, "ashby", '{"mode":"queue","queued":954}', started),
    ]
    # 938/954 — stalled
    counts = (938, 900, 38, 1000, 500, 500, 0, last_finished)
    get_conn.return_value = _conn_with_rows(running, counts)

    assert scrape_stats.finalize_completed_queue_runs() == 1


@patch.dict(
    "os.environ",
    {"SCRAPE_QUEUE_STALE_AFTER_HOURS": "4", "SCRAPE_QUEUE_STALL_MINUTES": "60"},
    clear=False,
)
@patch("utils.scrape_stats.db.get_db_connection")
def test_finalize_does_not_abandon_fresh_drain(get_conn):
    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    source_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    started = datetime.utcnow() - timedelta(hours=1)
    last_finished = datetime.utcnow() - timedelta(minutes=5)
    running = [
        (run_id, source_id, "ashby", '{"mode":"queue","queued":954}', started),
    ]
    counts = (500, 480, 20, 100, 50, 50, 0, last_finished)
    conn = _conn_with_rows(running, counts)
    get_conn.return_value = conn

    assert scrape_stats.finalize_completed_queue_runs() == 0
    # Only the SELECT + counts cursors should have been used (no close UPDATE)
    assert conn.commit.call_count == 0


@patch("services.company_scrape._scrape_one_company_impl")
@patch.dict("os.environ", {"COMPANY_SCRAPE_TIMEOUT_SECONDS": "1"}, clear=False)
def test_company_scrape_timeout(impl):
    import time

    from services.company_scrape import scrape_one_company

    def slow(*_a, **_k):
        time.sleep(3)
        return MagicMock(ok=True)

    impl.side_effect = slow
    source = MagicMock()
    source.name = "ashby"
    company = {
        "id": "11111111-1111-1111-1111-111111111111",
        "company_name": "SlowCo",
        "normalized_name": "slowco",
        "company_endpoint": "slowco",
    }
    outcome = scrape_one_company(source, company)
    assert outcome.ok is False
    assert isinstance(outcome.error, TimeoutError)
