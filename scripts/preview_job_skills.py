#!/usr/bin/env python3
"""
Sample random jobs and print hybrid skill extraction (alias + embeddings).

Does NOT write to job_skills — verification only.

Usage (from job-helper-scraper):

  python scripts/preview_job_skills.py
  python scripts/preview_job_skills.py --limit 10 --seed 42
  python scripts/preview_job_skills.py --alias-only
  python scripts/preview_job_skills.py --rebuild-embeddings

Requires DATABASE_URL / DB_*. Embeddings need OPENAI_API_KEY (or
SKILL_EMBEDDING_API_KEY). Without a key, runs alias-only automatically.
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv

load_dotenv(Path(_ROOT) / ".env")

import db
from utils.skills.alias_matcher import AliasMatcher
from utils.skills.catalog import load_skill_catalog
from utils.skills.embeddings import SkillEmbeddingIndex
from utils.skills.extract import extract_skills


def _has_embed_key() -> bool:
    return bool(
        (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("SKILL_EMBEDDING_API_KEY")
            or ""
        ).strip()
    )


def _fetch_job(job_id: str):
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, job_title, company, left(job_description, 20000) AS job_description,
                       source_website, country_code, role_function
                FROM jobs
                WHERE id = %s::uuid
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return []
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row))]
    finally:
        conn.close()


def _fetch_random_jobs(limit: int, seed: int | None):
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            if seed is not None:
                # Deterministic sample via md5(id || seed)
                cur.execute(
                    """
                    SELECT id, job_title, company, left(job_description, 20000) AS job_description,
                           source_website, country_code, role_function
                    FROM jobs
                    WHERE COALESCE(status, 'active') = 'active'
                      AND job_description IS NOT NULL
                      AND length(job_description) > 200
                    ORDER BY md5(id::text || %s)
                    LIMIT %s
                    """,
                    (str(seed), limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, job_title, company, left(job_description, 20000) AS job_description,
                           source_website, country_code, role_function
                    FROM jobs
                    WHERE COALESCE(status, 'active') = 'active'
                      AND job_description IS NOT NULL
                      AND length(job_description) > 200
                    ORDER BY random()
                    LIMIT %s
                    """,
                    (limit,),
                )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _print_job(job: dict, hits, idx: int) -> None:
    title = job.get("job_title") or "(no title)"
    company = job.get("company") or "?"
    print("=" * 72)
    print(f"[{idx}] {title}")
    print(f"     company: {company}")
    print(f"     id: {job['id']}")
    print(
        f"     source={job.get('source_website')}  "
        f"country={job.get('country_code')}  role_function={job.get('role_function')}"
    )
    desc = (job.get("job_description") or "").replace("\n", " ")
    print("     desc:", textwrap.shorten(desc, width=160, placeholder="..."))
    if not hits:
        print("     skills: (none)")
        return
    print(f"     skills ({len(hits)}):")
    for h in hits:
        extra = []
        if h.source:
            extra.append(f"src={h.source}")
        if h.method == "alias" and h.matched_alias:
            extra.append(f"alias={h.matched_alias!r}")
        if h.method == "embedding":
            if h.cosine is not None:
                extra.append(f"cos={h.cosine:.3f}")
            if h.phrase:
                extra.append(f"phrase={h.phrase!r}")
        extra_s = (", ".join(extra)) if extra else ""
        print(
            f"       - {h.skill_name:40s}  "
            f"w={h.weight:.2f}  [{h.method}]  {extra_s}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview skill extraction on random jobs")
    parser.add_argument("--limit", type=int, default=10, help="Number of jobs (default 10)")
    parser.add_argument("--seed", type=int, default=None, help="Deterministic sample seed")
    parser.add_argument(
        "--alias-only",
        action="store_true",
        help="Skip embeddings even if API key is present",
    )
    parser.add_argument(
        "--rebuild-embeddings",
        action="store_true",
        help="Force rebuild of skill embedding cache",
    )
    parser.add_argument(
        "--embed-min-cosine",
        type=float,
        default=0.60,
        help="Min cosine for phrase→skill embedding hits (default 0.60)",
    )
    parser.add_argument(
        "--embed-top-k",
        type=int,
        default=10,
        help="Max embedding hits per job (default 10)",
    )
    parser.add_argument(
        "--job-id",
        type=str,
        default=None,
        help="Preview a specific job UUID instead of a random sample",
    )
    args = parser.parse_args()

    print("Loading skill catalog ...")
    catalog = load_skill_catalog()
    print(f"  skills={len(catalog.skills)}  aliases={len(catalog.aliases)}")
    matcher = AliasMatcher(catalog)

    embedding_index = None
    use_embeddings = (not args.alias_only) and _has_embed_key()
    if args.alias_only:
        print("Mode: alias-only (--alias-only)")
    elif not _has_embed_key():
        print("Mode: alias-only (no OPENAI_API_KEY / SKILL_EMBEDDING_API_KEY)")
    else:
        print("Mode: alias + embeddings")
        print("Loading / building skill embedding index (cached under data/) ...")
        embedding_index = SkillEmbeddingIndex.build(
            catalog,
            force_rebuild=args.rebuild_embeddings,
        )
        print(f"  embedding vectors: {len(embedding_index.skill_ids)}")

    if args.job_id:
        print(f"\nLoading job {args.job_id} ...")
        jobs = _fetch_job(args.job_id)
    else:
        print(f"\nSampling {args.limit} jobs ...")
        jobs = _fetch_random_jobs(args.limit, args.seed)
    if not jobs:
        raise SystemExit("No jobs found with descriptions.")

    for i, job in enumerate(jobs, 1):
        hits = extract_skills(
            job.get("job_title") or "",
            job.get("job_description") or "",
            catalog=catalog,
            alias_matcher=matcher,
            embedding_index=embedding_index if use_embeddings else None,
            embed_top_k=args.embed_top_k,
            embed_min_cosine=args.embed_min_cosine,
        )
        _print_job(job, hits, i)

    print("=" * 72)
    print("Done. Nothing was written to job_skills.")


if __name__ == "__main__":
    main()
