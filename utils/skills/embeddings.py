"""Embed enriched skill catalog + match JD phrases via cosine similarity."""
from __future__ import annotations

import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence
from uuid import UUID

import numpy as np

from utils.skills.alias_matcher import derived_aliases_for_name
from utils.skills.catalog import SkillCatalog, SkillRecord
from utils.skills.phrases import Phrase

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "text-embedding-3-small"
_CACHE_VERSION = 2
_DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "skill_embeddings.bin"
_BATCH_SIZE = 100

EmbedFn = Callable[[list[str], str], list[list[float]]]

_openai_lock = threading.Lock()
_openai_client = None


@dataclass(frozen=True)
class EmbeddingHit:
    skill_id: UUID
    skill_name: str
    cosine: float
    weight: float
    phrase: str = ""
    phrase_source: str = ""


def _openai_embed_client():
    """Reuse one OpenAI client (and its HTTP pool) per process."""
    global _openai_client
    if _openai_client is not None:
        return _openai_client
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
    with _openai_lock:
        if _openai_client is None:
            _openai_client = OpenAI(api_key=api_key)
        return _openai_client


def _default_embed(texts: list[str], model: str) -> list[list[float]]:
    client = _openai_embed_client()
    resp = client.embeddings.create(model=model, input=texts)
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [list(d.embedding) for d in ordered]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


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


def _as_float32_matrix(vectors: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
    """Pack embeddings into one C-contiguous float32 array (not Python floats)."""
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.size == 0:
        return np.zeros((0, 0), dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2:
        raise ValueError(f"embedding matrix must be 2-D, got shape {matrix.shape}")
    return np.ascontiguousarray(matrix, dtype=np.float32)


class SkillEmbeddingIndex:
    """In-memory skill embedding matrix with on-disk cache.

    Vectors are stored as one float32 ndarray (L2-normalized after load/build)
    instead of `list[list[float]]`. For text-embedding-3-small (1536-d) that
    is ~4 bytes/dim versus ~28 bytes per Python float, plus list pointer
    overhead — typically a 5–8× RSS drop for the index.
    """

    def __init__(
        self,
        skill_ids: list[UUID],
        names: list[str],
        vectors: Sequence[Sequence[float]] | np.ndarray,
        model: str,
        *,
        version: int = _CACHE_VERSION,
        normalize: bool = True,
    ):
        matrix = _as_float32_matrix(vectors)
        if not (len(skill_ids) == len(names) == matrix.shape[0]):
            raise ValueError("skill_ids, names, vectors length mismatch")
        self.skill_ids = skill_ids
        self.names = names
        self.model = model
        self.version = version
        self._matrix = matrix
        if normalize and self._matrix.size:
            self._normalize_inplace()

    @property
    def vectors(self) -> np.ndarray:
        return self._matrix

    def _normalize_inplace(self) -> None:
        """L2-normalize rows so cosine similarity is a matrix multiply."""
        if self._matrix.size == 0:
            return
        norms = np.linalg.norm(self._matrix, axis=1, keepdims=True)
        np.maximum(norms, 1e-12, out=norms)
        self._matrix /= norms

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
                    "Loaded skill embedding cache v%s (%d vectors, %.1f MiB) from %s",
                    cached.version,
                    len(cached.skill_ids),
                    cached._matrix.nbytes / (1024 * 1024),
                    cache_path,
                )
                return cached

        skills = sorted(catalog.skills.values(), key=lambda s: s.normalized_name)
        texts = [skill_embed_text(s) for s in skills]
        rows: list[np.ndarray] = []
        logger.info(
            "Embedding %d enriched skills with %s (cache v%s) ...",
            len(texts),
            model,
            _CACHE_VERSION,
        )
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            batch_vecs = embed_fn(batch, model)
            rows.append(_as_float32_matrix(batch_vecs))
            logger.info(
                "  embedded %d / %d", min(i + _BATCH_SIZE, len(texts)), len(texts)
            )
            time.sleep(0.05)

        if rows:
            matrix = np.vstack(rows)
        else:
            matrix = np.zeros((0, 0), dtype=np.float32)
        del rows, texts

        # Persist original (unnormalized) vectors so the on-disk format stays
        # compatible with other services that share skill_embeddings.bin.
        index = cls(
            skill_ids=[s.skill_id for s in skills],
            names=[s.name for s in skills],
            vectors=matrix,
            model=model,
            version=_CACHE_VERSION,
            normalize=False,
        )
        index.save(cache_path)
        index._normalize_inplace()
        logger.info(
            "Skill embedding index ready (%d vectors, %.1f MiB float32)",
            len(index.skill_ids),
            index._matrix.nbytes / (1024 * 1024),
        )
        return index

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        dim = int(self._matrix.shape[1]) if self._matrix.size else 0
        meta = {
            "version": self.version,
            "model": self.model,
            "count": len(self.skill_ids),
            "dim": dim,
            "skill_ids": [str(s) for s in self.skill_ids],
            "names": self.names,
        }
        header = json.dumps(meta).encode("utf-8")
        payload = np.ascontiguousarray(self._matrix, dtype="<f4")
        with path.open("wb") as f:
            f.write(struct.pack("<I", len(header)))
            f.write(header)
            f.write(payload.tobytes())
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
                expected = count * dim * 4
                raw = f.read(expected)
            if dim <= 0 or count <= 0:
                matrix = np.zeros((count, max(dim, 0)), dtype=np.float32)
            else:
                if len(raw) != expected:
                    raise ValueError(
                        f"embedding payload size {len(raw)} != expected {expected}"
                    )
                matrix = np.frombuffer(raw, dtype="<f4").reshape(count, dim).copy()
            del raw
            return cls(
                skill_ids=[UUID(s) for s in meta["skill_ids"]],
                names=list(meta["names"]),
                vectors=matrix,
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
        if not parsed or self._matrix.size == 0:
            return []

        embed_fn = embed_fn or _default_embed
        exclude = exclude or set()
        texts = [t for t, _ in parsed]
        query = _as_float32_matrix(embed_fn(texts, self.model))
        q_norms = np.linalg.norm(query, axis=1, keepdims=True)
        np.maximum(q_norms, 1e-12, out=q_norms)
        query /= q_norms
        # (n_phrases, n_skills) — float32 matmul, no per-skill Python loop
        sims = query @ self._matrix.T

        if exclude:
            mask = np.fromiter(
                (sid in exclude for sid in self.skill_ids),
                dtype=bool,
                count=len(self.skill_ids),
            )
            sims[:, mask] = -1.0

        n_skills = sims.shape[1]
        k = max(1, min(int(top_k_per_phrase), n_skills))
        best: dict[UUID, EmbeddingHit] = {}
        for qi, (phrase, source) in enumerate(parsed):
            row = sims[qi]
            top_idx = np.argpartition(row, -k)[-k:]
            top_idx = top_idx[np.argsort(row[top_idx])[::-1]]
            for i in top_idx:
                sim = float(row[i])
                if sim < min_cosine:
                    continue
                sid = self.skill_ids[int(i)]
                weight = 0.45 + 0.40 * min(
                    1.0, (sim - min_cosine) / max(1e-6, 1.0 - min_cosine)
                )
                if source == "requirements":
                    weight = min(0.88, weight + 0.05)
                hit = EmbeddingHit(
                    skill_id=sid,
                    skill_name=self.names[int(i)],
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
