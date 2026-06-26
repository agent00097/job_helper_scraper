"""
Hermetic tests for utils.domain_resolver.
All HTTP calls are mocked — no real network access.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from utils.domain_resolver import (
    _normalize_slug,
    _slug_variants,
    is_junk_slug,
    resolve_domain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_response(title: str = "", status: int = 200, ct: str = "text/html; charset=utf-8") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": ct}
    resp.text = f"<html><head><title>{title}</title></head></html>"
    return resp


def _no_response() -> MagicMock:
    """Simulate a connection failure."""
    raise ConnectionError("unreachable")


# ---------------------------------------------------------------------------
# Slug normalization + variant generation
# ---------------------------------------------------------------------------

class TestNormalizeSlug:
    def test_lowercases(self):
        assert _normalize_slug("Stripe") == "stripe"

    def test_drops_non_alnum_hyphen(self):
        # Underscores, dots, slashes removed; hyphens kept
        assert _normalize_slug("acme_corp.io") == "acmecorpio"

    def test_keeps_hyphens(self):
        assert _normalize_slug("acme-corp") == "acme-corp"


class TestSlugVariants:
    def test_no_known_suffix_returns_base_only(self):
        assert _slug_variants("stripe") == ["stripe"]

    def test_strips_jobs_suffix(self):
        variants = _slug_variants("acme-jobs")
        assert "acme-jobs" in variants
        assert "acme" in variants
        assert variants[0] == "acme-jobs"  # base first

    def test_strips_careers_suffix(self):
        variants = _slug_variants("techcorp-careers")
        assert "techcorp" in variants

    def test_strips_inc_suffix(self):
        assert "myco" in _slug_variants("myco-inc")

    def test_no_strip_when_result_too_short(self):
        # "a-jobs": len=6, len("jobs")+2=6, 6 > 6 is False → no strip
        variants = _slug_variants("a-jobs")
        assert variants == ["a-jobs"]

    def test_base_longer_than_suffix_boundary_strips(self):
        # "ab-jobs": len=7, 7 > 6 → strips to "ab"
        variants = _slug_variants("ab-jobs")
        assert "ab" in variants

    def test_no_duplicate_in_variants(self):
        variants = _slug_variants("stripe")
        assert len(variants) == len(set(variants))


# ---------------------------------------------------------------------------
# is_junk_slug
# ---------------------------------------------------------------------------

class TestIsJunkSlug:
    def test_empty_slug(self):
        assert is_junk_slug("", "Acme Inc") is True

    def test_hex_hash_slug(self):
        # 32-char hex string, no word structure
        assert is_junk_slug("3a8c9d2b1f4e5a8c9d2b1f4e5a8c9d2b", "Acme") is True

    def test_uuid_slug(self):
        assert is_junk_slug("550e8400-e29b-41d4-a716-446655440000", "Acme") is True

    def test_not_on_website(self):
        assert is_junk_slug("some-company", "Not On Website") is True

    def test_sandbox_company(self):
        assert is_junk_slug("sandbox-co", "Sandbox Corp") is True

    def test_internal_employees_only(self):
        assert is_junk_slug("corp", "Internal Employees Only") is True

    def test_normal_company_not_junk(self):
        assert is_junk_slug("stripe", "Stripe") is False

    def test_normal_company_with_hyphens(self):
        assert is_junk_slug("acme-global-corp", "Acme Global Corp") is False

    def test_long_but_non_hex_slug_not_junk(self):
        # > 30 chars but contains non-hex letters — not a hex hash
        assert is_junk_slug("acme-international-technology-solutions", "Acme Intl") is False


# ---------------------------------------------------------------------------
# resolve_domain — .com preferred over alt TLD
# ---------------------------------------------------------------------------

class TestComPreferredOverAltTld:
    def test_returns_com_when_it_verifies(self):
        with patch("utils.domain_resolver.requests.get") as mock_get:
            mock_get.return_value = _html_response("Stripe - Home")
            result = resolve_domain("Stripe", "stripe")
        assert result == "stripe.com"

    def test_com_tried_before_io(self):
        """If .com verifies, .io should never be requested."""
        with patch("utils.domain_resolver.requests.get") as mock_get:
            mock_get.return_value = _html_response("Acme Corporation")
            result = resolve_domain("Acme", "acme")

        assert result == "acme.com"
        urls_called = [c.args[0] for c in mock_get.call_args_list]
        assert "https://acme.com" in urls_called
        assert not any(".io" in u for u in urls_called)

    def test_falls_back_to_io_when_com_fails(self):
        def _side(url, **kw):
            if url == "https://acme.com":
                return _html_response(status=404)
            if url == "https://acme.io":
                return _html_response("Acme - Developer Tools")
            return _html_response(status=404)

        with patch("utils.domain_resolver.requests.get", side_effect=_side):
            result = resolve_domain("Acme", "acme")

        assert result == "acme.io"


# ---------------------------------------------------------------------------
# Verification logic
# ---------------------------------------------------------------------------

class TestVerification:
    def test_rejects_page_without_distinctive_word(self):
        """A page whose title has nothing to do with the company should be rejected."""
        with patch("utils.domain_resolver.requests.get") as mock_get:
            mock_get.return_value = _html_response("Welcome to Our Website")
            result = resolve_domain("Stripe", "stripe")
        assert result is None

    def test_accepts_title_containing_company_name(self):
        with patch("utils.domain_resolver.requests.get") as mock_get:
            mock_get.return_value = _html_response("Stripe - Payments Infrastructure")
            result = resolve_domain("Stripe", "stripe")
        assert result == "stripe.com"

    def test_accepts_slug_word_in_title(self):
        """Even if company_name is generic, slug token in title should pass."""
        with patch("utils.domain_resolver.requests.get") as mock_get:
            mock_get.return_value = _html_response("Anthropic - AI Safety")
            result = resolve_domain("Anthropic", "anthropic")
        assert result == "anthropic.com"

    def test_rejects_non_html_content_type(self):
        with patch("utils.domain_resolver.requests.get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"Content-Type": "application/json"}
            resp.text = '{"company": "stripe"}'
            mock_get.return_value = resp
            result = resolve_domain("Stripe", "stripe")
        assert result is None

    def test_rejects_4xx_status(self):
        with patch("utils.domain_resolver.requests.get") as mock_get:
            mock_get.return_value = _html_response("Not Found", status=404)
            result = resolve_domain("Stripe", "stripe")
        assert result is None

    def test_og_site_name_used_for_verification(self):
        html = (
            '<html><head>'
            '<meta property="og:site_name" content="Stripe" />'
            '</head></html>'
        )
        with patch("utils.domain_resolver.requests.get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"Content-Type": "text/html"}
            resp.text = html
            mock_get.return_value = resp
            result = resolve_domain("Stripe", "stripe")
        assert result == "stripe.com"

    def test_suffix_stripped_variant_resolves(self):
        """acme-jobs → tries acme-jobs.com first, then acme.com."""
        call_log: list[str] = []

        def _side(url, **kw):
            call_log.append(url)
            if url == "https://acme.com":
                return _html_response("Acme Corp - Home")
            return _html_response(status=404)

        with patch("utils.domain_resolver.requests.get", side_effect=_side):
            result = resolve_domain("Acme", "acme-jobs")

        assert result == "acme.com"
        assert "https://acme-jobs.com" in call_log
        assert "https://acme.com" in call_log


# ---------------------------------------------------------------------------
# resolve_domain — graceful failure on network errors
# ---------------------------------------------------------------------------

class TestGracefulFailure:
    def test_returns_none_when_all_fetches_raise(self):
        with patch("utils.domain_resolver.requests.get", side_effect=ConnectionError("timeout")):
            result = resolve_domain("Stripe", "stripe")
        assert result is None

    def test_returns_none_for_empty_slug(self):
        with patch("utils.domain_resolver.requests.get") as mock_get:
            result = resolve_domain("Stripe", "")
        mock_get.assert_not_called()
        assert result is None

    def test_returns_none_for_all_stopword_company(self):
        """If company name yields no distinctive words, bail early without network calls."""
        with patch("utils.domain_resolver.requests.get") as mock_get:
            # "The Group" → tokens: "the" (stopword), "group" (stopword) → no distinctive words
            result = resolve_domain("The Group", "the-group")
        mock_get.assert_not_called()
        assert result is None
