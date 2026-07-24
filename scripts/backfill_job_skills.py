#!/usr/bin/env python3
"""
Backfill job_skills for existing jobs.

Uses the same extractor as live scrape (alias + optional embeddings).

Usage (from job-helper-scraper, with .env DB_* / DATABASE_URL):

  # Safe first run: alias-only, jobs missing skills, limited
  python scripts/backfill_job_skills.py --alias-only --limit 100

  # Full alias-only backfill (recommended for large catalogs; no OpenAI cost)
  python scripts/backfill_job_skills.py --alias-only

  # Hybrid (needs OPENAI_API_KEY). Slow + API cost — start with --limit
  python scripts/backfill_job_skills.py --limit 50

  # Parallel hybrid (much faster) — see:
  #   python scripts/backfill_job_skills_parallel.py --help

  # Re-extract even if job_skills already exist
  python scripts/backfill_job_skills.py --alias-only --force

  # Resume after a given job id (exclusive)
  python scripts/backfill_job_skills.py --alias-only --after-id <uuid>
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from uuid import UUID

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv

load_dotenv(Path(_ROOT) / ".env")

import db
from services.skill_extraction_service import get_skill_extraction_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_job_skills")


def _count_targets(*, only_missing: bool, after_id: str | None) -> int:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            clauses = [
                "COALESCE(j.status, 'active') = 'active'",
                "j.job_description IS NOT NULL",
                "length(j.job_description) > 50",
            ]
            params: list = []
            if only_missing:
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM job_skills js WHERE js.job_id = j.id)"
                )
            if after_id:
                clauses.append("j.id > %s::uuid")
                params.append(after_id)
            sql = f"SELECT count(*) FROM jobs j WHERE {' AND '.join(clauses)}"
            cur.execute(sql, params)
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _fetch_batch(
    *,
    batch_size: int,
    after_id: str | None,
    only_missing: bool,
) -> list[tuple]:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            clauses = [
                "COALESCE(j.status, 'active') = 'active'",
                "j.job_description IS NOT NULL",
                "length(j.job_description) > 50",
            ]
            params: list = []
            if only_missing:
                clauses.append(
                    "NOT EXISTS (SELECT 1 FROM job_skills js WHERE js.job_id = j.id)"
                )
            if after_id:
                clauses.append("j.id > %s::uuid")
                params.append(after_id)
            params.append(batch_size)
            sql = f"""
                SELECT j.id, j.job_title, left(j.job_description, 20000)
                FROM jobs j
                WHERE {' AND '.join(clauses)}
                ORDER BY j.id
                LIMIT %s
            """
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill job_skills for existing jobs")
    parser.add_argument(
        "--alias-only",
        action="store_true",
        help="Disable embeddings even if OPENAI_API_KEY is set (recommended for full runs)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract jobs that already have job_skills rows",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max jobs to process (default: all matching)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Jobs per DB fetch batch (default 100)",
    )
    parser.add_argument(
        "--after-id",
        type=str,
        default=None,
        help="Resume: only jobs with id > this UUID",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between jobs (useful with embeddings)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Log progress every N jobs (default 50)",
    )
    args = parser.parse_args()

    if args.after_id:
        try:
            UUID(args.after_id)
        except ValueError as exc:
            raise SystemExit(f"Invalid --after-id: {exc}") from exc

    only_missing = not args.force
    if args.alias_only:
        os.environ["SKILL_EXTRACTION_EMBEDDINGS"] = "false"
    os.environ.setdefault("SKILL_EXTRACTION_ENABLED", "true")

    service = get_skill_extraction_service()
    if not service.enabled:
        raise SystemExit("SKILL_EXTRACTION_ENABLED is false — aborting")

    embeddings = service.embeddings_enabled and not args.alias_only
    mode = "alias + embeddings" if embeddings else "alias-only"
    if embeddings and args.sleep <= 0:
        # Soft default pacing when embeddings are on
        args.sleep = 0.05

    total = _count_targets(only_missing=only_missing, after_id=args.after_id)
    to_run = total if args.limit is None else min(total, args.limit)
    logger.info(
        "Backfill starting: mode=%s only_missing=%s candidates=%d will_process=%d "
        "batch_size=%d after_id=%s",
        mode,
        only_missing,
        total,
        to_run,
        args.batch_size,
        args.after_id,
    )
    if embeddings and (args.limit is None or args.limit > 500):
        logger.warning(
            "Embeddings backfill can be slow and costly. "
            "Prefer --alias-only for full catalog, or keep --limit small."
        )

    processed = 0
    succeeded = 0
    failed = 0
    skills_written = 0
    after_id = args.after_id
    t0 = time.time()

    while processed < to_run:
        batch_limit = min(args.batch_size, to_run - processed)
        rows = _fetch_batch(
            batch_size=batch_limit,
            after_id=after_id,
            only_missing=only_missing,
        )
        if not rows:
            break

        for job_id, title, description in rows:
            try:
                n = service.extract_and_save(job_id, title, description)
                succeeded += 1
                skills_written += n
            except Exception as exc:
                failed += 1
                logger.error("Failed job %s: %s", job_id, exc, exc_info=False)
            processed += 1
            after_id = str(job_id)

            if processed % args.progress_every == 0 or processed == to_run:
                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                logger.info(
                    "Progress %d/%d succeeded=%d failed=%d skills_written=%d "
                    "rate=%.1f jobs/s last_id=%s",
                    processed,
                    to_run,
                    succeeded,
                    failed,
                    skills_written,
                    rate,
                    after_id,
                )

            if args.sleep > 0:
                time.sleep(args.sleep)

            if processed >= to_run:
                break

    elapsed = time.time() - t0
    logger.info(
        "Backfill done in %.1fs: processed=%d succeeded=%d failed=%d "
        "skills_written=%d resume_after_id=%s",
        elapsed,
        processed,
        succeeded,
        failed,
        skills_written,
        after_id,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
