"""HTML helpers that always tear down BeautifulSoup trees."""
from __future__ import annotations

import html as _html
import re
from contextlib import contextmanager
from typing import Iterator


def html_to_text(html_content: str) -> str:
    """Strip HTML to plain text and discard the parse tree."""
    if not html_content:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html_content)
        return _html.unescape(re.sub(r"\s+", " ", text)).strip()

    soup = BeautifulSoup(html_content, "html.parser")
    try:
        text = soup.get_text(separator="\n")
    finally:
        soup.decompose()
    return _html.unescape(text).strip()


@contextmanager
def parsed_html(html_text: str) -> Iterator:
    """Parse HTML and always decompose the soup, including on early return."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text or "", "html.parser")
    try:
        yield soup
    finally:
        soup.decompose()
