#!/usr/bin/env python3
"""
Rewrite stored Greenhouse URLs from boards-api JSON paths to public board URLs.

Converts e.g.
  https://boards-api.greenhouse.io/v1/boards/airbnb/jobs/123
to
  https://boards.greenhouse.io/airbnb/jobs/123

Dry-run by default; pass --execute to apply.

Typical use with port-forward to localhost:55432:

  export DB_HOST=127.0.0.1 DB_PORT=55432 DB_NAME=... DB_USER=... DB_PASSWORD=...
  python scripts/update_greenhouse_job_urls.py
  python scripts/update_greenhouse_job_urls.py --execute
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg

from sources.api.greenhouse_source import greenhouse_board_job_url, parse_greenhouse_board_job_url


def _to_board_url(raw: str | None, board_base: str) -> str | None:
    if not raw or not str(raw).strip():
        return None
    parsed = parse_greenhouse_board_job_url(str(raw).strip())
    if not parsed:
        return None
    board, job_id = parsed
    return greenhouse_board_job_url(board_base, board, job_id)


def _needs_board_rewrite(current: str, board_base: str) -> str | None:
    """Return canonical board URL if current differs; None if already that board URL or unparseable."""
    new_url = _to_board_url(current, board_base)
    if not new_url:
        return None
    if new_url.rstrip("/") == str(current).strip().rstrip("/"):
        return None
    return new_url


def _connect(args: argparse.Namespace):
    if args.database_url:
        return psycopg.connect(args.database_url.strip(), connect_timeout=30)

    host = args.host or os.getenv("DB_HOST")
    port_s = args.port if args.port is not None else os.getenv("DB_PORT")
    dbname = args.dbname or os.getenv("DB_NAME")
    user = args.dbuser or os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD") if args.dbpassword is None else args.dbpassword

    if host and port_s and dbname and user and password is not None:
        port = int(port_s)
        sslmode = os.getenv("PGSSLMODE", "prefer")
        return psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            connect_timeout=30,
        )

    from db import get_db_connection

    return get_db_connection()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply updates (default is dry-run only)",
    )
    parser.add_argument(
        "--board-base",
        default=os.getenv("GREENHOUSE_BOARD_BASE", "https://boards.greenhouse.io"),
        help="Public board host (must match job_sources public_boards_url)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Full PostgreSQL URL (otherwise uses DB_* / DATABASE_URL like db.get_db_connection)",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None)
    parser.add_argument("--dbname", default=None)
    parser.add_argument("--dbuser", default=None)
    parser.add_argument("--dbpassword", default=None)
    args = parser.parse_args()

    board_base = args.board_base.rstrip("/")

    conn = _connect(args)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, url::text, application_url::text
                FROM jobs
                WHERE url::text LIKE '%boards-api.greenhouse.io%'
                   OR COALESCE(application_url::text, '') LIKE '%boards-api.greenhouse.io%'
                ORDER BY id
                """
            )
            rows = cur.fetchall()

        updated = 0
        skipped_conflict = 0
        unchanged = 0

        for job_id, url, application_url in rows:
            nu = _needs_board_rewrite(url, board_base)
            new_url = nu if nu is not None else url

            new_app = application_url
            if application_url and str(application_url).strip() == str(url).strip():
                new_app = new_url
            elif application_url:
                af = _needs_board_rewrite(application_url, board_base)
                if af is not None:
                    new_app = af

            if new_url == url and (new_app or "") == (application_url or ""):
                unchanged += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM jobs WHERE url::text = %s AND id <> %s LIMIT 1",
                    (new_url, job_id),
                )
                if cur.fetchone():
                    print(f"SKIP id={job_id} conflict: target url already exists: {new_url}")
                    skipped_conflict += 1
                    continue

            print(f"id={job_id}\n  url: {url}\n  ->   {new_url}")
            if (application_url or "") != (new_app or ""):
                print(f"  application_url: {application_url}\n  ->   {new_app}")

            if args.execute:
                with conn.cursor() as cur:
                    if new_app is not None:
                        cur.execute(
                            "UPDATE jobs SET url = %s, application_url = %s WHERE id = %s",
                            (new_url, new_app, job_id),
                        )
                    else:
                        cur.execute(
                            "UPDATE jobs SET url = %s WHERE id = %s",
                            (new_url, job_id),
                        )
                conn.commit()
            updated += 1

        print()
        print(
            f"Summary: would update={updated}, unchanged={unchanged}, skipped_conflict={skipped_conflict}"
            + ("" if args.execute else " (dry-run; pass --execute to apply)")
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
