# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Showing the uploaded original next to the result.

* PDFs are served as ``application/pdf`` (inline, own restrictive CSP) for an
  ``<iframe>``.
* Common raster images are served with their image type for an ``<img>``.
* Text-like files (txt, csv, md, json, xml, html, svg, ...) are decoded and
  sent as text, which the page shows with ``textContent`` (never as HTML).
* Office files are exported to PDF by LibreOffice when it is installed, using
  the same isolated run as the core LibreOffice converter (private profile,
  explicit import filter, no network where the OS allows it, time limit).
"""

import codecs
import logging
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

__all__ = [
    "MAX_ORIGINAL_BYTES",
    "MAX_TEXT_CHARS",
    "PreviewUnavailable",
    "original_kind",
    "image_type",
    "decode_text",
    "office_to_pdf",
    "office_supported",
]

logger = logging.getLogger(__name__)

MAX_ORIGINAL_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 1024 * 1024

_TEXT_EXTENSIONS = {
    ".txt",
    ".text",
    ".log",
    ".csv",
    ".tsv",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".xml",
    ".html",
    ".htm",
    ".xhtml",
    ".svg",
    ".rss",
    ".atom",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".rst",
}
_IMAGE_TYPES: Dict[str, Tuple[str, Tuple[bytes, ...]]] = {
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    ".webp": ("image/webp", (b"RIFF",)),
    ".bmp": ("image/bmp", (b"BM",)),
}
# extension -> (LibreOffice import filter, PDF export filter)
_OOXML: Dict[str, Tuple[str, str]] = {
    ".docx": ("MS Word 2007 XML", "pdf:writer_pdf_Export"),
    ".docm": ("MS Word 2007 XML VBA", "pdf:writer_pdf_Export"),
    ".pptx": ("Impress MS PowerPoint 2007 XML", "pdf:impress_pdf_Export"),
    ".xlsx": ("Calc MS Excel 2007 XML", "pdf:calc_pdf_Export"),
}
_PDF_EXPORT = {
    "text": "pdf:writer_pdf_Export",
    "presentation": "pdf:impress_pdf_Export",
    "spreadsheet": "pdf:calc_pdf_Export",
}


class PreviewUnavailable(Exception):
    """No preview can be made; the message is shown to the user."""


def _lo() -> Optional[object]:
    try:
        from markitforge.converters import _libreoffice_converter as lo
    except ImportError:  # pragma: no cover - core always ships it
        return None
    return lo


def _office_filters(ext: str) -> Optional[Tuple[str, str]]:
    if ext in _OOXML:
        return _OOXML[ext]
    lo = _lo()
    filters = getattr(lo, "_INPUT_FILTERS", {})
    family = getattr(lo, "_family", None)
    if ext in filters and callable(family):
        kind = family(ext)
        if kind in _PDF_EXPORT:
            return filters[ext], _PDF_EXPORT[kind]
    return None


def office_supported(ext: str) -> bool:
    return _office_filters(ext) is not None


def image_type(ext: str, data: bytes) -> Optional[str]:
    """The image MIME type when the bytes really look like that image."""
    info = _IMAGE_TYPES.get(ext)
    if info is None:
        return None
    mime, magics = info
    if any(data.startswith(m) for m in magics):
        if ext == ".webp" and data[8:12] != b"WEBP":
            return None
        return mime
    return None


def original_kind(ext: str, data: bytes) -> str:
    """One of "pdf", "image", "text", "office" or "none"."""
    if ext == ".pdf" or data[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf" if b"%PDF-" in data[:1024] else "none"
    if image_type(ext, data):
        return "image"
    if ext in _TEXT_EXTENSIONS:
        return "text"
    if office_supported(ext):
        return "office"
    return "none"


def decode_text(data: bytes) -> Tuple[str, bool]:
    """Decode the start of a text file; returns (text, truncated)."""
    head = data[: MAX_TEXT_CHARS * 4]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = head.decode("utf-16", errors="replace")
    else:
        if head.startswith(b"\xef\xbb\xbf"):
            head = head[3:]
        try:
            # final=False: a character cut in half at the end is not an error.
            text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        except UnicodeDecodeError:
            text = head.decode("cp1252", errors="replace")
    truncated = len(text) > MAX_TEXT_CHARS or len(data) > MAX_TEXT_CHARS * 4
    return text[:MAX_TEXT_CHARS], truncated


def office_to_pdf(data: bytes, ext: str, timeout: float = 120.0) -> bytes:
    """Export an Office file to PDF with LibreOffice (isolated, time-limited)."""
    filters = _office_filters(ext)
    lo = _lo()
    if filters is None or lo is None:
        raise PreviewUnavailable("Preview not available for this file type.")
    try:
        soffice = lo.find_soffice()  # type: ignore[attr-defined]
        soffice_args = lo._soffice_args  # type: ignore[attr-defined]
        run = lo._run  # type: ignore[attr-defined]
        sandbox_prefix = lo._sandbox_prefix  # type: ignore[attr-defined]
        sandbox_works = lo._SANDBOX_WORKS  # type: ignore[attr-defined]
        sandbox_breaks = lo._sandbox_breaks  # type: ignore[attr-defined]
        input_filter = lo._input_filter  # type: ignore[attr-defined]
    except AttributeError:
        raise PreviewUnavailable("Preview not available for this file type.") from None
    if not soffice:
        raise PreviewUnavailable(
            "Preview not available for this file type. Install LibreOffice to "
            "see Office files here."
        )
    infilter, export = filters
    if ext not in _OOXML:
        try:
            infilter = input_filter(ext, data[:64])
        except Exception:  # noqa: BLE001 - keep the mapped filter
            pass
    with tempfile.TemporaryDirectory(prefix="markitforge-gui-preview-") as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / f"input{ext}"
        source.write_bytes(data)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        output = out_dir / "input.pdf"
        args = soffice_args(tmp_path / "profile", infilter, export, out_dir, source)
        sandbox = [] if sandbox_works.get(soffice) is False else sandbox_prefix()
        try:
            run(sandbox + [soffice] + args, tmp, timeout, ext)
            if sandbox and output.is_file():
                sandbox_works[soffice] = True
            elif sandbox and sandbox_breaks(soffice, sandbox):
                run([soffice] + args, tmp, timeout, ext)
        except Exception as exc:  # noqa: BLE001 - FileConversionException, OSError
            logger.info("LibreOffice preview failed: %s", exc)
            raise PreviewUnavailable(
                "LibreOffice could not make a preview of this file."
            ) from None
        if not output.is_file():
            raise PreviewUnavailable(
                "LibreOffice could not make a preview of this file."
            )
        pdf = output.read_bytes()
    if len(pdf) > MAX_ORIGINAL_BYTES:
        raise PreviewUnavailable("The preview of this file is too large to show.")
    return pdf
