"""
Tests for services.company_check._company_handle.

company_check has heavy runtime dependencies (db, pika, rabbitmq_settings).
We mock them at sys.modules level before the import so the function can be
tested without a live database or message broker.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# Patch heavy deps before importing company_check.
# 'models' is mocked as a plain module so pydantic is never imported.
_models_mock = types.ModuleType("models")
_models_mock.JobData = MagicMock()  # type: ignore[attr-defined]

for _dep, _mock in (
    ("db", MagicMock()),
    ("pika", MagicMock()),
    ("workers", MagicMock()),
    ("workers.rabbitmq_settings", MagicMock()),
    ("models", _models_mock),
):
    if _dep not in sys.modules:
        sys.modules[_dep] = _mock

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

    def test_workday_malformed_url_falls_through(self):
        # urlparse("not-a-url").hostname is None → parts[0] is "" → falls through
        assert _company_handle("workday", "not-a-url") == "not-a-url"

    def test_non_workday_source_with_url_like_value_passes_through(self):
        # Only Workday triggers the URL parse; other sources pass through as-is
        val = "https://boards.greenhouse.io/acme"
        assert _company_handle("greenhouse", val) == val
