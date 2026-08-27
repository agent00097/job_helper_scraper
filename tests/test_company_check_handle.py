"""
Tests for services.company_check._company_handle.

company_check has heavy runtime dependencies (db, pika, rabbitmq_settings).
We mock them at sys.modules level before the import so the function can be
tested without a live database or message broker.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Patch heavy deps before importing company_check.
for _dep, _mock in (
    ("db", MagicMock()),
    ("pika", MagicMock()),
):
    if _dep not in sys.modules:
        sys.modules[_dep] = _mock

from services import company_check  # noqa: E402
from services.company_check import _company_handle  # noqa: E402


class TestCompanyHandle:
    def test_workday_url_extracts_tenant(self):
        result = _company_handle("workday", "https://stripe.wd5.myworkdayjobs.com/careers")
        assert result == "stripe"

    def test_workday_url_different_dc(self):
        result = _company_handle("workday", "https://airbnb.wd1.myworkdayjobs.com/Airbnb")
        assert result == "airbnb"

    def test_workday_url_with_site_path(self):
        result = _company_handle("workday", "https://anthropic.wd1.myworkdayjobs.com/External_Site")
        assert result == "anthropic"

    def test_bare_ashby_slug_passthrough(self):
        assert _company_handle("ashby", "stripe") == "stripe"

    def test_bare_lever_slug_passthrough(self):
        assert _company_handle("lever", "acme-corp") == "acme-corp"

    def test_bare_greenhouse_slug_passthrough(self):
        assert _company_handle("greenhouse", "anthropic") == "anthropic"

    def test_bare_smartrecruiters_slug_passthrough(self):
        assert _company_handle("smartrecruiters", "Visa") == "Visa"

    def test_workday_malformed_url_falls_through(self):
        # urlparse("not-a-url").hostname is None → parts[0] is "" → falls through
        assert _company_handle("workday", "not-a-url") == "not-a-url"

    def test_non_workday_source_with_url_like_value_passes_through(self):
        # Only Workday triggers the URL parse; other sources pass through as-is
        val = "https://boards.greenhouse.io/acme"
        assert _company_handle("greenhouse", val) == val


def test_ensure_company_publishes_scraped_logo_hint(monkeypatch):
    monkeypatch.setattr(company_check, "_lookup_company", lambda _: None)
    monkeypatch.setattr(company_check, "resolve_domain", lambda *_: "acme.com")
    monkeypatch.setattr(
        company_check,
        "resolve_ats_logo_hint",
        lambda *_: "https://cdn.example.com/acme-logo.png",
    )
    published = []
    monkeypatch.setattr(company_check, "_publish_onboarding", published.append)

    job = MagicMock()
    job.company = "Acme"
    job.company_domain_hint = None

    assert company_check.ensure_company(job, "ashby", "acme") is None
    assert published[0]["domain_hint"] == "acme.com"
    assert (
        published[0]["logo_hint_url"]
        == "https://cdn.example.com/acme-logo.png"
    )


def test_ensure_company_prefers_source_domain_hint(monkeypatch):
    monkeypatch.setattr(company_check, "_lookup_company", lambda _: None)
    guessed_domain = MagicMock(return_value="wrong.example")
    monkeypatch.setattr(company_check, "resolve_domain", guessed_domain)
    monkeypatch.setattr(company_check, "resolve_ats_logo_hint", lambda *_: None)
    published = []
    monkeypatch.setattr(company_check, "_publish_onboarding", published.append)

    job = MagicMock()
    job.company = "Acme"
    job.company_domain_hint = "acme.example"

    company_check.ensure_company(job, "greenhouse", "acme")

    assert published[0]["domain_hint"] == "acme.example"
    guessed_domain.assert_not_called()


def test_queue_existing_company_enrichment_publishes_without_lookup(monkeypatch):
    monkeypatch.setattr(
        company_check,
        "resolve_ats_logo_hint",
        lambda *_: "https://cdn.example.com/acme.png",
    )
    published = []
    monkeypatch.setattr(company_check, "_publish_onboarding", published.append)

    success = company_check.queue_existing_company_enrichment(
        company_name="Acme",
        normalized_name="acme",
        source_name="greenhouse",
        source_endpoint="acme",
        stored_domain="acme.com",
        source_domain_hint="careers.acme.com",
    )

    assert success is True
    assert published == [
        {
            "company_name": "Acme",
            "normalized_name": "acme",
            "source_name": "greenhouse",
            "source_endpoint": "acme",
            "domain_hint": "acme.com",
            "logo_hint_url": "https://cdn.example.com/acme.png",
        }
    ]
