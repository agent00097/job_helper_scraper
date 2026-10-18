"""get_source_companies: health-filtered query with fail-open fallback."""
from unittest.mock import MagicMock, patch

from utils import source_loader

ROW = ("11111111-1111-1111-1111-111111111111", "Acme", "acme", "acme", None, None)


def _conn_with_cursors(*cursors):
    conn = MagicMock()
    conn.cursor.side_effect = [MagicMock(__enter__=MagicMock(return_value=c), __exit__=MagicMock(return_value=False)) for c in cursors]
    return conn


def test_uses_health_filtered_query_and_maps_rows():
    filtered = MagicMock()
    filtered.fetchall.return_value = [ROW]
    count = MagicMock()
    count.fetchone.return_value = (5,)
    conn = _conn_with_cursors(filtered, count)

    with patch("utils.source_loader.db.get_db_connection", return_value=conn):
        companies = source_loader.get_source_companies("greenhouse")

    assert companies == [{
        "id": ROW[0], "company_name": "Acme", "normalized_name": "acme",
        "company_endpoint": "acme", "logo_url": None, "domain": None,
    }]
    sql, params = filtered.execute.call_args[0]
    assert "scrape_company_results" in sql
    assert params["source_name"] == "greenhouse"
    assert params["dead_n"] == 3 and params["empty_n"] == 4
    conn.close.assert_called_once()


def test_falls_back_to_plain_query_when_health_query_fails():
    broken = MagicMock()
    broken.execute.side_effect = RuntimeError("relation does not exist")
    plain = MagicMock()
    plain.fetchall.return_value = [ROW]
    conn = _conn_with_cursors(broken, plain)

    with patch("utils.source_loader.db.get_db_connection", return_value=conn):
        companies = source_loader.get_source_companies("greenhouse")

    assert [c["company_name"] for c in companies] == ["Acme"]
    conn.rollback.assert_called_once()
    assert "scrape_company_results" not in plain.execute.call_args[0][0]


def test_thresholds_are_env_configurable(monkeypatch):
    monkeypatch.setenv("SCRAPE_SKIP_DEAD_AFTER", "0")
    monkeypatch.setenv("SCRAPE_SKIP_EMPTY_AFTER", "bogus")
    filtered = MagicMock()
    filtered.fetchall.return_value = []
    count = MagicMock()
    count.fetchone.return_value = (0,)
    conn = _conn_with_cursors(filtered, count)

    with patch("utils.source_loader.db.get_db_connection", return_value=conn):
        source_loader.get_source_companies("lever")

    params = filtered.execute.call_args[0][1]
    assert params["dead_n"] == 0
    assert params["empty_n"] == 4  # invalid value falls back to default
