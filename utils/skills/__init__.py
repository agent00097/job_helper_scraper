"""Job/user skill extraction (section-aware alias + phrase embeddings)."""

from utils.skills.extract import SkillHit, extract_skills
from utils.skills.catalog import SkillCatalog, load_skill_catalog
from utils.skills.sections import JobSections, extract_sections

__all__ = [
    "SkillHit",
    "SkillCatalog",
    "extract_skills",
    "load_skill_catalog",
    "JobSections",
    "extract_sections",
]
