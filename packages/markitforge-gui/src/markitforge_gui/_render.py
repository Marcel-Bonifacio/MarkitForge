# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Markdown -> safe HTML for the preview pane.

Converted documents are untrusted input: a PDF or web page can carry raw
HTML (``<script>``, ``onerror=``, ``javascript:`` links) straight into the
Markdown. We render with markdown-it-py (raw HTML allowed, so HTML tables
from ``table_format="html"`` display properly) and then run the result
through nh3, an allow-list sanitizer, before it ever reaches the browser.
"""

import re
from typing import Optional

import nh3
from markdown_it import MarkdownIt

__all__ = ["render_markdown", "sanitize_html"]

_md = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable(
    ["table", "strikethrough"]
)

ALLOWED_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "col",
    "colgroup",
    "dd",
    "del",
    "details",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "ins",
    "kbd",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "samp",
    "small",
    "span",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}

# Tags whose *content* is dropped as well (not just the tag itself).
CLEAN_CONTENT_TAGS = {"script", "style", "noscript", "template", "iframe", "object"}

ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start"},
}

# Page/slide/sheet markers are HTML comments (stripped by the sanitizer), so
# we swap them for a visible divider first. Only this class survives.
ALLOWED_CLASSES = {"div": {"mf-page-marker"}}

_LINK_SCHEMES = ("http:", "https:", "mailto:")
_IMAGE_DATA_PREFIXES = (
    "data:image/png",
    "data:image/jpeg",
    "data:image/jpg",
    "data:image/gif",
    "data:image/webp",
    "data:image/bmp",
)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_MARKER_RE = re.compile(
    r"<!--\s*(page|slide|sheet|chapter)\s+(\d{1,7})\s*-->", re.IGNORECASE
)
_INT_RE = re.compile(r"^\d{1,4}$")


def _is_relative(url: str) -> bool:
    # Browsers drop tab/CR/LF and treat "\\" as "/", so "\\/evil.com" or
    # "/<TAB>/evil.com" are protocol-relative too. Normalise first.
    norm = re.sub(r"[\t\r\n]", "", url).replace("\\", "/")
    return not _SCHEME_RE.match(norm) and not norm.startswith("//")


def _attribute_filter(tag: str, attr: str, value: str) -> Optional[str]:
    stripped = value.strip()
    if attr == "src":
        # Images: embedded data: images or relative paths only. No remote
        # images (no tracking pixels, nothing fetched from the internet).
        # Embedded data: images only. Relative paths would make the page send
        # same-origin GET requests chosen by the document (e.g. /api/zip).
        lowered = re.sub(r"\s", "", stripped).lower()
        if lowered.startswith(_IMAGE_DATA_PREFIXES):
            return stripped
        return None
    if attr == "href":
        lowered = stripped.lower()
        if lowered.startswith(_LINK_SCHEMES) or _is_relative(stripped):
            return stripped
        return None
    if attr in ("colspan", "rowspan", "span", "start", "width", "height"):
        return stripped if _INT_RE.match(stripped) else None
    if attr == "align":
        return (
            stripped.lower()
            if stripped.lower() in ("left", "right", "center", "justify")
            else None
        )
    return value


def sanitize_html(html: str) -> str:
    """Strip everything not on the allow-list from an HTML fragment."""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        clean_content_tags=CLEAN_CONTENT_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        url_schemes={"http", "https", "mailto", "data"},
        allowed_classes=ALLOWED_CLASSES,
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def _marker_to_div(match: "re.Match[str]") -> str:
    kind = match.group(1).lower()
    return (
        f'<div class="mf-page-marker">{kind.capitalize()} {int(match.group(2))}</div>'
    )


def render_markdown(markdown: str) -> str:
    """Render Markdown to sanitized HTML."""
    html = _md.render(markdown or "")
    html = _MARKER_RE.sub(_marker_to_div, html)
    return sanitize_html(html)
