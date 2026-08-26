"""Skill embedding index: compact float32 storage + cosine query."""
from __future__ import annotations

from uuid import uuid4

import numpy as np

from utils.skills.catalog import SkillCatalog, SkillRecord
from utils.skills.embeddings import SkillEmbeddingIndex, cosine


def _ids_and_names():
    python_id = uuid4()
    sql_id = uuid4()
    return [python_id, sql_id], ["Python", "SQL"]


def test_cosine_self_and_orthogonal():
    assert abs(cosine([3.0, 4.0], [3.0, 4.0]) - 1.0) < 1e-6
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-6


def test_index_stores_float32_not_python_lists():
    ids, names = _ids_and_names()
    index = SkillEmbeddingIndex(
        ids,
        names,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        "test-model",
    )
    assert isinstance(index.vectors, np.ndarray)
    assert index.vectors.dtype == np.float32
    assert index.vectors.nbytes == 2 * 3 * 4


def test_query_phrases_picks_nearest(tmp_path):
    ids, names = _ids_and_names()
    original = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    index = SkillEmbeddingIndex(
        ids, names, original, "test-model", normalize=False
    )
    cache = tmp_path / "skill_embeddings.bin"
    index.save(cache)
    loaded = SkillEmbeddingIndex.load(cache)
    assert loaded is not None
    assert loaded.vectors.dtype == np.float32

    def embed(texts, _model):
        return [[0.99, 0.01, 0.0] for _ in texts]

    hits = loaded.query_phrases(
        ["python"],
        embed_fn=embed,
        top_k_per_phrase=1,
        min_cosine=0.5,
    )
    assert hits
    assert hits[0].skill_name == "Python"
    assert hits[0].cosine > 0.9


def test_query_phrases_honors_exclude():
    ids, names = _ids_and_names()
    index = SkillEmbeddingIndex(
        ids,
        names,
        [[1.0, 0.0], [0.0, 1.0]],
        "test-model",
    )

    def embed(texts, _model):
        return [[1.0, 0.0] for _ in texts]

    hits = index.query_phrases(
        ["python"],
        embed_fn=embed,
        top_k_per_phrase=2,
        min_cosine=0.1,
        exclude={ids[0]},
    )
    assert all(h.skill_id != ids[0] for h in hits)


def test_build_loads_existing_cache(tmp_path):
    python_id = uuid4()
    catalog = SkillCatalog()
    catalog.skills[python_id] = SkillRecord(
        skill_id=python_id,
        name="Python",
        normalized_name="python",
        category="lang",
        is_hot=True,
        is_in_demand=True,
    )
    cache = tmp_path / "skill_embeddings.bin"
    built = SkillEmbeddingIndex(
        [python_id],
        ["Python"],
        [[1.0, 0.0]],
        "test-model",
        normalize=False,
    )
    built.save(cache)

    loaded = SkillEmbeddingIndex.build(
        catalog,
        model="test-model",
        cache_path=cache,
        embed_fn=lambda *_: (_ for _ in ()).throw(AssertionError("should not embed")),
    )
    assert len(loaded.skill_ids) == 1
    assert loaded.vectors.dtype == np.float32
