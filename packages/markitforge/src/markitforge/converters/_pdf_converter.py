# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

"""PDF to Markdown: tables, headings, page sections, metadata and warnings.

Backends (``pdf_backend``):
- "auto" (default): pdfplumber layout analysis, falling back to pdfminer
  plain text if pdfplumber fails on the file.
- "pdfplumber": the same analysis, but errors are raised.
- "pdfminer": plain text only (the original MarkItDown behaviour).
- "docling": IBM docling, when installed.
"""

import io
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from .._base_converter import (
    DocumentAsset,
    DocumentConverter,
    DocumentConverterResult,
    DocumentSection,
    join_sections,
)
from .._stream_info import StreamInfo
from .._exceptions import (
    MISSING_DEPENDENCY_MESSAGE,
    MissingDependencyException,
    ResourceLimitException,
)
from ..converter_utils._text_safety import neutralize_page_markers
from ..converter_utils.pdf._docling import convert_with_docling
from ..converter_utils.pdf._layout import (
    Block,
    HeadingLevels,
    TextLine,
    annotate_headings,
    body_font_size,
    extract_lines,
    heading_runs,
    is_math_font,
    render_page,
)
from ..converter_utils.pdf._metadata import build_metadata, read_metadata
from ..converter_utils.pdf._tables import (
    find_page_tables,
    point_in_bboxes,
    render_markdown_table,
)
from ..converter_utils.pdf._visual import (
    DEFAULT_MAX_LLM_CALLS,
    DEFAULT_MAX_RENDER_PIXELS,
    VisualBudget,
    describe_page_images,
    find_math_regions,
    transcribe_math_regions,
)
from ._llm_caption import llm_caption

logger = logging.getLogger(__name__)

PDF_BACKENDS = ("auto", "pdfplumber", "pdfminer", "docling")

# Documents with more pages are refused (max_pdf_pages; 0 = unlimited).
DEFAULT_MAX_PDF_PAGES = 5000

# Pattern for MasterFormat-style partial numbering (e.g., ".1", ".2", ".10")
PARTIAL_NUMBERING_PATTERN = re.compile(r"^\.\d+$")


def _merge_partial_numbering_lines(text: str) -> str:
    """
    Post-process extracted text to merge MasterFormat-style partial numbering
    with the following text line.

    MasterFormat documents use partial numbering like:
        .1  The intent of this Request for Proposal...
        .2  Available information relative to...

    Some PDF extractors split these into separate lines:
        .1
        The intent of this Request for Proposal...

    This function merges them back together.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check if this line is ONLY a partial numbering
        if PARTIAL_NUMBERING_PATTERN.match(stripped):
            # Look for the next non-empty line to merge with
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1

            if j < len(lines):
                # Merge the partial numbering with the next line
                next_line = lines[j].strip()
                result_lines.append(f"{stripped} {next_line}")
                i = j + 1  # Skip past the merged line
            else:
                # No next line to merge with, keep as is
                result_lines.append(line)
                i += 1
        else:
            result_lines.append(line)
            i += 1

    return "\n".join(result_lines)


# Load dependencies
_dependency_exc_info = None
try:
    import pdfminer
    import pdfminer.high_level
    import pdfplumber
except ImportError:
    _dependency_exc_info = sys.exc_info()


ACCEPTED_MIME_TYPE_PREFIXES = [
    "application/pdf",
    "application/x-pdf",
]

ACCEPTED_FILE_EXTENSIONS = [".pdf"]


def _to_markdown_table(table: list[list[str]], include_separator: bool = True) -> str:
    """Convert a 2D list (rows/columns) into a nicely aligned Markdown table.

    Args:
        table: 2D list of cell values
        include_separator: If True, include header separator row (standard markdown).
                          If False, output simple pipe-separated rows.
    """
    if not table:
        return ""

    # Normalize None → ""
    table = [[cell if cell is not None else "" for cell in row] for row in table]

    # Filter out empty rows
    table = [row for row in table if any(cell.strip() for cell in row)]

    if not table:
        return ""

    # Column widths
    col_widths = [max(len(str(cell)) for cell in col) for col in zip(*table)]

    def fmt_row(row: list[str]) -> str:
        return (
            "|"
            + "|".join(str(cell).ljust(width) for cell, width in zip(row, col_widths))
            + "|"
        )

    if include_separator:
        header, *rows = table
        md = [fmt_row(header)]
        md.append("|" + "|".join("-" * w for w in col_widths) + "|")
        for row in rows:
            md.append(fmt_row(row))
    else:
        md = [fmt_row(row) for row in table]

    return "\n".join(md)


def _extract_form_content_from_words(page: Any) -> str | None:
    """
    Extract form-style content from a PDF page by analyzing word positions.
    This handles borderless forms/tables where words are aligned in columns.

    Returns markdown with proper table formatting:
    - Tables have pipe-separated columns with header separator rows
    - Non-table content is rendered as plain text

    Returns None if the page doesn't appear to be a form-style document,
    indicating that pdfminer should be used instead for better text spacing.
    """
    words = page.extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)
    if not words:
        return None

    # Group words by their Y position (rows)
    y_tolerance = 5
    rows_by_y: dict[float, list[dict]] = {}
    for word in words:
        y_key = round(word["top"] / y_tolerance) * y_tolerance
        if y_key not in rows_by_y:
            rows_by_y[y_key] = []
        rows_by_y[y_key].append(word)

    # Sort rows by Y position
    sorted_y_keys = sorted(rows_by_y.keys())
    page_width = page.width if hasattr(page, "width") else 612

    # First pass: analyze each row
    row_info: list[dict] = []
    for y_key in sorted_y_keys:
        row_words = sorted(rows_by_y[y_key], key=lambda w: w["x0"])
        if not row_words:
            continue

        first_x0 = row_words[0]["x0"]
        last_x1 = row_words[-1]["x1"]
        line_width = last_x1 - first_x0
        combined_text = " ".join(w["text"] for w in row_words)

        # Count distinct x-position groups (columns)
        x_positions = [w["x0"] for w in row_words]
        x_groups: list[float] = []
        for x in sorted(x_positions):
            if not x_groups or x - x_groups[-1] > 50:
                x_groups.append(x)

        # Determine row type
        is_paragraph = line_width > page_width * 0.55 and len(combined_text) > 60

        # Check for MasterFormat-style partial numbering (e.g., ".1", ".2")
        # These should be treated as list items, not table rows
        has_partial_numbering = False
        if row_words:
            first_word = row_words[0]["text"].strip()
            if PARTIAL_NUMBERING_PATTERN.match(first_word):
                has_partial_numbering = True

        row_info.append(
            {
                "y_key": y_key,
                "words": row_words,
                "text": combined_text,
                "x_groups": x_groups,
                "is_paragraph": is_paragraph,
                "num_columns": len(x_groups),
                "has_partial_numbering": has_partial_numbering,
            }
        )

    # Collect ALL x-positions from rows with 3+ columns (table-like rows)
    # This gives us the global column structure
    all_table_x_positions: list[float] = []
    for info in row_info:
        if info["num_columns"] >= 3 and not info["is_paragraph"]:
            all_table_x_positions.extend(info["x_groups"])

    if not all_table_x_positions:
        return None

    # Compute adaptive column clustering tolerance based on gap analysis
    all_table_x_positions.sort()

    # Calculate gaps between consecutive x-positions
    gaps = []
    for i in range(len(all_table_x_positions) - 1):
        gap = all_table_x_positions[i + 1] - all_table_x_positions[i]
        if gap > 5:  # Only significant gaps
            gaps.append(gap)

    # Determine optimal tolerance using statistical analysis
    if gaps and len(gaps) >= 3:
        # Use 70th percentile of gaps as threshold (balances precision/recall)
        sorted_gaps = sorted(gaps)
        percentile_70_idx = int(len(sorted_gaps) * 0.70)
        adaptive_tolerance = sorted_gaps[percentile_70_idx]

        # Clamp tolerance to reasonable range [25, 50]
        adaptive_tolerance = max(25, min(50, adaptive_tolerance))
    else:
        # Fallback to conservative value
        adaptive_tolerance = 35

    # Compute global column boundaries using adaptive tolerance
    global_columns: list[float] = []
    for x in all_table_x_positions:
        if not global_columns or x - global_columns[-1] > adaptive_tolerance:
            global_columns.append(x)

    # Adaptive max column check based on page characteristics
    # Calculate average column width
    if len(global_columns) > 1:
        content_width = global_columns[-1] - global_columns[0]
        avg_col_width = content_width / len(global_columns)

        # Forms with very narrow columns (< 30px) are likely dense text
        if avg_col_width < 30:
            return None

        # Compute adaptive max based on columns per inch
        # Typical forms have 3-8 columns per inch
        columns_per_inch = len(global_columns) / (content_width / 72)

        # If density is too high (> 10 cols/inch), likely not a form
        if columns_per_inch > 10:
            return None

        # Adaptive max: allow more columns for wider pages
        # Standard letter is 612pt wide, so scale accordingly
        adaptive_max_columns = int(20 * (page_width / 612))
        adaptive_max_columns = max(15, adaptive_max_columns)  # At least 15

        if len(global_columns) > adaptive_max_columns:
            return None
    else:
        # Single column, not a form
        return None

    # Now classify each row as table row or not
    # A row is a table row if it has words that align with 2+ of the global columns
    for info in row_info:
        if info["is_paragraph"]:
            info["is_table_row"] = False
            continue

        # Rows with partial numbering (e.g., ".1", ".2") are list items, not table rows
        if info["has_partial_numbering"]:
            info["is_table_row"] = False
            continue

        # Count how many global columns this row's words align with
        aligned_columns: set[int] = set()
        for word in info["words"]:
            word_x = word["x0"]
            for col_idx, col_x in enumerate(global_columns):
                if abs(word_x - col_x) < 40:
                    aligned_columns.add(col_idx)
                    break

        # If row uses 2+ of the established columns, it's a table row
        info["is_table_row"] = len(aligned_columns) >= 2

    # Find table regions (consecutive table rows)
    table_regions: list[tuple[int, int]] = []  # (start_idx, end_idx)
    i = 0
    while i < len(row_info):
        if row_info[i]["is_table_row"]:
            start_idx = i
            while i < len(row_info) and row_info[i]["is_table_row"]:
                i += 1
            end_idx = i
            table_regions.append((start_idx, end_idx))
        else:
            i += 1

    # Check if enough rows are table rows (at least 20%)
    total_table_rows = sum(end - start for start, end in table_regions)
    if len(row_info) > 0 and total_table_rows / len(row_info) < 0.2:
        return None

    # Build output - collect table data first, then format with proper column widths
    result_lines: list[str] = []
    num_cols = len(global_columns)

    # Helper function to extract cells from a row
    def extract_cells(info: dict) -> list[str]:
        cells: list[str] = ["" for _ in range(num_cols)]
        for word in info["words"]:
            word_x = word["x0"]
            # Find the correct column using boundary ranges
            assigned_col = num_cols - 1  # Default to last column
            for col_idx in range(num_cols - 1):
                col_end = global_columns[col_idx + 1]
                if word_x < col_end - 20:
                    assigned_col = col_idx
                    break
            if cells[assigned_col]:
                cells[assigned_col] += " " + word["text"]
            else:
                cells[assigned_col] = word["text"]
        return cells

    # Process rows, collecting table data for proper formatting
    idx = 0
    while idx < len(row_info):
        info = row_info[idx]

        # Check if this row starts a table region
        table_region = None
        for start, end in table_regions:
            if idx == start:
                table_region = (start, end)
                break

        if table_region:
            start, end = table_region
            # Collect all rows in this table
            table_data: list[list[str]] = []
            for table_idx in range(start, end):
                cells = extract_cells(row_info[table_idx])
                table_data.append(cells)

            # Calculate column widths for this table
            if table_data:
                col_widths = [
                    max(len(row[col]) for row in table_data) for col in range(num_cols)
                ]
                # Ensure minimum width of 3 for separator dashes
                col_widths = [max(w, 3) for w in col_widths]

                # Format header row
                header = table_data[0]
                header_str = (
                    "| "
                    + " | ".join(
                        cell.ljust(col_widths[i]) for i, cell in enumerate(header)
                    )
                    + " |"
                )
                result_lines.append(header_str)

                # Format separator row
                separator = (
                    "| "
                    + " | ".join("-" * col_widths[i] for i in range(num_cols))
                    + " |"
                )
                result_lines.append(separator)

                # Format data rows
                for row in table_data[1:]:
                    row_str = (
                        "| "
                        + " | ".join(
                            cell.ljust(col_widths[i]) for i, cell in enumerate(row)
                        )
                        + " |"
                    )
                    result_lines.append(row_str)

            idx = end  # Skip to end of table region
        else:
            # Check if we're inside a table region (not at start)
            in_table = False
            for start, end in table_regions:
                if start < idx < end:
                    in_table = True
                    break

            if not in_table:
                # Non-table content
                result_lines.append(info["text"])
            idx += 1

    return "\n".join(result_lines)


def _extract_tables_from_words(page: Any) -> list[list[list[str]]]:
    """
    Extract tables from a PDF page by analyzing word positions.
    This handles borderless tables where words are aligned in columns.

    This function is designed for structured tabular data (like invoices),
    not for multi-column text layouts in scientific documents.
    """
    words = page.extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)
    if not words:
        return []

    # Group words by their Y position (rows)
    y_tolerance = 5
    rows_by_y: dict[float, list[dict]] = {}
    for word in words:
        y_key = round(word["top"] / y_tolerance) * y_tolerance
        if y_key not in rows_by_y:
            rows_by_y[y_key] = []
        rows_by_y[y_key].append(word)

    # Sort rows by Y position
    sorted_y_keys = sorted(rows_by_y.keys())

    # Find potential column boundaries by analyzing x positions across all rows
    all_x_positions = []
    for words_in_row in rows_by_y.values():
        for word in words_in_row:
            all_x_positions.append(word["x0"])

    if not all_x_positions:
        return []

    # Cluster x positions to find column starts
    all_x_positions.sort()
    x_tolerance_col = 20
    column_starts: list[float] = []
    for x in all_x_positions:
        if not column_starts or x - column_starts[-1] > x_tolerance_col:
            column_starts.append(x)

    # Need at least 3 columns but not too many (likely text layout, not table)
    if len(column_starts) < 3 or len(column_starts) > 10:
        return []

    # Find rows that span multiple columns (potential table rows)
    table_rows = []
    for y_key in sorted_y_keys:
        words_in_row = sorted(rows_by_y[y_key], key=lambda w: w["x0"])

        # Assign words to columns
        row_data = [""] * len(column_starts)
        for word in words_in_row:
            # Find the closest column
            best_col = 0
            min_dist = float("inf")
            for i, col_x in enumerate(column_starts):
                dist = abs(word["x0"] - col_x)
                if dist < min_dist:
                    min_dist = dist
                    best_col = i

            if row_data[best_col]:
                row_data[best_col] += " " + word["text"]
            else:
                row_data[best_col] = word["text"]

        # Only include rows that have content in multiple columns
        non_empty = sum(1 for cell in row_data if cell.strip())
        if non_empty >= 2:
            table_rows.append(row_data)

    # Validate table quality - tables should have:
    # 1. Enough rows (at least 3 including header)
    # 2. Short cell content (tables have concise data, not paragraphs)
    # 3. Consistent structure across rows
    if len(table_rows) < 3:
        return []

    # Check if cells contain short, structured data (not long text)
    long_cell_count = 0
    total_cell_count = 0
    for row in table_rows:
        for cell in row:
            if cell.strip():
                total_cell_count += 1
                # If cell has more than 30 chars, it's likely prose text
                if len(cell.strip()) > 30:
                    long_cell_count += 1

    # If more than 30% of cells are long, this is probably not a table
    if total_cell_count > 0 and long_cell_count / total_cell_count > 0.3:
        return []

    return [table_rows]


# Pages with at least this many math-font characters trigger the
# "formulas may be garbled" warning.
_MATH_WARNING_MIN_CHARS = 3
# Above this many scanned pages, the scan warnings are merged into one.
_MAX_SCAN_WARNINGS = 5


def _limit(kwargs: Dict[str, Any], name: str, default: int) -> int:
    """A non-negative integer limit from kwargs; 0 means unlimited."""
    value = kwargs.get(name)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}") from None
    if number < 0:
        raise ValueError(f"{name} must be 0 (unlimited) or more, got {number}")
    return number


@dataclass
class _Options:
    """Conversion options taken from convert() keyword arguments."""

    page_markers: bool = False
    describe_images: bool = False
    transcribe_formulas: bool = False
    llm_client: Any = None
    llm_model: Optional[str] = None
    llm_prompt: Optional[str] = None
    max_render_pixels: int = DEFAULT_MAX_RENDER_PIXELS
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES

    @classmethod
    def from_kwargs(cls, kwargs: Dict[str, Any]) -> "_Options":
        return cls(
            page_markers=bool(kwargs.get("page_markers", False)),
            describe_images=bool(kwargs.get("describe_images", False)),
            transcribe_formulas=bool(kwargs.get("transcribe_formulas", False)),
            llm_client=kwargs.get("llm_client"),
            llm_model=kwargs.get("llm_model"),
            llm_prompt=kwargs.get("llm_prompt"),
            max_render_pixels=_limit(
                kwargs, "max_render_pixels", DEFAULT_MAX_RENDER_PIXELS
            ),
            max_llm_calls=_limit(kwargs, "max_llm_calls", DEFAULT_MAX_LLM_CALLS),
            max_pdf_pages=_limit(kwargs, "max_pdf_pages", DEFAULT_MAX_PDF_PAGES),
        )

    @property
    def has_llm(self) -> bool:
        return self.llm_client is not None and bool(self.llm_model)

    @property
    def can_describe(self) -> bool:
        return self.describe_images and self.has_llm

    @property
    def can_transcribe(self) -> bool:
        return self.transcribe_formulas and self.has_llm


@dataclass
class _PageRecord:
    """What one page produced; pages are closed before Markdown is assembled.

    kind is "form" (legacy form heuristics), "structured" (tables, images or
    formulas placed among text lines) or "plain" (text only).
    """

    index: int
    kind: str
    text: str = ""
    lines: List[TextLine] = field(default_factory=list)
    blocks: List[Block] = field(default_factory=list)


@dataclass
class _State:
    """Document-wide data gathered while walking the pages."""

    sizes: Counter = field(default_factory=Counter)
    scan_pages: List[int] = field(default_factory=list)
    math_pages: List[int] = field(default_factory=list)
    assets: List[DocumentAsset] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    visual: VisualBudget = field(default_factory=VisualBudget)


def _page_list(pages: List[int]) -> str:
    """'1-3, 7' style list of page numbers."""
    spans: List[str] = []
    start = prev = pages[0]
    for p in pages[1:] + [None]:  # type: ignore[list-item]
        if p is not None and p == prev + 1:
            prev = p
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        if p is not None:
            start = prev = p
    return ", ".join(spans)


def _scan_warnings(pages: List[int]) -> List[str]:
    hint = "Try the markitforge-ocr plugin."
    if not pages:
        return []
    if len(pages) <= _MAX_SCAN_WARNINGS:
        return [f"Page {p} has no text layer; it may be a scan. {hint}" for p in pages]
    return [f"Pages {_page_list(pages)} have no text layer; they may be scans. {hint}"]


def _check_page_count(pdf_bytes: io.BytesIO, max_pages: int) -> None:
    """Refuse documents with more than max_pages pages (0 = unlimited).

    Pages are counted lazily, so the check stops after max_pages + 1.
    """
    if max_pages <= 0:
        return
    try:
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser
    except ImportError:  # docling-only installs
        return
    pos = pdf_bytes.tell()
    try:
        pdf_bytes.seek(0)
        doc = PDFDocument(PDFParser(pdf_bytes))
        count = 0
        for _page in PDFPage.create_pages(doc):
            count += 1
            if count > max_pages:
                raise ResourceLimitException(
                    f"The PDF has more than {max_pages} pages "
                    "(max_pdf_pages); refusing to convert it."
                )
    except ResourceLimitException:
        raise
    except Exception as exc:
        # Unreadable here means unreadable for the backends too; let them
        # report it.
        logger.debug("Could not count PDF pages: %s", exc, exc_info=True)
    finally:
        pdf_bytes.seek(pos)


def _split_pdfminer_pages(raw: str) -> List[str]:
    """pdfminer ends every page with a form feed; split on it."""
    parts = raw.split("\x0c")
    if len(parts) > 1 and not parts[-1].strip():
        parts = parts[:-1]
    return parts


class PdfConverter(DocumentConverter):
    """
    Converts PDFs to Markdown.

    Finds ruled (and clearly aligned) tables on every page, infers headings
    from font size, keeps the form-style heuristics for form pages, and fills
    ``pages``, ``metadata`` and ``warnings`` on the result. Optional LLM
    features describe images and transcribe formulas to LaTeX.
    """

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        backend = str(kwargs.get("pdf_backend") or "auto").strip().lower()
        if backend not in PDF_BACKENDS:
            raise ValueError(
                f"Unknown pdf_backend {backend!r}; use one of: "
                + ", ".join(PDF_BACKENDS)
            )

        if backend != "docling" and _dependency_exc_info is not None:
            raise MissingDependencyException(
                MISSING_DEPENDENCY_MESSAGE.format(
                    converter=type(self).__name__,
                    extension=".pdf",
                    feature="pdf",
                )
            ) from _dependency_exc_info[1].with_traceback(_dependency_exc_info[2])  # type: ignore[union-attr]

        assert isinstance(file_stream, io.IOBase)

        # Read file stream into BytesIO for compatibility with pdfplumber
        pdf_bytes = io.BytesIO(file_stream.read())
        opts = _Options.from_kwargs(kwargs)
        _check_page_count(pdf_bytes, opts.max_pdf_pages)

        if backend == "docling":
            return self._convert_docling(pdf_bytes, opts)
        if backend == "pdfminer":
            return self._convert_pdfminer(pdf_bytes, opts, [])

        try:
            return self._convert_pdfplumber(pdf_bytes, opts)
        except (MissingDependencyException, ResourceLimitException):
            raise
        except Exception as exc:
            if backend == "pdfplumber":
                raise
            logger.warning(
                "pdfplumber could not analyse this PDF (%s: %s); "
                "falling back to pdfminer plain text.",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            warning = (
                f"Layout analysis failed ({type(exc).__name__}: {exc}); fell back "
                "to plain text extraction with pdfminer, so tables and headings "
                "may be missing."
            )
            return self._convert_pdfminer(pdf_bytes, opts, [warning])

    # -- backends ---------------------------------------------------------

    def _convert_pdfminer(
        self, pdf_bytes: io.BytesIO, opts: _Options, warnings: List[str]
    ) -> DocumentConverterResult:
        """Plain text for the whole document via pdfminer."""
        pdf_bytes.seek(0)
        raw = pdfminer.high_level.extract_text(pdf_bytes)
        metadata = self._read_metadata(pdf_bytes)
        sections = [
            DocumentSection(
                index=i + 1,
                markdown=neutralize_page_markers(
                    _merge_partial_numbering_lines(p.strip())
                ),
            )
            for i, p in enumerate(_split_pdfminer_pages(raw))
        ]
        if opts.page_markers:
            markdown = join_sections(sections, markers=True)
        else:
            markdown = _merge_partial_numbering_lines(raw)
        return DocumentConverterResult(
            markdown=markdown,
            title=metadata.get("title"),
            metadata=metadata,
            pages=sections,
            warnings=warnings,
        )

    def _convert_docling(
        self, pdf_bytes: io.BytesIO, opts: _Options
    ) -> DocumentConverterResult:
        """Delegate to docling (optional dependency)."""
        markdown, sections = convert_with_docling(pdf_bytes.getvalue())
        for section in sections:
            section.markdown = neutralize_page_markers(section.markdown)
        metadata = self._read_metadata(pdf_bytes)
        if opts.page_markers:
            markdown = join_sections(sections, markers=True)
        return DocumentConverterResult(
            markdown=markdown,
            title=metadata.get("title"),
            metadata=metadata,
            pages=sections,
        )

    def _read_metadata(self, pdf_bytes: io.BytesIO) -> Dict[str, Any]:
        try:
            info, page_count = read_metadata(pdf_bytes)
            return build_metadata(info, page_count)
        except Exception as exc:
            logger.debug("Could not read PDF metadata: %s", exc, exc_info=True)
            return {}

    def _convert_pdfplumber(
        self, pdf_bytes: io.BytesIO, opts: _Options
    ) -> DocumentConverterResult:
        """Layout-aware conversion, one page at a time."""
        state = _State(
            visual=VisualBudget(
                max_llm_calls=opts.max_llm_calls,
                max_render_pixels=opts.max_render_pixels,
            )
        )
        records: List[_PageRecord] = []
        # page.close() after each page frees pdfplumber's cached objects and
        # keeps memory flat regardless of page count; only text survives.
        with pdfplumber.open(pdf_bytes) as pdf:
            info = dict(pdf.metadata or {})
            for page_idx, page in enumerate(pdf.pages):
                try:
                    records.append(self._process_page(page, page_idx + 1, opts, state))
                finally:
                    page.close()  # Free cached page data immediately

        body = body_font_size(state.sizes)
        runs = {
            r.index: heading_runs(r.lines, body) for r in records if r.kind != "form"
        }
        levels = HeadingLevels(
            first.style_key
            for page_runs in runs.values()
            for _s, _e, first in page_runs
        )

        def headings_for(rec: _PageRecord) -> List[Tuple[str, int]]:
            return [
                (
                    " ".join(line.text for line in rec.lines[start:end]),
                    levels.level(first.style_key),
                )
                for start, end, first in runs.get(rec.index, [])
            ]

        # Text for plain pages. Without form pages, pdfminer gives better word
        # spacing; documents with form pages keep pdfplumber text.
        plain = [r for r in records if r.kind == "plain"]
        all_plain = bool(records) and len(plain) == len(records)
        has_form = any(r.kind == "form" for r in records)
        texts: Dict[int, str] = {r.index: r.text for r in plain}
        raw: Optional[str] = None
        parts: List[str] = []
        if plain and not has_form:
            pdf_bytes.seek(0)
            if all_plain:
                raw = pdfminer.high_level.extract_text(pdf_bytes)
            else:
                raw = pdfminer.high_level.extract_text(
                    pdf_bytes, page_numbers=[r.index - 1 for r in plain]
                )
            parts = raw.split("\x0c")
            if len(parts) < len(plain):
                logger.debug(
                    "pdfminer returned %d page(s) for %d plain page(s)",
                    len(parts),
                    len(plain),
                )
            for i, rec in enumerate(plain):
                if i < len(parts):
                    texts[rec.index] = parts[i]
        for rec in plain:
            texts[rec.index] = annotate_headings(texts[rec.index], headings_for(rec))

        sections: List[DocumentSection] = []
        chunks: List[str] = []
        for rec in records:
            if rec.kind == "form":
                md = rec.text
            elif rec.kind == "structured":
                md = render_page(rec.lines, rec.blocks, body, levels)
            else:
                md = texts[rec.index]
            chunks.append(md if rec.kind == "form" else md.strip())
            sections.append(
                DocumentSection(
                    index=rec.index,
                    # Page text must not forge the markers join_sections adds.
                    markdown=neutralize_page_markers(
                        _merge_partial_numbering_lines(md.strip())
                    ),
                    kind="page",
                )
            )

        if all_plain and raw is not None:
            # Same text as plain pdfminer extraction, plus any headings.
            for i, rec in enumerate(plain):
                if i < len(parts):
                    parts[i] = texts[rec.index]
            markdown = "\x0c".join(parts)
        else:
            markdown = "\n\n".join(c for c in chunks if c.strip()).strip()
            if not markdown and raw is None:
                logger.debug("No text from pdfplumber; trying pdfminer.")
                pdf_bytes.seek(0)
                markdown = pdfminer.high_level.extract_text(pdf_bytes)

        # Merge MasterFormat-style partial numbering with the following text
        markdown = _merge_partial_numbering_lines(markdown)
        if opts.page_markers:
            markdown = join_sections(sections, markers=True)

        metadata = build_metadata(info, len(records))
        warnings = _scan_warnings(state.scan_pages) + state.warnings
        warnings.extend(self._option_warnings(opts, state))
        return DocumentConverterResult(
            markdown=markdown,
            title=metadata.get("title"),
            metadata=metadata,
            pages=sections,
            assets=state.assets,
            warnings=warnings,
        )

    # -- per page -----------------------------------------------------------

    def _process_page(
        self, page: Any, page_no: int, opts: _Options, state: _State
    ) -> _PageRecord:
        """Analyse one open page; everything that needs the page happens here."""
        visible = [c for c in page.chars if str(c.get("text", "")).strip()]
        state.sizes.update(round(float(c.get("size", 0.0)), 1) for c in visible)
        if not visible and page.images:
            state.scan_pages.append(page_no)
        math_chars = sum(1 for c in visible if is_math_font(c.get("fontname", "")))
        if math_chars >= _MATH_WARNING_MIN_CHARS:
            state.math_pages.append(page_no)

        form_content = _extract_form_content_from_words(page)
        if form_content is not None:
            if opts.can_describe:
                images = self._describe_images(page, page_no, opts, state)
                if images:
                    form_content = "\n\n".join(
                        [form_content.rstrip()] + [b.markdown for b in images]
                    )
            return _PageRecord(page_no, "form", text=form_content)

        tables = find_page_tables(page)
        text_page = page
        if tables:
            bboxes = [t.bbox for t in tables]
            text_page = page.filter(lambda obj: not point_in_bboxes(obj, bboxes))
        lines = extract_lines(text_page)
        blocks = [
            Block(top=t.bbox[1], x0=t.bbox[0], markdown=render_markdown_table(t.rows))
            for t in tables
        ]
        if opts.can_describe:
            blocks.extend(self._describe_images(page, page_no, opts, state))
        if math_chars and opts.can_transcribe:
            regions = find_math_regions(lines)
            done = transcribe_math_regions(
                page,
                page_no,
                regions,
                client=opts.llm_client,
                model=str(opts.llm_model),
                caption_fn=llm_caption,
                warnings=state.warnings,
                budget=state.visual,
            )
            replaced: set[int] = set()
            for region, block in done:
                replaced.update(range(region.start, region.end))
                blocks.append(block)
            lines = [line for i, line in enumerate(lines) if i not in replaced]

        if blocks:
            return _PageRecord(page_no, "structured", lines=lines, blocks=blocks)
        text = page.extract_text()
        return _PageRecord(page_no, "plain", text=(text or "").strip(), lines=lines)

    def _describe_images(
        self, page: Any, page_no: int, opts: _Options, state: _State
    ) -> List[Block]:
        return describe_page_images(
            page,
            page_no,
            client=opts.llm_client,
            model=str(opts.llm_model),
            prompt=opts.llm_prompt,
            caption_fn=llm_caption,
            assets=state.assets,
            warnings=state.warnings,
            budget=state.visual,
        )

    @staticmethod
    def _option_warnings(opts: _Options, state: _State) -> List[str]:
        warnings: List[str] = []
        if opts.describe_images and not opts.has_llm:
            warnings.append(
                "describe_images needs llm_client and llm_model; "
                "images were not described."
            )
        if state.math_pages and not opts.can_transcribe:
            pages = _page_list(state.math_pages)
            if opts.transcribe_formulas:
                hint = "transcribe_formulas needs llm_client and llm_model."
            else:
                hint = "Pass transcribe_formulas=True with an llm_client to get LaTeX."
            warnings.append(
                f"Math fonts found on page(s) {pages}; formulas may be garbled "
                f"in the text. {hint}"
            )
        return warnings
