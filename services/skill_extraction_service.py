"""
Skill extraction for scraped jobs (alias + optional embeddings).

Controlled by env:
  SKILL_EXTRACTION_ENABLED   default "true"
  SKILL_EXTRACTION_EMBEDDINGS default "true" (only used when an API key is present)
  OPENAI_API_KEY or SKILL_EMBEDDING_API_KEY
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional
from uuid import UUID

from utils.skills.alias_matcher import AliasMatcher
from utils.skills.catalog import SkillCatalog, load_skill_catalog
from utils.skills.embeddings import SkillEmbeddingIndex
from utils.skills.extract import extract_skills
from utils.skills.persist import replace_job_skills

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_service: Optional["SkillExtractionService"] = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _has_embed_key() -> bool:
    return bool(
        (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("SKILL_EMBEDDING_API_KEY")
            or ""
        ).strip()
    )


class SkillExtractionService:
    def __init__(self) -> None:
        self._catalog: Optional[SkillCatalog] = None
        self._matcher: Optional[AliasMatcher] = None
        self._index: Optional[SkillEmbeddingIndex] = None
        self._index_load_attempted = False
        self._load_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return _env_bool("SKILL_EXTRACTION_ENABLED", True)

    @property
    def embeddings_enabled(self) -> bool:
        return _env_bool("SKILL_EXTRACTION_EMBEDDINGS", True) and _has_embed_key()

    def _ensure_alias_ready(self) -> None:
        if self._catalog is not None and self._matcher is not None:
            return
        with self._load_lock:
            if self._catalog is None:
                logger.info("Loading skill catalog for extraction ...")
                self._catalog = load_skill_catalog()
                logger.info(
                    "Skill catalog loaded: %d skills, %d aliases",
                    len(self._catalog.skills),
                    len(self._catalog.aliases),
                )
            if self._matcher is None:
                self._matcher = AliasMatcher(self._catalog)

    def _ensure_embeddings_ready(self) -> Optional[SkillEmbeddingIndex]:
        if not self.embeddings_enabled:
            return None
        if self._index is not None:
            return self._index
        if self._index_load_attempted:
            return self._index
        with self._load_lock:
            if self._index is not None or self._index_load_attempted:
                return self._index
            self._index_load_attempted = True
            self._ensure_alias_ready()
            assert self._catalog is not None
            try:
                logger.info(
                    "Loading/building skill embedding index "
                    "(cached under data/skill_embeddings.bin) ..."
                )
                self._index = SkillEmbeddingIndex.build(self._catalog)
                logger.info(
                    "Skill embedding index ready (%d vectors)",
                    len(self._index.skill_ids),
                )
            except Exception as exc:
                logger.warning(
                    "Skill embeddings unavailable; continuing alias-only: %s",
                    exc,
                )
                self._index = None
            return self._index

    def extract_and_save(
        self,
        job_id: UUID | str,
        title: Optional[str],
        description: Optional[str],
    ) -> int:
        """
        Extract skills for a job and write job_skills.
        Returns number of skills written. Never raises to callers of scrape path
        when wrapped; this method may raise and the caller should catch.
        """
        if not self.enabled:
            return 0
        if not description or not str(description).strip():
            return 0

        self._ensure_alias_ready()
        assert self._catalog is not None and self._matcher is not None
        index = self._ensure_embeddings_ready()

        hits = extract_skills(
            title or "",
            description or "",
            catalog=self._catalog,
            alias_matcher=self._matcher,
            embedding_index=index,
        )
        n = replace_job_skills(job_id, hits)
        logger.info(
            "Extracted %d skills for job %s (embeddings=%s)",
            n,
            job_id,
            "on" if index is not None else "off",
        )
        return n


def get_skill_extraction_service() -> SkillExtractionService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = SkillExtractionService()
    return _service


def extract_skills_for_job(
    job_id: UUID | str,
    title: Optional[str],
    description: Optional[str],
) -> int:
    """Module-level helper used by job_storage."""
    return get_skill_extraction_service().extract_and_save(job_id, title, description)
