"""Combine section-aware alias + phrase-embedding skill extraction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from utils.skills.alias_matcher import AliasMatcher
from utils.skills.catalog import SkillCatalog
from utils.skills.embeddings import SkillEmbeddingIndex
from utils.skills.phrases import phrases_from_sections
from utils.skills.sections import extract_sections


@dataclass(frozen=True)
class SkillHit:
    skill_id: UUID
    skill_name: str
    weight: float
    method: str  # alias | embedding
    matched_alias: Optional[str] = None
    cosine: Optional[float] = None
    in_title: bool = False
    source: str = ""  # title | requirements | responsibilities | body | phrase source
    phrase: Optional[str] = None


def extract_skills(
    title: str,
    description: str = "",
    *,
    catalog: SkillCatalog,
    alias_matcher: Optional[AliasMatcher] = None,
    embedding_index: Optional[SkillEmbeddingIndex] = None,
    embed_top_k: int = 10,
    embed_min_cosine: float = 0.60,
) -> list[SkillHit]:
    """
    Hybrid extract:
      1) Find Requirements / What-you'll-do sections
      2) Alias-match title + those sections (or full body fallback)
      3) Extract phrases from sections → embed → nearest skills
    """
    matcher = alias_matcher or AliasMatcher(catalog)
    sections = extract_sections(description or "")
    alias_hits = matcher.match_sections(
        title or "",
        sections,
        fallback_body=description or "",
    )

    by_id: dict[UUID, SkillHit] = {}
    for h in alias_hits:
        by_id[h.skill_id] = SkillHit(
            skill_id=h.skill_id,
            skill_name=h.skill_name,
            weight=h.weight,
            method="alias",
            matched_alias=h.alias,
            in_title=h.in_title,
            source=h.source,
        )

    if embedding_index is not None:
        phrases = phrases_from_sections(
            sections.requirements,
            sections.responsibilities,
            fallback_text="" if sections.structured else (description or ""),
        )
        # Title only contributes product-like tokens (MAWM, C++), not full titles.
        if title:
            from utils.skills.phrases import extract_phrases

            for p in extract_phrases(title, source="title"):
                if len(p.text.split()) > 3:
                    continue
                if p.text.lower() not in {x.text.lower() for x in phrases}:
                    phrases.append(p)

        emb_hits = embedding_index.query_phrases(
            phrases,
            top_k_per_phrase=1,
            min_cosine=embed_min_cosine,
            exclude=set(by_id.keys()),
            max_skills=embed_top_k * 2,
        )
        kept = 0
        for h in emb_hits:
            if h.skill_id in by_id:
                continue
            skill = catalog.get(h.skill_id)
            # Prefer hot/in-demand; allow others at a slightly higher bar.
            if skill and not (skill.is_hot or skill.is_in_demand):
                if (h.cosine or 0.0) < embed_min_cosine + 0.05:
                    continue
            by_id[h.skill_id] = SkillHit(
                skill_id=h.skill_id,
                skill_name=h.skill_name,
                weight=h.weight,
                method="embedding",
                cosine=h.cosine,
                source=h.phrase_source or "embedding",
                phrase=h.phrase,
            )
            kept += 1
            if kept >= embed_top_k:
                break

    return sorted(by_id.values(), key=lambda x: (-x.weight, x.skill_name.lower()))
