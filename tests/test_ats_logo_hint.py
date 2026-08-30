from unittest.mock import MagicMock, patch

import requests

from services.ats_logo_hint import (
    _board_url,
    _extract_logo_url,
    resolve_ats_logo_hint,
)


def test_board_urls():
    assert _board_url("ashby", "acme") == "https://jobs.ashbyhq.com/acme"
    assert _board_url("lever", "acme") == "https://jobs.lever.co/acme"
    assert _board_url("greenhouse", "acme") == "https://boards.greenhouse.io/acme"
    assert _board_url("smartrecruiters", "Visa") == "https://jobs.smartrecruiters.com/Visa"
    workday = "https://acme.wd1.myworkdayjobs.com/Careers"
    assert _board_url("workday", workday) == workday
    sap = "https://jobs.sap.com"
    assert _board_url("successfactors", sap) == sap
    assert _board_url("unknown", "acme") is None


def test_extracts_embedded_json_logo():
    html = """
        <script id="__NEXT_DATA__" type="application/json">
          {"props":{"organization":{"logoUrl":"https://cdn.example.com/acme.png"}}}
        </script>
    """
    assert (
        _extract_logo_url(html, "https://jobs.ashbyhq.com/acme")
        == "https://cdn.example.com/acme.png"
    )


def test_extracts_ashby_inline_theme_logo():
    html = r"""
        <script>
          window.__theme = {"logoWordmarkImageUrl":"https:\/\/cdn.example.com\/wordmark.png",
                            "logoSquareImageUrl":"https:\/\/cdn.example.com\/square.png"};
        </script>
    """
    assert (
        _extract_logo_url(html, "https://jobs.ashbyhq.com/acme")
        == "https://cdn.example.com/square.png"
    )


def test_extracts_logo_img_before_meta_fallback():
    html = """
        <meta property="og:image" content="/social.png">
        <img class="main-header-logo" src="/company.png" alt="Acme logo">
    """
    assert (
        _extract_logo_url(html, "https://jobs.lever.co/acme")
        == "https://jobs.lever.co/company.png"
    )


def test_resolve_fetches_board_once():
    response = MagicMock()
    response.text = '<img id="company-logo" src="https://cdn.example.com/logo.svg">'
    response.url = "https://boards.greenhouse.io/acme"
    response.raise_for_status.return_value = None

    with patch("services.ats_logo_hint.requests.get", return_value=response) as get:
        result = resolve_ats_logo_hint("greenhouse", "acme")

    assert result == "https://cdn.example.com/logo.svg"
    get.assert_called_once()


def test_resolve_is_best_effort_on_http_failure():
    with patch(
        "services.ats_logo_hint.requests.get",
        side_effect=requests.RequestException("network down"),
    ):
        assert resolve_ats_logo_hint("ashby", "acme") is None
