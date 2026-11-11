# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import html
import re
import markdownify

from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse, urlunparse

_PERCENT_ENCODED_OCTET = re.compile(r"%[0-9A-Fa-f]{2}")

# Accepted values for the ``table_format`` option.
#   markdown: pipe tables (default, lossy for merged cells)
#   html:     every table becomes a sanitized HTML table
#   auto:     HTML only for tables that use colspan/rowspan, Markdown otherwise
TABLE_FORMATS = ("markdown", "html", "auto")

_MAX_SPAN = 1000
_WHITESPACE = re.compile(r"\s+")


def _own_rows(table: Any) -> List[Any]:
    """The <tr> elements that belong to ``table`` itself, not to nested tables."""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _span(cell: Any, name: str) -> int:
    value = str(cell.get(name, "") or "").strip()
    if not value.isdigit():
        return 1
    return max(1, min(_MAX_SPAN, int(value)))


def _has_spans(table: Any) -> bool:
    for row in _own_rows(table):
        for cell in row.find_all(["td", "th"], recursive=False):
            if _span(cell, "colspan") > 1 or _span(cell, "rowspan") > 1:
                return True
    return False


def _cell_text(cell: Any) -> str:
    return _WHITESPACE.sub(" ", cell.get_text(" ")).strip()


# Inline markup kept inside HTML table cells: source tag -> emitted tag.
_INLINE_TAGS = {
    "strong": "strong",
    "b": "strong",
    "em": "em",
    "i": "em",
    "code": "code",
}
_SAFE_HREF_SCHEMES = ("http", "https", "mailto")
_HREF_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# C0 controls, DEL and whitespace: browsers ignore them inside a scheme, so
# "java\tscript:" must not slip past the scheme check.
_HREF_IGNORED = re.compile(r"[\x00-\x20\x7f]+")
# Elements whose boundaries separate words (get_text(" ") puts a space there).
_CELL_BLOCKS = frozenset(
    "address article aside blockquote caption dd div dl dt figcaption figure "
    "footer h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table "
    "tbody td tfoot th thead tr ul".split()
)
_CELL_SKIP = frozenset(("script", "style", "template", "noscript"))
_MAX_CELL_DEPTH = 64


def safe_href(href: Any) -> Optional[str]:
    """``href`` if it is http(s), mailto or relative; otherwise None."""
    if not isinstance(href, str):
        return None
    cleaned = _HREF_IGNORED.sub("", href)
    if not cleaned:
        return None
    match = _HREF_SCHEME.match(cleaned)
    if match is not None:
        if match.group(1).lower() not in _SAFE_HREF_SCHEMES:
            return None
    elif ":" in cleaned.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]:
        # Something like "x y:..." that is neither a scheme nor relative.
        return None
    return cleaned


def _has_inline_markup(cell: Any) -> bool:
    for tag in cell.find_all(True):
        if tag.name in _INLINE_TAGS or tag.name == "br":
            return True
        if tag.name == "a" and safe_href(tag.get("href")) is not None:
            return True
    return False


def _cell_markup(cell: Any) -> str:
    """Cell content as escaped text plus a small set of safe inline tags.

    Kept: <strong>/<b>, <em>/<i>, <code>, <br> and <a href> with an http(s),
    mailto or relative URL, all re-emitted without any other attribute. Every
    other element is dropped (its text is kept, escaped); script and style
    content is dropped entirely.
    """
    from bs4 import CData, NavigableString, Tag

    out: List[str] = []

    def walk(node: Any, depth: int, in_link: bool) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if type(child) in (NavigableString, CData):
                    out.append(html.escape(_WHITESPACE.sub(" ", str(child))))
                continue
            if not isinstance(child, Tag) or child.name in _CELL_SKIP:
                continue
            name = child.name
            if depth >= _MAX_CELL_DEPTH:
                out.append(html.escape(" " + _cell_text(child) + " "))
                continue
            if name == "br":
                out.append("<br>")
                continue
            if name in _CELL_BLOCKS:
                out.append(" ")
                walk(child, depth + 1, in_link)
                out.append(" ")
                continue
            emitted = _INLINE_TAGS.get(name)
            href = None
            if name == "a" and not in_link:
                href = safe_href(child.get("href"))
            if href is not None:
                start = len(out)
                out.append(f'<a href="{html.escape(href, quote=True)}">')
                walk(child, depth + 1, True)
                if "".join(out[start + 1 :]).strip():
                    out.append("</a>")
                else:
                    del out[start:]
            elif emitted is not None:
                start = len(out)
                out.append(f"<{emitted}>")
                walk(child, depth + 1, in_link)
                if "".join(out[start + 1 :]).strip():
                    out.append(f"</{emitted}>")
                else:
                    del out[start:]
            else:
                walk(child, depth + 1, in_link)

    walk(cell, 0, False)
    text = _WHITESPACE.sub(" ", "".join(out)).strip()
    return re.sub(r" ?<br> ?", "<br>", text)


def _cell_html(cell: Any) -> str:
    if _has_inline_markup(cell):
        return _cell_markup(cell)
    # Unchanged output for cells without inline markup.
    return html.escape(_cell_text(cell))


def table_to_html(table: Any) -> str:
    """Render a BeautifulSoup <table> as minimal, sanitized HTML.

    Only table/thead/tbody/tr/th/td are emitted, the only attributes kept are
    colspan and rowspan (as validated integers), and all cell text is escaped.
    Inside cells, <strong>/<b>, <em>/<i>, <code>, <br> and links with an
    http(s), mailto or relative href are kept (without other attributes);
    any other markup is flattened to text.
    """
    head: List[str] = []
    body: List[str] = []
    rows = _own_rows(table)
    has_thead = any(
        r.parent is not None and getattr(r.parent, "name", None) == "thead"
        for r in rows
    )
    for i, row in enumerate(rows):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        parts = []
        for cell in cells:
            tag = "th" if cell.name == "th" else "td"
            attrs = ""
            for name in ("colspan", "rowspan"):
                n = _span(cell, name)
                if n > 1:
                    attrs += f' {name}="{n}"'
            parts.append(f"<{tag}{attrs}>{_cell_html(cell)}</{tag}>")
        line = "<tr>" + "".join(parts) + "</tr>"
        in_head = getattr(row.parent, "name", None) == "thead"
        if not has_thead and i == 0 and all(c.name == "th" for c in cells):
            in_head = True
        (head if in_head else body).append(line)

    out = ["<table>"]
    if head:
        out += ["<thead>", *head, "</thead>"]
    if body:
        out += ["<tbody>", *body, "</tbody>"]
    out.append("</table>")
    return "\n".join(out)


def _quote_path_preserving_percent_encoded_octets(path: str) -> str:
    """Quote a URL path while preserving existing %HH byte encodings."""
    parts: list[str] = []
    last_end = 0

    for match in _PERCENT_ENCODED_OCTET.finditer(path):
        parts.append(quote(path[last_end : match.start()]))
        parts.append(match.group(0))
        last_end = match.end()

    parts.append(quote(path[last_end:]))
    return "".join(parts)


class _CustomMarkdownify(markdownify.MarkdownConverter):
    """
    A custom version of markdownify's MarkdownConverter. Changes include:

    - Altering the default heading style to use '#', '##', etc.
    - Removing javascript hyperlinks.
    - Truncating images with large data:uri sources.
    - Ensuring URIs are properly escaped, and do not conflict with Markdown syntax
    - Optionally emitting tables as sanitized HTML (``table_format`` option)
    """

    options: Dict[str, Any]

    def __init__(self, **options: Any):
        options["heading_style"] = options.get("heading_style", markdownify.ATX)
        options["keep_data_uris"] = options.get("keep_data_uris", False)
        table_format = options.get("table_format") or "markdown"
        if table_format not in TABLE_FORMATS:
            raise ValueError(
                f"table_format must be one of {', '.join(TABLE_FORMATS)}; got {table_format!r}"
            )
        options["table_format"] = table_format
        # Explicitly cast options to the expected type if necessary
        super().__init__(**options)

    def convert_hn(
        self,
        n: int,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """Same as usual, but be sure to start with a new line"""
        if not convert_as_inline:
            if not re.search(r"^\n", text):
                return "\n" + super().convert_hn(n, el, text, convert_as_inline)  # type: ignore

        return super().convert_hn(n, el, text, convert_as_inline)  # type: ignore

    def convert_a(
        self,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ):
        """Same as usual converter, but removes JavaScript links and escapes URIs."""
        prefix, suffix, text = markdownify.chomp(text)  # type: ignore
        if not text:
            return ""

        if el.find_parent("pre") is not None:
            return text

        href = el.get("href")
        title = el.get("title")

        # Escape URIs and skip non-http or file schemes
        if href:
            try:
                parsed_url = urlparse(href)  # type: ignore
                if parsed_url.scheme and parsed_url.scheme.lower() not in [
                    "http",
                    "https",
                    "file",
                ]:  # type: ignore
                    return "%s%s%s" % (prefix, text, suffix)
                href = urlunparse(
                    parsed_url._replace(
                        path=_quote_path_preserving_percent_encoded_octets(
                            parsed_url.path
                        )
                    )
                )  # type: ignore
            except ValueError:  # It's not clear if this ever gets thrown
                return "%s%s%s" % (prefix, text, suffix)

        # For the replacement see #29: text nodes underscores are escaped
        if (
            self.options["autolinks"]
            and text.replace(r"\_", "_") == href
            and not title
            and not self.options["default_title"]
        ):
            # Shortcut syntax
            return "<%s>" % href
        if self.options["default_title"] and not title:
            title = href
        title_part = ' "%s"' % title.replace('"', r"\"") if title else ""
        return (
            "%s[%s](%s%s)%s" % (prefix, text, href, title_part, suffix)
            if href
            else text
        )

    def convert_img(
        self,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """Same as usual converter, but removes data URIs"""

        alt = el.attrs.get("alt", None) or ""
        src = el.attrs.get("src", None) or ""
        data_src = el.attrs.get("data-src", None) or ""
        # Lazy-loading libraries commonly leave a tiny placeholder data URI in
        # src and put the real image in data-src. Prefer data-src when src
        # isn't a usable URL, so the placeholder doesn't win over actual
        # content. When keep_data_uris is set the caller explicitly wants the
        # embedded bytes, so a data URI in src is left alone.
        if data_src and (
            not src
            or (src[:5].lower() == "data:" and not self.options["keep_data_uris"])
        ):
            src = data_src
        title = el.attrs.get("title", None) or ""
        title_part = ' "%s"' % title.replace('"', r"\"") if title else ""
        # Remove all line breaks from alt
        alt = alt.replace("\n", " ")
        if (
            convert_as_inline
            and el.parent.name not in self.options["keep_inline_images_in"]
        ):
            return alt

        # Remove dataURIs
        if src[:5].lower() == "data:" and not self.options["keep_data_uris"]:
            src = src.split(",")[0] + "..."

        return "![%s](%s%s)" % (alt, src, title_part)

    def convert_input(
        self,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """Convert checkboxes to Markdown [x]/[ ] syntax."""

        if el.get("type") == "checkbox":
            return "[x] " if el.has_attr("checked") else "[ ] "
        return ""

    def convert_u(
        self,
        el: Any,
        text: str,
        convert_as_inline: Optional[bool] = False,
        **kwargs,
    ) -> str:
        if not text.strip():
            return text

        prefix, suffix, text = markdownify.chomp(text)  # type: ignore
        if not text:
            return ""
        return f"{prefix}<u>{text}</u>{suffix}"

    def convert_strike(self, el: Any, text: str, *args, **kwargs) -> str:
        """Obsolete <strike> is still in the wild; treat it like <s>/<del>."""
        return self.convert_s(el, text, *args, **kwargs)  # type: ignore

    def convert_table(self, el: Any, text: str, *args: Any, **kwargs: Any) -> str:
        """Markdown pipe table, or sanitized HTML depending on ``table_format``."""
        table_format = self.options.get("table_format", "markdown")
        if table_format == "html" or (table_format == "auto" and _has_spans(el)):
            caption = el.find("caption")
            prefix = ""
            if caption is not None and caption.find_parent("table") is el:
                caption_text = _cell_text(caption)
                if caption_text:
                    prefix = html.escape(caption_text) + "\n\n"
            return "\n\n" + prefix + table_to_html(el) + "\n\n"
        return super().convert_table(el, text, *args, **kwargs)  # type: ignore

    def convert_soup(self, soup: Any) -> str:
        return super().convert_soup(soup)  # type: ignore
