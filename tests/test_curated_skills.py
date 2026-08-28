"""Curated skill list sanity — no DB."""
from collections import Counter

from utils.skills.curated import CURATED_SKILLS


def test_curated_names_and_aliases_are_unique():
    names = [s["name"].strip().lower() for s in CURATED_SKILLS]
    assert len(names) == len(set(names))
    alias_counts = Counter()
    for skill in CURATED_SKILLS:
        assert skill["name"].strip()
        assert skill["aliases"]
        for alias in skill["aliases"]:
            key = alias.strip().lower()
            assert key
            assert len(key) >= 3
            alias_counts[key] += 1
    dupes = [k for k, n in alias_counts.items() if n > 1]
    assert dupes == []
