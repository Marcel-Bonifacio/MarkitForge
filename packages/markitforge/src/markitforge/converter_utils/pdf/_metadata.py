# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PDF document-information metadata, normalised for DocumentConverterResult."""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# PDF Info key -> MarkitForge metadata key
_KEYS = {
    "Title": "title",
    "Author": "author",
    "Subject": "subject",
    "Keywords": "keywords",
    "Creator": "creator",
    "Producer": "producer",
    "CreationDate": "created",
    "ModDate": "modified",
}

# Defaults written by PDF tools when the author set nothing.
_PLACEHOLDERS = {
    "",
    "(anonymous)",
    "anonymous",
    "(unspecified)",
    "unspecified",
    "untitled",
    "unknown",
}

_DATE_RE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz+\-])(\d{2})?'?(\d{2})?'?)?"
)


def parse_pdf_date(value: str) -> Optional[str]:
    """Convert a PDF date ("D:20231005000756Z") to ISO 8601, or None."""
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, sign, tzh, tzm = m.groups()
    try:
        tz = None
        if sign in ("Z", "z"):
            tz = timezone.utc
        elif sign in ("+", "-"):
            delta = timedelta(hours=int(tzh or 0), minutes=int(tzm or 0))
            tz = timezone(delta if sign == "+" else -delta)
        dt = datetime(
            int(year),
            int(month or 1),
            int(day or 1),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=tz,
        )
    except ValueError:
        return None
    return dt.isoformat()


def _to_text(value: Any) -> Optional[str]:
    """Decode a raw PDF info value to text."""
    try:
        from pdfminer.pdftypes import resolve1
        from pdfminer.psparser import PSLiteral
        from pdfminer.utils import decode_text

        value = resolve1(value)
        if isinstance(value, PSLiteral):
            value = value.name
        if isinstance(value, bytes):
            value = decode_text(value)
    except ImportError:  # pragma: no cover - pdfminer is a hard dependency
        if isinstance(value, bytes):
            value = value.decode("latin-1")
    if value is None or isinstance(value, (list, dict)):
        return None
    return str(value).replace("\x00", "").strip()


def build_metadata(
    info: Mapping[str, Any], page_count: Optional[int]
) -> Dict[str, Any]:
    """Normalised metadata dict from a raw PDF Info dictionary."""
    meta: Dict[str, Any] = {}
    for pdf_key, key in _KEYS.items():
        text = _to_text(info.get(pdf_key)) if pdf_key in info else None
        if text is None or text.lower() in _PLACEHOLDERS:
            continue
        if key in ("created", "modified"):
            meta[key] = parse_pdf_date(text) or text
        else:
            meta[key] = text
    if page_count is not None:
        meta["page_count"] = page_count
    return meta


def read_metadata(pdf_file: BinaryIO) -> Tuple[Dict[str, Any], Optional[int]]:
    """Read the Info dictionary and page count with pdfminer alone."""
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    pos = pdf_file.tell()
    try:
        pdf_file.seek(0)
        doc = PDFDocument(PDFParser(pdf_file))
        info: Dict[str, Any] = {}
        for entry in doc.info or []:
            info.update(entry)
        pages = sum(1 for _ in PDFPage.create_pages(doc))
        return info, pages
    finally:
        pdf_file.seek(pos)
