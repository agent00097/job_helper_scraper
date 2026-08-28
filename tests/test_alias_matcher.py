"""Unit tests for alias skill matcher (no DB / API)."""
from __future__ import annotations

from uuid import uuid4

from utils.skills.alias_matcher import (
    AliasMatcher,
    derived_aliases_for_name,
    resolve_catalog_skill_id,
)
from utils.skills.catalog import SkillCatalog, SkillRecord


def _catalog_with(*pairs: tuple[str, str]) -> SkillCatalog:
    """pairs: (name, alias) — alias may equal name."""
    cat = SkillCatalog()
    for name, alias in pairs:
        sid = uuid4()
        cat.skills[sid] = SkillRecord(
            skill_id=sid,
            name=name,
            normalized_name=name.lower(),
            category="test",
            is_hot=False,
            is_in_demand=False,
        )
        cat.aliases[alias.lower()] = (sid, alias)
    return cat


def test_matches_cpp_in_title():
    cat = _catalog_with(("C++", "c++"), ("Python", "python"))
    hits = AliasMatcher(cat).match("Senior C++ Engineer", "Build systems in rust maybe")
    names = {h.skill_name for h in hits}
    assert "C++" in names
    cpp = next(h for h in hits if h.skill_name == "C++")
    assert cpp.in_title
    assert cpp.weight == 1.0


def test_derived_sql_alias():
    aliases = derived_aliases_for_name("Structured query language SQL")
    assert "SQL" in aliases


def test_sql_matched_via_derived_alias_in_requirements():
    cat = _catalog_with(
        ("Structured query language SQL", "structured query language sql"),
        ("Atlassian JIRA", "atlassian jira"),
    )
    desc = """
What you'll do
Lead projects.

Requirements
Knowledge of SQL and JIRA required.
"""
    hits = AliasMatcher(cat).match("Engineer", desc)
    names = {h.skill_name for h in hits}
    assert "Structured query language SQL" in names
    assert "Atlassian JIRA" in names


def test_skips_generic_email_software():
    cat = _catalog_with(("Email software", "email software"), ("Python", "python"))
    hits = AliasMatcher(cat).match("Dev", "Use email software and Python daily")
    names = {h.skill_name for h in hits}
    assert "Python" in names
    assert "Email software" not in names


def test_extra_alias_salesforce():
    cat = _catalog_with(("Salesforce software", "salesforce software"))
    hits = AliasMatcher(cat).match(
        "Business Systems Analyst III (Salesforce)",
        "Configure Salesforce CRM",
    )
    assert any(h.skill_name == "Salesforce software" for h in hits)


def test_extra_alias_react_via_software_suffix():
    cat = _catalog_with(("React software", "react software"))
    hits = AliasMatcher(cat).match("Frontend", "Build UI in React.js and TypeScript")
    assert any(h.skill_name == "React software" for h in hits)


def test_extra_alias_k8s():
    cat = _catalog_with(("Kubernetes", "kubernetes"))
    hits = AliasMatcher(cat).match("SRE", "Operate services on k8s")
    assert any(h.skill_name == "Kubernetes" for h in hits)


def test_extra_alias_nextjs():
    cat = _catalog_with(("Next.js", "next.js"))
    hits = AliasMatcher(cat).match("Engineer", "Experience with NextJS required")
    assert any(h.skill_name == "Next.js" for h in hits)


def test_resolve_catalog_skill_id_software_suffix_and_alias():
    sid = uuid4()
    by_name = {"react software": sid}
    alias_map = {"react software": (sid, "React software")}
    assert resolve_catalog_skill_id("react", by_name, alias_map) == sid
    assert resolve_catalog_skill_id("missing", by_name, alias_map) is None


def test_bare_go_not_matched_but_golang_is():
    cat = _catalog_with(("Go", "go"))
    hits = AliasMatcher(cat).match("Engineer", "Please go to the office")
    assert hits == []
    hits2 = AliasMatcher(cat).match("Backend", "Experience with Golang required")
    assert len(hits2) == 1
    assert hits2[0].skill_name == "Go"
