#!/usr/bin/env python3
"""
Load O*NET Software Skills CSV into skills + skill_aliases.

Dedupes on Workplace Example (canonical skill name). Category is the most
common Element Name for that example. Hot/In Demand flags are OR'd across rows.

Usage (from job-helper-scraper, with .env DATABASE_URL or DB_* pointing at DB):

  python scripts/load_onet_software_skills.py
  python scripts/load_onet_software_skills.py --csv ../software_skills.csv
  python scripts/load_onet_software_skills.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_db_connection

SOURCE = "onet_software_skills"
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def default_csv_path() -> Path:
    candidates = [
        Path(_ROOT).parent / "software_skills.csv",
        Path(_ROOT) / "software_skills.csv",
        Path.cwd() / "software_skills.csv",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def aggregate_skills(csv_path: Path) -> list[dict]:
    """
    Returns list of dicts:
      name, normalized_name, category, is_hot, is_in_demand
    """
    buckets: dict[str, dict] = {}

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"Workplace Example", "Element Name", "Hot Technology", "In Demand"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV missing columns: {sorted(missing)}")

        for row in reader:
            name = (row.get("Workplace Example") or "").strip()
            if not name:
                continue
            key = normalize(name)
            if not key:
                continue

            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    "name": name,
                    "normalized_name": key,
                    "categories": Counter(),
                    "is_hot": False,
                    "is_in_demand": False,
                }
                buckets[key] = bucket

            category = (row.get("Element Name") or "").strip()
            if category:
                bucket["categories"][category] += 1

            if (row.get("Hot Technology") or "").strip().upper() == "Y":
                bucket["is_hot"] = True
            if (row.get("In Demand") or "").strip().upper() == "Y":
                bucket["is_in_demand"] = True

    skills: list[dict] = []
    for bucket in buckets.values():
        category = None
        if bucket["categories"]:
            category = bucket["categories"].most_common(1)[0][0]
        skills.append(
            {
                "name": bucket["name"],
                "normalized_name": bucket["normalized_name"],
                "category": category,
                "is_hot": bucket["is_hot"],
                "is_in_demand": bucket["is_in_demand"],
            }
        )

    skills.sort(key=lambda s: s["normalized_name"])
    return skills


def upsert_skills(skills: list[dict]) -> tuple[int, int]:
    """Upsert all skills + seed alias=name via set-based SQL. Returns (skills_n, aliases_n)."""
    names = [s["name"] for s in skills]
    norms = [s["normalized_name"] for s in skills]
    categories = [s["category"] for s in skills]
    hots = [s["is_hot"] for s in skills]
    demands = [s["is_in_demand"] for s in skills]
    sources = [SOURCE] * len(skills)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            print(f"Upserting {len(skills)} skills ...", flush=True)
            cur.execute(
                """
                INSERT INTO skills (
                    name, normalized_name, category, source, is_hot, is_in_demand
                )
                SELECT *
                FROM unnest(
                    %s::text[],
                    %s::text[],
                    %s::text[],
                    %s::text[],
                    %s::boolean[],
                    %s::boolean[]
                ) AS t(name, normalized_name, category, source, is_hot, is_in_demand)
                ON CONFLICT (normalized_name) DO UPDATE SET
                    name = EXCLUDED.name,
                    category = COALESCE(EXCLUDED.category, skills.category),
                    is_hot = skills.is_hot OR EXCLUDED.is_hot,
                    is_in_demand = skills.is_in_demand OR EXCLUDED.is_in_demand,
                    updated_at = now()
                """,
                (names, norms, categories, sources, hots, demands),
            )
            skills_upserted = cur.rowcount

            print("Upserting aliases ...", flush=True)
            cur.execute(
                """
                INSERT INTO skill_aliases (skill_id, alias, normalized_alias)
                SELECT s.id, s.name, s.normalized_name
                FROM skills s
                WHERE s.source = %s
                ON CONFLICT (normalized_alias) DO UPDATE SET
                    skill_id = EXCLUDED.skill_id,
                    alias = EXCLUDED.alias
                """,
                (SOURCE,),
            )
            aliases_upserted = cur.rowcount

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return skills_upserted, aliases_upserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Load O*NET software skills into Postgres")
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Path to software_skills.csv (default: search workspace root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report counts without writing to the database",
    )
    args = parser.parse_args()

    csv_path = args.csv or default_csv_path()
    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    print(f"Reading {csv_path} ...")
    skills = aggregate_skills(csv_path)
    hot = sum(1 for s in skills if s["is_hot"])
    in_demand = sum(1 for s in skills if s["is_in_demand"])
    print(f"Aggregated {len(skills)} unique skills ({hot} hot, {in_demand} in-demand)")

    if args.dry_run:
        for sample in skills[:5]:
            print(f"  sample: {sample['name']!r} / {sample['category']!r}")
        print("Dry run — no DB writes.")
        return

    print("Writing to database ...")
    skills_n, aliases_n = upsert_skills(skills)
    print(f"Upserted {skills_n} skills, {aliases_n} aliases (source={SOURCE})")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM skills WHERE source = %s", (SOURCE,))
            print(f"skills rows with source={SOURCE}: {cur.fetchone()[0]}")
            cur.execute("SELECT count(*) FROM skill_aliases")
            print(f"skill_aliases total: {cur.fetchone()[0]}")
            cur.execute(
                """
                SELECT name, category, is_hot
                FROM skills
                WHERE normalized_name IN ('c++', 'python', 'kubernetes', 'microsoft excel')
                ORDER BY normalized_name
                """
            )
            print("spot checks:")
            for row in cur.fetchall():
                print(f"  {row}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
