"""Embed enriched skill catalog + match JD phrases via cosine similarity."""
from __future__ import annotations

import json
import logging
import math
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence
from uuid import UUID

from utils.skills.alias_matcher import derived_aliases_for_name
from utils.skills.catalog import SkillCatalog, SkillRecord
from utils.skills.phrases import Phrase

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "text-embedding-3-small"
_CACHE_VERSION = 2
_DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "skill_embeddings.bin"
_BATCH_SIZE = 100

EmbedFn = Callable[[list[str], str], list[list[float]]]


@dataclass(frozen=True)
class EmbeddingHit:
    skill_id: UUID
    skill_name: str
    cosine: float
    weight: float
    phrase: str = ""
    phrase_source: str = ""


def _default_embed(texts: list[str], model: str) -> list[list[float]]:
    from openai import OpenAI

    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("SKILL_EMBEDDING_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY (or SKILL_EMBEDDING_API_KEY) is required for embeddings"
        )
    client = OpenAI(api_key=api_key)
    resp = client.embeddings.create(model=model, input=texts)
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [list(d.embedding) for d in ordered]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def skill_embed_text(skill: SkillRecord) -> str:
    """
    Enriched document for a skill: official name + short forms + category.
    Makes phrase queries like "SQL" or "MuleSoft" land on the right skill.
    """
    parts = [skill.name]
    for alias in derived_aliases_for_name(skill.name):
        if alias.lower() != skill.name.lower():
            parts.append(alias)
    if skill.category:
        parts.append(skill.category)
    # De-dupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        key = p.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(p.strip())
    return " | ".join(uniq)


class SkillEmbeddingIndex:
    """In-memory skill embedding matrix with on-disk cache."""

    def __init__(
        self,
        skill_ids: list[UUID],
        names: list[str],
        vectors: list[list[float]],
        model: str,
        *,
        version: int = _CACHE_VERSION,
    ):
        if not (len(skill_ids) == len(names) == len(vectors)):
            raise ValueError("skill_ids, names, vectors length mismatch")
        self.skill_ids = skill_ids
        self.names = names
        self.vectors = vectors
        self.model = model
        self.version = version
        self._norms = [math.sqrt(sum(x * x for x in v)) or 1.0 for v in vectors]

    @classmethod
    def build(
        cls,
        catalog: SkillCatalog,
        *,
        model: str = _DEFAULT_MODEL,
        embed_fn: Optional[EmbedFn] = None,
        cache_path: Optional[Path] = None,
        force_rebuild: bool = False,
    ) -> "SkillEmbeddingIndex":
        cache_path = cache_path or _DEFAULT_CACHE
        embed_fn = embed_fn or _default_embed

        if cache_path.is_file() and not force_rebuild:
            cached = cls.load(cache_path)
            if (
                cached
                and cached.model == model
                and cached.version == _CACHE_VERSION
                and len(cached.skill_ids) == len(catalog.skills)
            ):
                logger.info(
                    "Loaded skill embedding cache v%s (%d vectors) from %s",
                    cached.version,
                    len(cached.skill_ids),
                    cache_path,
                )
                return cached

        skills = sorted(catalog.skills.values(), key=lambda s: s.normalized_name)
        texts = [skill_embed_text(s) for s in skills]
        vectors: list[list[float]] = []
        logger.info(
            "Embedding %d enriched skills with %s (cache v%s) ...",
            len(texts),
            model,
            _CACHE_VERSION,
        )
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            vectors.extend(embed_fn(batch, model))
            logger.info(
                "  embedded %d / %d", min(i + _BATCH_SIZE, len(texts)), len(texts)
            )
            time.sleep(0.05)

        index = cls(
            skill_ids=[s.skill_id for s in skills],
            names=[s.name for s in skills],
            vectors=vectors,
            model=model,
            version=_CACHE_VERSION,
        )
        index.save(cache_path)
        return index

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        dim = len(self.vectors[0]) if self.vectors else 0
        meta = {
            "version": self.version,
            "model": self.model,
            "count": len(self.skill_ids),
            "dim": dim,
            "skill_ids": [str(s) for s in self.skill_ids],
            "names": self.names,
        }
        header = json.dumps(meta).encode("utf-8")
        with path.open("wb") as f:
            f.write(struct.pack("<I", len(header)))
            f.write(header)
            for vec in self.vectors:
                f.write(struct.pack(f"<{dim}f", *vec))
        logger.info("Wrote skill embedding cache to %s", path)

    @classmethod
    def load(cls, path: Path) -> Optional["SkillEmbeddingIndex"]:
        if not path.is_file():
            return None
        try:
            with path.open("rb") as f:
                (hlen,) = struct.unpack("<I", f.read(4))
                meta = json.loads(f.read(hlen).decode("utf-8"))
                dim = int(meta["dim"])
                count = int(meta["count"])
                vectors: list[list[float]] = []
                for _ in range(count):
                    raw = f.read(4 * dim)
                    vectors.append(list(struct.unpack(f"<{dim}f", raw)))
            return cls(
                skill_ids=[UUID(s) for s in meta["skill_ids"]],
                names=list(meta["names"]),
                vectors=vectors,
                model=meta["model"],
                version=int(meta.get("version", 1)),
            )
        except Exception as exc:
            logger.warning("Failed to load embedding cache %s: %s", path, exc)
            return None

    def query_phrases(
        self,
        phrases: Sequence[Phrase | str],
        *,
        embed_fn: Optional[EmbedFn] = None,
        top_k_per_phrase: int = 2,
        min_cosine: float = 0.52,
        exclude: Optional[set[UUID]] = None,
        max_skills: int = 20,
    ) -> list[EmbeddingHit]:
        """
        Embed each phrase and take nearest skills. Aggregate by max cosine.
        """
        parsed: list[tuple[str, str]] = []
        for p in phrases:
            if isinstance(p, Phrase):
                text, source = p.text, p.source
            else:
                text, source = str(p), ""
            text = (text or "").strip()
            if text:
                parsed.append((text[:200], source))
        if not parsed or not self.vectors:
            return []

        embed_fn = embed_fn or _default_embed
        exclude = exclude or set()
        texts = [t for t, _ in parsed]
        vectors = embed_fn(texts, self.model)

        # skill_id -> best hit
        best: dict[UUID, EmbeddingHit] = {}
        for (phrase, source), q in zip(parsed, vectors):
            qn = math.sqrt(sum(x * x for x in q)) or 1.0
            scored: list[tuple[float, int]] = []
            for i, vec in enumerate(self.vectors):
                sid = self.skill_ids[i]
                if sid in exclude:
                    continue
                dot = 0.0
                for a, b in zip(q, vec):
                    dot += a * b
                sim = dot / (qn * self._norms[i])
                if sim >= min_cosine:
                    scored.append((sim, i))
            scored.sort(reverse=True)
            for sim, i in scored[:top_k_per_phrase]:
                sid = self.skill_ids[i]
                weight = 0.45 + 0.40 * min(
                    1.0, (sim - min_cosine) / max(1e-6, 1.0 - min_cosine)
                )
                # Slight boost when phrase came from requirements.
                if source == "requirements":
                    weight = min(0.88, weight + 0.05)
                hit = EmbeddingHit(
                    skill_id=sid,
                    skill_name=self.names[i],
                    cosine=sim,
                    weight=round(weight, 4),
                    phrase=phrase,
                    phrase_source=source,
                )
                prev = best.get(sid)
                if prev is None or hit.cosine > prev.cosine:
                    best[sid] = hit

        return sorted(best.values(), key=lambda h: (-h.cosine, h.skill_name.lower()))[
            :max_skills
        ]

    def query(
        self,
        text: str | Sequence[str],
        *,
        embed_fn: Optional[EmbedFn] = None,
        top_k: int = 25,
        min_cosine: float = 0.45,
        exclude: Optional[set[UUID]] = None,
    ) -> list[EmbeddingHit]:
        """Legacy helper: treat texts as phrases."""
        if isinstance(text, str):
            phrases: list[Phrase | str] = [text]
        else:
            phrases = list(text)
        return self.query_phrases(
            phrases,
            embed_fn=embed_fn,
            top_k_per_phrase=1,
            min_cosine=min_cosine,
            exclude=exclude,
            max_skills=top_k,
        )
