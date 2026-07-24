"""Load skills + aliases from Postgres into an in-memory catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

import db


@dataclass(frozen=True)
class SkillRecord:
    skill_id: UUID
    name: str
    normalized_name: str
    category: Optional[str]
    is_hot: bool
    is_in_demand: bool


@dataclass
class SkillCatalog:
    skills: dict[UUID, SkillRecord] = field(default_factory=dict)
    # normalized_alias -> (skill_id, display_alias)
    aliases: dict[str, tuple[UUID, str]] = field(default_factory=dict)

    def get(self, skill_id: UUID) -> Optional[SkillRecord]:
        return self.skills.get(skill_id)


def load_skill_catalog(conn=None) -> SkillCatalog:
    """Load all skills and aliases. Caller may pass an open connection."""
    owns = conn is None
    if owns:
        conn = db.get_db_connection()
    try:
        catalog = SkillCatalog()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, normalized_name, category, is_hot, is_in_demand
                FROM skills
                """
            )
            for sid, name, norm, category, is_hot, is_in_demand in cur.fetchall():
                skill_id = sid if isinstance(sid, UUID) else UUID(str(sid))
                catalog.skills[skill_id] = SkillRecord(
                    skill_id=skill_id,
                    name=name,
                    normalized_name=norm,
                    category=category,
                    is_hot=bool(is_hot),
                    is_in_demand=bool(is_in_demand),
                )

            cur.execute(
                """
                SELECT skill_id, alias, normalized_alias
                FROM skill_aliases
                """
            )
            for skill_id, alias, norm_alias in cur.fetchall():
                sid = skill_id if isinstance(skill_id, UUID) else UUID(str(skill_id))
                if sid not in catalog.skills:
                    continue
                catalog.aliases[norm_alias] = (sid, alias)
        return catalog
    finally:
        if owns:
            conn.close()
