#!/usr/bin/env python3
"""
Parallel hybrid (alias + embeddings) backfill for job_skills.

Shards work across processes with hash(job_id) % workers so workers never
collide. Each worker loads its own catalog + embedding index (shared disk cache).

Usage (from job-helper-scraper, with .env + OPENAI_API_KEY):

  # Smoke test
  python scripts/backfill_job_skills_parallel.py --workers 4 --limit 100

  # Jobs with no skills yet
  python scripts/backfill_job_skills_parallel.py --workers 8

  # After an alias-only pass: re-run hybrid for jobs that still lack embedding hits
  python scripts/backfill_job_skills_parallel.py --workers 8 --upgrade-from-alias

  # Force re-extract everything (expensive)
  python scripts/backfill_job_skills_parallel.py --workers 8 --force

Tips:
  - Start with --workers 4; raise toward 8–16 if OpenAI rate limits allow.
  - Embedding index is cached at data/skill_embeddings.bin (built once).
  - Alias-only full catalog is still faster via scripts/backfill_job_skills.py --alias-only
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from multiprocessing import Process, Queue, Value
from pathlib import Path
from typing import Literal, Optional
from uuid import UUID

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv

load_dotenv(Path(_ROOT) / ".env")

import db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_job_skills_parallel")

TargetMode = Literal["missing", "force", "upgrade_from_alias"]


def _target_clause(mode: TargetMode) -> str:
    if mode == "force":
        return "TRUE"
    if mode == "upgrade_from_alias":
        # No skills yet, OR only alias/other methods (no embedding rows).
        return """
        (
          NOT EXISTS (SELECT 1 FROM job_skills js WHERE js.job_id = j.id)
          OR NOT EXISTS (
            SELECT 1 FROM job_skills js
            WHERE js.job_id = j.id AND js.method = 'embedding'
          )
        )
        """
    return "NOT EXISTS (SELECT 1 FROM job_skills js WHERE js.job_id = j.id)"


def _base_clauses(mode: TargetMode, after_id: str | None) -> tuple[list[str], list]:
    clauses = [
        "COALESCE(j.status, 'active') = 'active'",
        "j.job_description IS NOT NULL",
        "length(j.job_description) > 50",
        _target_clause(mode),
    ]
    params: list = []
    if after_id:
        clauses.append("j.id > %s::uuid")
        params.append(after_id)
    return clauses, params


def _count_targets(
    *,
    mode: TargetMode,
    after_id: str | None,
    workers: int,
    worker_id: int | None = None,
) -> int:
    clauses, params = _base_clauses(mode, after_id)
    if worker_id is not None:
        clauses.append("mod(abs(hashtext(j.id::text)), %s) = %s")
        params.extend([workers, worker_id])
    sql = f"SELECT count(*) FROM jobs j WHERE {' AND '.join(clauses)}"
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _fetch_batch(
    *,
    batch_size: int,
    after_id: str | None,
    mode: TargetMode,
    workers: int,
    worker_id: int,
) -> list[tuple]:
    clauses, params = _base_clauses(mode, after_id)
    clauses.append("mod(abs(hashtext(j.id::text)), %s) = %s")
    params.extend([workers, worker_id, batch_size])
    sql = f"""
        SELECT j.id, j.job_title, left(j.job_description, 20000)
        FROM jobs j
        WHERE {' AND '.join(clauses)}
        ORDER BY j.id
        LIMIT %s
    """
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


@dataclass
class WorkerStats:
    worker_id: int
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skills_written: int = 0
    last_id: str | None = None
    done: bool = False
    error: str | None = None


def _worker_main(
    worker_id: int,
    workers: int,
    mode: TargetMode,
    after_id: str | None,
    batch_size: int,
    sleep_s: float,
    progress_every: int,
    global_limit: int | None,
    shared_processed: Value,
    event_q: Queue,
) -> None:
    # Fresh logging + service per process (spawn-safe).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    wlog = logging.getLogger(f"backfill_job_skills_parallel.w{worker_id}")

    os.environ.setdefault("SKILL_EXTRACTION_ENABLED", "true")
    os.environ["SKILL_EXTRACTION_EMBEDDINGS"] = "true"

    from services.skill_extraction_service import SkillExtractionService

    # Avoid sharing the module singleton across workers; construct directly.
    service = SkillExtractionService()
    if not service.enabled:
        event_q.put(
            WorkerStats(
                worker_id=worker_id,
                done=True,
                error="SKILL_EXTRACTION_ENABLED is false",
            )
        )
        return
    if not service.embeddings_enabled:
        event_q.put(
            WorkerStats(
                worker_id=worker_id,
                done=True,
                error="Embeddings unavailable (set OPENAI_API_KEY)",
            )
        )
        return

    # Warm catalog + embedding index once per worker.
    try:
        service._ensure_alias_ready()
        index = service._ensure_embeddings_ready()
        if index is None:
            event_q.put(
                WorkerStats(
                    worker_id=worker_id,
                    done=True,
                    error="Failed to load/build embedding index",
                )
            )
            return
        wlog.info(
            "Worker %d/%d ready (index=%d vectors) mode=%s",
            worker_id,
            workers,
            len(index.skill_ids),
            mode,
        )
    except Exception as exc:
        event_q.put(
            WorkerStats(worker_id=worker_id, done=True, error=f"init failed: {exc}")
        )
        return

    stats = WorkerStats(worker_id=worker_id)
    cursor_after = after_id
    t0 = time.time()

    try:
        while True:
            if global_limit is not None and shared_processed.value >= global_limit:
                break

            rows = _fetch_batch(
                batch_size=batch_size,
                after_id=cursor_after,
                mode=mode,
                workers=workers,
                worker_id=worker_id,
            )
            if not rows:
                break

            for job_id, title, description in rows:
                if global_limit is not None:
                    with shared_processed.get_lock():
                        if shared_processed.value >= global_limit:
                            break
                        shared_processed.value += 1

                try:
                    n = service.extract_and_save(job_id, title, description)
                    stats.succeeded += 1
                    stats.skills_written += n
                except Exception as exc:
                    stats.failed += 1
                    wlog.error("Failed job %s: %s", job_id, exc, exc_info=False)

                stats.processed += 1
                stats.last_id = str(job_id)
                cursor_after = str(job_id)

                if stats.processed % progress_every == 0:
                    elapsed = time.time() - t0
                    rate = stats.processed / elapsed if elapsed > 0 else 0.0
                    wlog.info(
                        "w%d Progress local=%d succeeded=%d failed=%d "
                        "skills=%d rate=%.2f jobs/s last_id=%s",
                        worker_id,
                        stats.processed,
                        stats.succeeded,
                        stats.failed,
                        stats.skills_written,
                        rate,
                        stats.last_id,
                    )
                    event_q.put(
                        WorkerStats(
                            worker_id=worker_id,
                            processed=stats.processed,
                            succeeded=stats.succeeded,
                            failed=stats.failed,
                            skills_written=stats.skills_written,
                            last_id=stats.last_id,
                        )
                    )

                if sleep_s > 0:
                    time.sleep(sleep_s)

            else:
                continue
            break  # broke from inner loop due to global_limit
    except Exception as exc:
        stats.error = str(exc)
        wlog.exception("Worker %d crashed: %s", worker_id, exc)

    stats.done = True
    event_q.put(stats)
    wlog.info(
        "Worker %d done: processed=%d succeeded=%d failed=%d skills=%d last_id=%s",
        worker_id,
        stats.processed,
        stats.succeeded,
        stats.failed,
        stats.skills_written,
        stats.last_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parallel hybrid backfill of job_skills"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel worker processes (default 4)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract all matching jobs (even if job_skills exist)",
    )
    parser.add_argument(
        "--upgrade-from-alias",
        action="store_true",
        help="Target jobs with no skills OR no embedding-method skills "
        "(for post alias-only hybrid upgrade)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Global max jobs across all workers (default: all)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Jobs per DB fetch per worker (default 50)",
    )
    parser.add_argument(
        "--after-id",
        type=str,
        default=None,
        help="Only jobs with id > this UUID",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between jobs per worker (rate-limit helper)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Per-worker progress log every N jobs (default 25)",
    )
    args = parser.parse_args()

    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    if args.force and args.upgrade_from_alias:
        raise SystemExit("Use only one of --force or --upgrade-from-alias")
    if args.after_id:
        try:
            UUID(args.after_id)
        except ValueError as exc:
            raise SystemExit(f"Invalid --after-id: {exc}") from exc

    if not (
        os.environ.get("OPENAI_API_KEY") or os.environ.get("SKILL_EMBEDDING_API_KEY")
    ):
        raise SystemExit(
            "OPENAI_API_KEY (or SKILL_EMBEDDING_API_KEY) is required for hybrid backfill"
        )

    if args.force:
        mode: TargetMode = "force"
    elif args.upgrade_from_alias:
        mode = "upgrade_from_alias"
    else:
        mode = "missing"

    total = _count_targets(mode=mode, after_id=args.after_id, workers=args.workers)
    to_run = total if args.limit is None else min(total, args.limit)
    logger.info(
        "Parallel hybrid backfill: workers=%d mode=%s candidates=%d "
        "will_process<=%d batch_size=%d after_id=%s sleep=%s",
        args.workers,
        mode,
        total,
        to_run,
        args.batch_size,
        args.after_id,
        args.sleep,
    )
    if args.limit is None and mode == "force":
        logger.warning(
            "Full-force hybrid on the whole catalog is slow/costly. "
            "Prefer --upgrade-from-alias after alias-only, or set --limit."
        )

    # Rough per-worker counts for visibility
    for wid in range(args.workers):
        n = _count_targets(
            mode=mode,
            after_id=args.after_id,
            workers=args.workers,
            worker_id=wid,
        )
        logger.info("  shard w%d ≈ %d jobs", wid, n)

    event_q: Queue = Queue()
    shared_processed = Value("i", 0)
    procs: list[Process] = []
    t0 = time.time()

    for wid in range(args.workers):
        p = Process(
            target=_worker_main,
            kwargs={
                "worker_id": wid,
                "workers": args.workers,
                "mode": mode,
                "after_id": args.after_id,
                "batch_size": args.batch_size,
                "sleep_s": args.sleep,
                "progress_every": args.progress_every,
                "global_limit": args.limit,
                "shared_processed": shared_processed,
                "event_q": event_q,
            },
            name=f"skill-backfill-w{wid}",
            daemon=False,
        )
        procs.append(p)
        p.start()

    latest: dict[int, WorkerStats] = {}
    finished = 0
    fatal: list[str] = []

    try:
        while finished < args.workers:
            stats: WorkerStats = event_q.get()
            latest[stats.worker_id] = stats
            if stats.error and stats.done:
                fatal.append(f"w{stats.worker_id}: {stats.error}")
                logger.error("Worker %d error: %s", stats.worker_id, stats.error)
            if stats.done:
                finished += 1
                logger.info(
                    "Worker %d finished (%d/%d workers)",
                    stats.worker_id,
                    finished,
                    args.workers,
                )
                continue

            # Aggregate snapshot
            processed = sum(s.processed for s in latest.values())
            succeeded = sum(s.succeeded for s in latest.values())
            failed = sum(s.failed for s in latest.values())
            skills = sum(s.skills_written for s in latest.values())
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0.0
            logger.info(
                "TOTAL Progress %d/%d succeeded=%d failed=%d skills_written=%d "
                "rate=%.2f jobs/s workers_alive=%d",
                processed,
                to_run,
                succeeded,
                failed,
                skills,
                rate,
                sum(1 for p in procs if p.is_alive()),
            )
    except KeyboardInterrupt:
        logger.warning("Interrupted — terminating workers ...")
        for p in procs:
            if p.is_alive():
                p.terminate()

    for p in procs:
        p.join(timeout=30)

    processed = sum(s.processed for s in latest.values())
    succeeded = sum(s.succeeded for s in latest.values())
    failed = sum(s.failed for s in latest.values())
    skills = sum(s.skills_written for s in latest.values())
    elapsed = time.time() - t0
    rate = processed / elapsed if elapsed > 0 else 0.0
    logger.info(
        "Parallel backfill done in %.1fs: processed=%d succeeded=%d failed=%d "
        "skills_written=%d rate=%.2f jobs/s",
        elapsed,
        processed,
        succeeded,
        failed,
        skills,
        rate,
    )
    if fatal:
        logger.error("Fatal worker errors: %s", "; ".join(fatal))
        sys.exit(2)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    # Required on some platforms; also avoids forking messy DB/OpenAI state.
    from multiprocessing import set_start_method

    try:
        set_start_method("spawn")
    except RuntimeError:
        pass
    main()
