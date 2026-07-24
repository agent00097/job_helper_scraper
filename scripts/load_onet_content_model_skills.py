#!/usr/bin/env python3
"""
Load O*NET Essential / Transferable / Knowledge / Abilities CSVs into
skills + skill_aliases.

These files use Content Model Element Name (not Workplace Example). Each unique
Element Name becomes one skill row.

Usage (from job-helper-scraper):

  python scripts/load_onet_content_model_skills.py
  python scripts/load_onet_content_model_skills.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_db_connection

_WS_RE = re.compile(r"\s+")

# (filename, source, category_label)
_DATASETS = (
    ("essential_skills.csv", "onet_essential_skills", "Essential Skill"),
    ("transferable_skills.csv", "onet_transferable_skills", "Transferable Skill"),
    ("knowledge.csv", "onet_knowledge", "Knowledge"),
    ("abilities.csv", "onet_abilities", "Ability"),
)

_CONTENT_SOURCES = {source for _, source, _ in _DATASETS}


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip().lower())


def resolve_csv(filename: str) -> Path:
    candidates = [
        Path(_ROOT).parent / filename,
        Path(_ROOT) / filename,
        Path.cwd() / filename,
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(f"CSV not found: {filename} (searched {[str(c) for c in candidates]})")


def aggregate_elements(csv_path: Path, *, source: str, category: str) -> list[dict]:
    by_name: dict[str, dict] = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"Element Name", "Element ID"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{csv_path}: missing columns {sorted(missing)}")

        for row in reader:
            name = (row.get("Element Name") or "").strip()
            element_id = (row.get("Element ID") or "").strip()
            if not name:
                continue
            key = normalize(name)
            if not key:
                continue
            if key not in by_name:
                by_name[key] = {
                    "name": name,
                    "normalized_name": key,
                    "category": category,
                    "source": source,
                    "element_id": element_id,
                    "is_hot": False,
                    "is_in_demand": False,
                }
    return sorted(by_name.values(), key=lambda s: s["normalized_name"])


def prepare_skills(
    skills: list[dict],
    *,
    norms_by_source: dict[str, str],
    claimed_norms: set[str],
) -> list[dict]:
    """
    norms_by_source: normalized_name -> source already in DB (any source).
    claimed_norms: norms already claimed in this run.

    Same source + same norm → keep canonical name (idempotent re-run).
    Different source owns the norm → disambiguate with category suffix.
    """
    out: list[dict] = []
    for skill in skills:
        key = skill["normalized_name"]
        existing_source = norms_by_source.get(key)
        if existing_source and existing_source != skill["source"]:
            label = skill["category"]
            new_name = f"{skill['name']} ({label})"
            new_key = normalize(new_name)
            if new_key in claimed_norms or (
                new_key in norms_by_source and norms_by_source[new_key] != skill["source"]
            ):
                new_name = f"{skill['name']} ({skill['source']})"
                new_key = normalize(new_name)
            skill = dict(skill)
            skill["name"] = new_name
            skill["normalized_name"] = new_key
            key = new_key
        claimed_norms.add(key)
        norms_by_source[key] = skill["source"]
        out.append(skill)
    return out


def cleanup_accidental_suffix_duplicates() -> int:
    """
    Remove rows like 'Critical Thinking (Essential Skill)' when
    'Critical Thinking' already exists for the same source.
    Keep intentional 'Mathematics (Knowledge)'.
    """
    conn = get_db_connection()
    deleted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM skills s
                WHERE s.source = ANY(%s)
                  AND s.name ~ ' \\([^)]+\\)$'
                  AND EXISTS (
                    SELECT 1
                    FROM skills base
                    WHERE base.source = s.source
                      AND base.normalized_name = trim(
                        both from regexp_replace(s.name, ' \\([^)]+\\)$', '', 'g')
                      )
                      AND lower(base.name) = lower(
                        trim(both from regexp_replace(s.name, ' \\([^)]+\\)$', '', 'g'))
                      )
                  )
                """,
                (list(_CONTENT_SOURCES),),
            )
            deleted = cur.rowcount
            # Orphans in aliases cascade via FK; also drop aliases pointing nowhere (safety).
            cur.execute(
                """
                DELETE FROM skill_aliases a
                WHERE NOT EXISTS (SELECT 1 FROM skills s WHERE s.id = a.skill_id)
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return deleted


def upsert_skills(skills: list[dict]) -> tuple[int, int]:
    if not skills:
        return 0, 0
    names = [s["name"] for s in skills]
    norms = [s["normalized_name"] for s in skills]
    categories = [s["category"] for s in skills]
    sources = [s["source"] for s in skills]
    hots = [s["is_hot"] for s in skills]
    demands = [s["is_in_demand"] for s in skills]

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
                    source = EXCLUDED.source,
                    is_hot = skills.is_hot OR EXCLUDED.is_hot,
                    is_in_demand = skills.is_in_demand OR EXCLUDED.is_in_demand,
                    updated_at = now()
                """,
                (names, norms, categories, sources, hots, demands),
            )
            skills_n = cur.rowcount

            source_list = sorted({s["source"] for s in skills})
            cur.execute(
                """
                INSERT INTO skill_aliases (skill_id, alias, normalized_alias)
                SELECT s.id, s.name, s.normalized_name
                FROM skills s
                WHERE s.source = ANY(%s)
                ON CONFLICT (normalized_alias) DO UPDATE SET
                    skill_id = EXCLUDED.skill_id,
                    alias = EXCLUDED.alias
                """,
                (source_list,),
            )
            aliases_n = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return skills_n, aliases_n


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load O*NET Content Model skill/knowledge/ability CSVs"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    norms_by_source: dict[str, str] = {}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT normalized_name, source FROM skills")
            for norm, source in cur.fetchall():
                norms_by_source[norm] = source
    finally:
        conn.close()

    if not args.dry_run:
        removed = cleanup_accidental_suffix_duplicates()
        if removed:
            print(f"Removed {removed} accidental suffix-duplicate skill rows")
            # Refresh map after cleanup
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT normalized_name, source FROM skills")
                    norms_by_source = {norm: source for norm, source in cur.fetchall()}
            finally:
                conn.close()

    claimed: set[str] = set()
    all_skills: list[dict] = []
    for filename, source, category in _DATASETS:
        path = resolve_csv(filename)
        print(f"Reading {path} ...")
        skills = aggregate_elements(path, source=source, category=category)
        skills = prepare_skills(
            skills,
            norms_by_source=norms_by_source,
            claimed_norms=claimed,
        )
        print(f"  {source}: {len(skills)} unique elements")
        for s in skills[:3]:
            print(f"    sample: {s['name']!r}")
        all_skills.extend(skills)

    print(f"Total to upsert: {len(all_skills)}")
    if args.dry_run:
        print("Dry run — no DB writes.")
        return

    skills_n, aliases_n = upsert_skills(all_skills)
    print(f"Upserted ~{skills_n} skill rows, ~{aliases_n} aliases")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, count(*)
                FROM skills
                GROUP BY source
                ORDER BY source
                """
            )
            print("skills by source:")
            for source, n in cur.fetchall():
                print(f"  {source}: {n}")
            cur.execute("SELECT count(*) FROM skills")
            print(f"skills total: {cur.fetchone()[0]}")
            cur.execute("SELECT count(*) FROM skill_aliases")
            print(f"skill_aliases total: {cur.fetchone()[0]}")
            cur.execute(
                """
                SELECT name, source FROM skills
                WHERE name ILIKE 'Mathematics%'
                ORDER BY source
                """
            )
            print("mathematics rows:", cur.fetchall())
    finally:
        conn.close()


if __name__ == "__main__":
    main()
