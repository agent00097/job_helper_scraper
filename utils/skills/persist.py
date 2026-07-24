"""Persist extracted skills into job_skills."""
from __future__ import annotations

import logging
from typing import Iterable, Optional
from uuid import UUID

import db
from utils.skills.extract import SkillHit

logger = logging.getLogger(__name__)


def replace_job_skills(job_id: UUID | str, hits: Iterable[SkillHit]) -> int:
    """
    Replace all job_skills rows for a job with the given hits.
    Returns number of rows written.
    """
    job_id = str(job_id)
    rows = [
        (job_id, str(h.skill_id), float(h.weight), h.method)
        for h in hits
        if h.skill_id and h.weight and h.weight > 0
    ]

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_skills WHERE job_id = %s::uuid", (job_id,))
            if rows:
                cur.executemany(
                    """
                    INSERT INTO job_skills (job_id, skill_id, weight, method, updated_at)
                    VALUES (%s::uuid, %s::uuid, %s, %s, now())
                    ON CONFLICT (job_id, skill_id) DO UPDATE SET
                        weight = EXCLUDED.weight,
                        method = EXCLUDED.method,
                        updated_at = now()
                    """,
                    rows,
                )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
