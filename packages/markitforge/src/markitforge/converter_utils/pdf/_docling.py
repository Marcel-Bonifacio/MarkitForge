# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Optional adapter that converts PDFs with IBM's docling."""

import logging
import os
import tempfile
from typing import Any, List, Tuple

from ..._base_converter import DocumentSection
from ..._exceptions import MissingDependencyException

logger = logging.getLogger(__name__)

DOCLING_MISSING_MESSAGE = (
    "pdf_backend='docling' needs the docling package, which is not installed. "
    "Install it with:\n\n* pip install docling\n\n"
    "(docling pulls in PyTorch and is large.) Or use pdf_backend='auto'."
)


def _page_sections(document: Any, markdown: str) -> List[DocumentSection]:
    """One section per page when docling can export pages separately."""
    try:
        page_numbers = sorted(int(n) for n in getattr(document, "pages", {}) or {})
        sections = [
            DocumentSection(
                index=n, markdown=document.export_to_markdown(page_no=n), kind="page"
            )
            for n in page_numbers
        ]
        if sections:
            return sections
    except Exception as exc:  # older docling without page_no support
        logger.debug("docling per-page export unavailable: %s", exc)
    return [DocumentSection(index=1, markdown=markdown, kind="page")]


def convert_with_docling(pdf_bytes: bytes) -> Tuple[str, List[DocumentSection]]:
    """Convert PDF bytes with docling; returns (markdown, page sections)."""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise MissingDependencyException(DOCLING_MISSING_MESSAGE) from exc

    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(pdf_bytes)
        result = DocumentConverter().convert(path)
        document = result.document
        markdown = document.export_to_markdown()
        return markdown, _page_sections(document, markdown)
    finally:
        try:
            os.unlink(path)
        except OSError:
            logger.debug("Could not remove temporary file %s", path)
