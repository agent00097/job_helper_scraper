"""Tests for JD section + phrase extraction."""
from __future__ import annotations

from utils.skills.phrases import phrases_from_sections
from utils.skills.sections import extract_sections

SAMPLE = """
Our partner is looking for a Project Incident Manager.

What you'll do
Lead incident management for Manhattan Active Warehouse Management (MAWM).
Coordinate integration platforms such as MuleSoft.

What we offer
Fully remote position. Great benefits.

Requirements
Solid knowledge of REST APIs, SQL, log analysis, and cloud platforms including AWS, Azure, or Google Cloud.
Experience using JIRA and Xray. Hands-on with MuleSoft or TIBCO.
"""


def test_extracts_requirements_and_responsibilities():
    secs = extract_sections(SAMPLE)
    assert secs.structured
    assert "REST APIs" in secs.requirements or "SQL" in secs.requirements
    assert "Manhattan Active" in secs.responsibilities or "incident" in secs.responsibilities.lower()
    assert "Fully remote" not in secs.requirements


def test_phrases_include_sql_jira_mulesoft():
    secs = extract_sections(SAMPLE)
    phrases = phrases_from_sections(secs.requirements, secs.responsibilities)
    blob = " | ".join(p.text.lower() for p in phrases)
    assert "sql" in blob
    assert "jira" in blob
    assert "mulesoft" in blob or "rest" in blob
