"""Pooled connections should not be returned INTRANS."""
from unittest.mock import MagicMock

from db import _PooledConnection


def test_pooled_connection_rollback_before_putconn():
    conn = MagicMock()
    conn.closed = False
    pool = MagicMock()
    wrapped = _PooledConnection(conn, pool)

    wrapped.close()

    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn)

    wrapped.close()
    assert pool.putconn.call_count == 1
    assert conn.rollback.call_count == 1
