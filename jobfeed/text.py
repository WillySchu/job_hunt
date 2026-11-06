"""Turning posting HTML into readable plain text."""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

_BLOCK_TAGS = ["p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"]
# List items become "- item" lines with no blank line between them.


def html_to_text(raw: str | None) -> str:
    """Strip tags, decode entities and tidy whitespace.

    Greenhouse returns descriptions entity-escaped ("&lt;p&gt;..."), so the
    string is unescaped first when it looks escaped.
    """
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        if tag.name == "li":
            tag.insert_before("\n- ")
        else:
            tag.insert_before("\n")
            tag.insert_after("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
