#!/usr/bin/env python3
"""
Backfill jobs.admin1_code / locality / geo_precision from free-text location.

Usage (from job-helper-scraper root, with DB_* env set):
    python scripts/backfill_jobs_geo.py
    python scripts/backfill_jobs_geo.py --limit 1000 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from utils.geo import parse_location

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_jobs_geo")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="Max rows (0 = all)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-parse even when geo_precision is already set",
    )
    args = parser.parse_args()

    where = "location IS NOT NULL AND btrim(location) <> ''"
    if not args.force:
        where += " AND (geo_precision IS NULL OR geo_precision = 'unknown' OR admin1_code IS NULL)"

    conn = db.get_db_connection()
    updated = 0
    scanned = 0
    try:
        with conn.cursor() as cur:
            sql = f"SELECT id, location FROM jobs WHERE {where} ORDER BY created_at DESC"
            if args.limit > 0:
                sql += f" LIMIT {int(args.limit)}"
            cur.execute(sql)
            rows = cur.fetchall()

        logger.info("Loaded %d candidate rows", len(rows))
        batch: list[tuple] = []
        for job_id, location in rows:
            scanned += 1
            parts = parse_location(location)
            batch.append(
                (
                    parts.country_code,
                    parts.admin1_code,
                    parts.admin1_name,
                    parts.locality,
                    parts.geo_precision,
                    job_id,
                )
            )
            if len(batch) >= args.batch_size:
                updated += _flush(conn, batch, dry_run=args.dry_run)
                batch = []
                logger.info("Progress scanned=%d updated=%d", scanned, updated)
        if batch:
            updated += _flush(conn, batch, dry_run=args.dry_run)
    finally:
        conn.close()

    logger.info("Done scanned=%d updated=%d dry_run=%s", scanned, updated, args.dry_run)
    return 0


def _flush(conn, batch: list[tuple], *, dry_run: bool) -> int:
    if dry_run:
        return len(batch)
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE jobs SET
                country_code = COALESCE(%s, country_code),
                admin1_code = %s,
                admin1_name = %s,
                locality = %s,
                geo_precision = %s
            WHERE id = %s
            """,
            batch,
        )
    conn.commit()
    return len(batch)


if __name__ == "__main__":
    raise SystemExit(main())
