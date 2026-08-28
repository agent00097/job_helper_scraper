#!/usr/bin/env python3
"""
Upsert a small curated skill list (modern JD tools O*NET often misses).

Attaches aliases to an existing skills row when the name or any alias already
exists, so we do not duplicate O*NET entries. New rows get source=curated.

Usage (from job-helper-scraper, with DATABASE_URL / DB_*):

  python scripts/load_curated_skills.py
  python scripts/load_curated_skills.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_db_connection
from utils.skills.curated import CURATED_SKILLS

SOURCE = "curated"
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def _find_existing_skill_id(cur, norms: list[str]) -> str | None:
    cur.execute(
        """
        SELECT id
        FROM skills
        WHERE normalized_name = ANY(%s)
        LIMIT 1
        """,
        (norms,),
    )
    row = cur.fetchone()
    if row:
        return str(row[0])
    cur.execute(
        """
        SELECT skill_id
        FROM skill_aliases
        WHERE normalized_alias = ANY(%s)
        LIMIT 1
        """,
        (norms,),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def upsert_curated(*, dry_run: bool) -> tuple[int, int, int]:
    """
    Returns (new_skills, existing_attached, aliases_inserted).
    """
    new_skills = 0
    attached = 0
    aliases_inserted = 0

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for item in CURATED_SKILLS:
                name = item["name"].strip()
                aliases = [name, *item["aliases"]]
                seen: set[str] = set()
                unique_aliases: list[str] = []
                norms: list[str] = []
                for alias in aliases:
                    a = alias.strip()
                    key = normalize(a)
                    if not a or not key or key in seen:
                        continue
                    seen.add(key)
                    unique_aliases.append(a)
                    norms.append(key)

                existing_id = _find_existing_skill_id(cur, norms)
                if existing_id:
                    skill_id = existing_id
                    attached += 1
                else:
                    if dry_run:
                        new_skills += 1
                        aliases_inserted += len(unique_aliases)
                        continue
                    cur.execute(
                        """
                        INSERT INTO skills (
                            name, normalized_name, category, source,
                            is_hot, is_in_demand
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (normalized_name) DO UPDATE SET
                            is_hot = skills.is_hot OR EXCLUDED.is_hot,
                            is_in_demand = skills.is_in_demand OR EXCLUDED.is_in_demand,
                            updated_at = now()
                        RETURNING id
                        """,
                        (
                            name,
                            normalize(name),
                            item["category"],
                            SOURCE,
                            item["is_hot"],
                            item["is_in_demand"],
                        ),
                    )
                    row = cur.fetchone()
                    if not row:
                        continue
                    skill_id = str(row[0])
                    new_skills += 1

                if dry_run:
                    aliases_inserted += len(unique_aliases)
                    continue

                for alias, norm in zip(unique_aliases, norms):
                    cur.execute(
                        """
                        INSERT INTO skill_aliases (skill_id, alias, normalized_alias)
                        VALUES (%s::uuid, %s, %s)
                        ON CONFLICT (normalized_alias) DO NOTHING
                        """,
                        (skill_id, alias, norm),
                    )
                    aliases_inserted += cur.rowcount

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return new_skills, attached, aliases_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Load curated skills into Postgres")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve matches and report counts without writing",
    )
    args = parser.parse_args()

    print(f"Curated skills: {len(CURATED_SKILLS)}")
    new_skills, attached, aliases_n = upsert_curated(dry_run=args.dry_run)
    prefix = "dry-run " if args.dry_run else ""
    print(
        f"{prefix}new_skills={new_skills} attached_to_existing={attached} "
        f"alias_inserts={aliases_n}"
    )


if __name__ == "__main__":
    main()
