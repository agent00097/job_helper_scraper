"""Best-effort company logo URL extraction from public ATS board pages."""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8
_LOGO_KEYS = {"logo", "logourl", "companylogo", "companylogourl"}


def _board_url(source_name: str, source_endpoint: str) -> Optional[str]:
    source = (source_name or "").strip().lower()
    endpoint = (source_endpoint or "").strip()
    if not endpoint:
        return None
    if source == "ashby":
        return f"https://jobs.ashbyhq.com/{endpoint}"
    if source == "lever":
        return f"https://jobs.lever.co/{endpoint}"
    if source == "greenhouse":
        return f"https://boards.greenhouse.io/{endpoint}"
    if source == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{endpoint}"
    if source in ("workday", "successfactors") and endpoint.startswith(
        ("http://", "https://")
    ):
        return endpoint.rstrip("/")
    return None


def _logo_from_json(node: object, base_url: str) -> Optional[str]:
    if isinstance(node, list):
        for item in node:
            result = _logo_from_json(item, base_url)
            if result:
                return result
        return None
    if not isinstance(node, dict):
        return None

    for key, value in node.items():
        normalized_key = str(key).replace("-", "").replace("_", "").lower()
        if normalized_key in _LOGO_KEYS:
            if isinstance(value, str) and value.strip():
                return urljoin(base_url, value.strip())
            if isinstance(value, dict):
                nested_url = value.get("url") or value.get("contentUrl")
                if isinstance(nested_url, str) and nested_url.strip():
                    return urljoin(base_url, nested_url.strip())

    for value in node.values():
        if isinstance(value, (dict, list)):
            result = _logo_from_json(value, base_url)
            if result:
                return result
    return None


def _extract_logo_url(html: str, base_url: str) -> Optional[str]:
    from utils.html_text import parsed_html

    with parsed_html(html) as soup:
        # ATS pages frequently embed organization metadata in JSON/JSON-LD.
        for script in soup.find_all("script"):
            script_type = (script.get("type") or "").lower()
            if "json" not in script_type and not script.get("id"):
                continue
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            result = _logo_from_json(data, base_url)
            if result:
                return result

        # Ashby sometimes serializes theme data into inline JavaScript rather than
        # a JSON script. Prefer the square mark, then the wordmark.
        for key in ("logoSquareImageUrl", "logoWordmarkImageUrl"):
            match = re.search(
                rf'"{key}"\s*:\s*"((?:\\.|[^"])*)"',
                html,
            )
            if not match:
                continue
            try:
                value = json.loads(f'"{match.group(1)}"')
            except (TypeError, ValueError):
                value = match.group(1).replace(r"\/", "/")
            if isinstance(value, str) and value.strip():
                return urljoin(base_url, value.strip())

        # Lever/Greenhouse and customized boards commonly render a logo <img>.
        for image in soup.find_all("img"):
            src = (image.get("src") or image.get("data-src") or "").strip()
            if not src:
                continue
            classes = image.get("class") or []
            context = " ".join(
                [src, image.get("alt") or "", image.get("id") or "", *classes]
            ).lower()
            if any(token in context for token in ("logo", "brand", "employer")):
                return urljoin(base_url, src)

        # Social preview image is less precise, so keep it as the final hint.
        for key in ("og:image", "twitter:image", "twitter:image:src"):
            tag = soup.find(
                "meta",
                attrs={
                    "property": key,
                },
            ) or soup.find("meta", attrs={"name": key})
            if tag:
                content = (tag.get("content") or "").strip()
                if content:
                    return urljoin(base_url, content)
        return None


@lru_cache(maxsize=2048)
def resolve_ats_logo_hint(
    source_name: str, source_endpoint: str
) -> Optional[str]:
    """Return a cached ATS-provided logo URL without downloading the image."""
    board_url = _board_url(source_name, source_endpoint)
    if not board_url:
        return None
    try:
        response = requests.get(
            board_url,
            timeout=_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 HarcoJobScraper/1.0"},
        )
        try:
            response.raise_for_status()
            html_text = response.text
            resolved_url = response.url
        finally:
            response.close()
    except requests.RequestException as exc:
        logger.debug("ATS logo hint fetch failed url=%s: %s", board_url, exc)
        return None

    logo_url = _extract_logo_url(html_text, resolved_url)
    if logo_url:
        logger.info(
            "Captured ATS logo hint source=%s endpoint=%s",
            source_name,
            source_endpoint,
        )
    return logo_url
