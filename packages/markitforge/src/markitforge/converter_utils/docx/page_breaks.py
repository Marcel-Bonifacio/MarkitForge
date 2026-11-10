# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Page-break tracking for DOCX conversion.

Word records page boundaries in two ways: explicit breaks (``<w:br
w:type="page"/>``) and ``<w:lastRenderedPageBreak/>``, which Word writes where a
page started the last time it laid out the document. Mammoth drops both, so
before conversion each break is turned into a sentinel token that rides along
inside existing text, and after conversion the Markdown is split at those
tokens.

The token is only ever glued onto an existing, non-empty ``w:t`` (never a new
run or paragraph), so removing it restores exactly the Markdown that the
document would have produced without it.

Those stored breaks are only as good as the last layout Word saved (and are
missing from files other tools wrote). :func:`libreoffice_page_count` asks
LibreOffice to lay the document out instead and reports how many pages that
gives.
"""

import re
import tempfile
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple
from uuid import uuid4

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_CHOICE = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice"
_T = _W + "t"
_BR = _W + "br"
_P = _W + "p"
_RENDERED = _W + "lastRenderedPageBreak"
# Text inside these is rendered as a link; a token there could defeat
# markdownify's "<url>" autolink shortcut, so it is not used as an anchor.
_LINK_CONTAINERS = {_W + "hyperlink", _W + "fldSimple"}

_TABLE_LINE = re.compile(r"^\s*(?:\||</?(?:table|thead|tbody|tr)\b)")
_LIST_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_WORD = re.compile(r"\w")


def new_sentinel() -> str:
    """A unique, purely alphanumeric token (survives HTML and Markdown escaping)."""
    return "mfpagebreak" + uuid4().hex + "x"


def _append(el, token: str) -> None:  # type: ignore[no-untyped-def]
    text = el.text or ""
    body = text.rstrip()
    el.text = body + token + text[len(body) :]


def _prepend(el, token: str) -> None:  # type: ignore[no-untyped-def]
    text = el.text or ""
    lead = len(text) - len(text.lstrip())
    el.text = text[:lead] + token + text[lead:]


def mark_page_breaks(content: bytes, sentinel: str) -> bytes:
    """Insert ``sentinel`` into word/document.xml wherever a page starts.

    A ``lastRenderedPageBreak`` that follows another break with no text in
    between is the same page boundary (Word writes one right after a hard
    break) and is not counted twice. A break in a paragraph with text is
    attached to the end of the preceding text in that paragraph; otherwise it
    is carried forward to the start of the next text.

    The tree is walked once, top down, keeping the enclosing ``mc:Choice``
    and link depth and the stack of open paragraphs, so the cost is linear in
    the document size whatever its nesting. Parsing uses libxml2's default
    depth and size limits (no ``huge_tree``); a document beyond them raises,
    and the caller then leaves it unmarked.
    """
    if b"lastRenderedPageBreak" not in content and b'"page"' not in content:
        return content

    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser=parser)

    pending = 0
    seen_text = False
    last_anchor: Any = None
    anchor_paragraph: Any = None
    changed = False
    choice_depth = 0  # mc:Choice ancestors (Mammoth renders mc:Fallback)
    link_depth = 0  # hyperlink/field ancestors
    paragraphs: List[Any] = []  # open w:p elements, innermost last
    for event, el in etree.iterwalk(root, events=("start", "end")):
        tag = el.tag
        if event == "end":
            if tag == _MC_CHOICE:
                choice_depth -= 1
            elif tag in _LINK_CONTAINERS:
                link_depth -= 1
            elif tag == _P:
                paragraphs.pop()
            continue
        if tag == _MC_CHOICE:
            choice_depth += 1
            continue
        if tag in _LINK_CONTAINERS:
            link_depth += 1
            continue
        if tag == _P:
            paragraphs.append(el)
            continue
        if choice_depth or tag not in (_T, _BR, _RENDERED):
            continue
        para = paragraphs[-1] if paragraphs else None
        if tag == _T:
            if not (el.text and el.text.strip()):
                continue
            seen_text = True
            if link_depth:
                continue
            if pending:
                _prepend(el, sentinel * pending)
                pending = 0
                changed = True
            last_anchor = el
            anchor_paragraph = para
            continue
        if tag == _BR and el.get(_W + "type") != "page":
            continue
        if tag == _RENDERED and not seen_text:
            continue
        seen_text = False
        if last_anchor is not None and para is not None and anchor_paragraph is para:
            _append(last_anchor, sentinel)
            changed = True
        else:
            pending += 1

    if not changed:
        return content
    return etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)


def remove_sentinels(markdown: str, sentinel: str) -> str:
    return markdown.replace(sentinel, "")


def split_pages(markdown: str, sentinel: str) -> List[str]:
    """Split converted Markdown into pages at the sentinel tokens.

    Splits happen at line boundaries so Markdown syntax is never cut in half:
    a token at the start of a line (ignoring heading/list/emphasis markup)
    starts a new page before that line, any other token starts one after it.
    Tokens inside a table start the new page after the table. Trailing empty
    pages are dropped.
    """
    pages: List[List[str]] = [[]]
    pending = 0
    for line in markdown.split("\n"):
        count = line.count(sentinel)
        in_table = bool(_TABLE_LINE.match(line))
        if pending and not in_table:
            for _ in range(pending):
                pages.append([])
            pending = 0
        if not count:
            pages[-1].append(line)
            continue
        parts = line.split(sentinel)
        clean = "".join(parts)
        if in_table:
            pending += count
            pages[-1].append(clean)
            continue
        before = 0
        prefix = ""
        for part in parts[:-1]:
            prefix += part
            if _WORD.search(_LIST_PREFIX.sub("", prefix, count=1)):
                break
            before += 1
        for _ in range(before):
            pages.append([])
        pages[-1].append(clean)
        for _ in range(count - before):
            pages.append([])

    texts = ["\n".join(lines).strip() for lines in pages]
    while len(texts) > 1 and not texts[-1]:
        texts.pop()
    return texts


# ------------------------------------------------------ rendered page count

#: Accepted values of the ``docx_page_count`` option (besides ``None``).
PAGE_COUNT_MODES = ("libreoffice",)

_DOCX_FILTER = "MS Word 2007 XML"
_PDF_TARGET = "pdf:writer_pdf_Export"
_PDF_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![A-Za-z])")


def count_pdf_pages(path: Path) -> Optional[int]:
    """Number of pages of a PDF file, or ``None`` if it cannot be told."""
    try:
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfparser import PDFParser
        from pdfminer.pdftypes import resolve1

        with open(path, "rb") as stream:
            document = PDFDocument(PDFParser(stream))
            count = resolve1(resolve1(document.catalog["Pages"])["Count"])
        if isinstance(count, int) and count > 0:
            return count
    except Exception:
        pass
    try:
        count = len(_PDF_PAGE_OBJECT.findall(path.read_bytes()))
    except OSError:
        return None
    return count or None


def libreoffice_page_count(
    data: bytes, options: Mapping[str, Any]
) -> Tuple[Optional[int], str]:
    """Lay a DOCX out with LibreOffice and count the pages.

    The document is exported to PDF by the same isolated ``soffice`` run the
    LibreOffice converter uses (private profile, no network, the
    ``libreoffice_timeout`` option). Returns ``(pages, "")``, or ``(None,
    reason)`` when LibreOffice is missing or the export failed. The count is
    LibreOffice's layout, which can differ from Word's by a page when fonts
    are substituted.
    """
    # Imported here: the LibreOffice converter itself imports the DOCX one.
    from ..._exceptions import FileConversionException
    from ...converters import _libreoffice_converter as lo

    soffice = lo.find_soffice()
    if not soffice:
        return None, "LibreOffice (soffice) was not found"
    timeout = lo._timeout(dict(options))
    with tempfile.TemporaryDirectory(prefix="markitforge-lo-pages-") as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "input.docx"
        source.write_bytes(data)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        output = out_dir / "input.pdf"
        args = lo._soffice_args(
            tmp_path / "profile", _DOCX_FILTER, _PDF_TARGET, out_dir, source
        )
        sandbox = (
            [] if lo._SANDBOX_WORKS.get(soffice) is False else lo._sandbox_prefix()
        )
        try:
            lo._run(sandbox + [soffice] + args, tmp, timeout, ".docx")
            if sandbox and output.is_file():
                lo._SANDBOX_WORKS[soffice] = True
            elif sandbox and lo._sandbox_breaks(soffice, sandbox):
                lo._run([soffice] + args, tmp, timeout, ".docx")
        except FileConversionException as exc:
            return None, str(exc)
        except OSError as exc:
            return None, f"LibreOffice could not be run ({exc})"
        if not output.is_file():
            return None, "LibreOffice could not export the document to PDF"
        pages = count_pdf_pages(output)
    if pages is None:
        return None, "the PDF LibreOffice exported could not be read"
    return pages, ""
