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

Layout options of the pdfplumber analysis (all on by default):
- ``pdf_reading_order``: "xycut" (recursive XY-cut over word boxes; handles
  multi-column pages) or "simple" (top to bottom, left to right).
- ``pdf_reflow``: join hard-wrapped lines into paragraphs and undo
  end-of-line hyphenation.
- ``pdf_strip_headers_footers``: drop running headers and footers; what was
  removed is listed in ``metadata["removed_headers_footers"]``.
- ``pdf_outline_headings``: take headings from the PDF outline (bookmarks)
  or heading tags when the document has them.
- ``pdf_merge_tables``: merge tables continued on the next page (in the
  joined Markdown; ``result.pages`` keeps each page's own rows).

Other options: ``table_format`` ("markdown", "html" or "auto": HTML only
for tables with merged cells) and ``pdf_inline_math`` (off by default:
write inline formulas as ``$...$``).

Images and scans (all off by default):
- ``extract_images``: return every distinct embedded image as a PNG asset
  (``page3_img1.png``) and link it where it sits in the reading order; the
  alt text is the LLM caption when ``describe_images`` produced one.
- ``ocr_scanned_pages``: recognise the text of pages without a usable text
  layer (scans, garbled or near-empty layers over a page image) with
  ``ocr_engine`` "llm" (``llm_client``/``llm_model``), "tesseract"
  (pytesseract) or "auto" (the LLM when configured, else tesseract when
  installed). OCR'd pages are listed in ``metadata["ocr_pages"]``.

Progress (``progress``) is reported per page and per LLM call, and
``cancel_event`` is checked before every page.

``pdf_reading_order="simple"`` with ``pdf_reflow=False`` gives the earlier
line-by-line output; ``pdf_backend="pdfminer"`` skips layout analysis
altogether.
"""

import hashlib
import io
import logging
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Dict, List, Optional, Sequence, Tuple

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
    ConversionCancelled,
    MissingDependencyException,
    ResourceLimitException,
)
from .._progress import check_cancelled, report_progress
from ..converter_utils._text_safety import neutralize_page_markers
from ..converter_utils.pdf._docling import convert_with_docling
from ..converter_utils.pdf import _tables as _pdf_tables
from ..converter_utils.pdf._furniture import (
    MIN_PAGES as FURNITURE_MIN_PAGES,
    PageLines,
    find_furniture,
    strip_furniture,
    strip_furniture_text,
)
from ..converter_utils.pdf._layout import (
    Block,
    Flow,
    OutlineEntry,
    TextLine,
    annotate_headings,
    assign_headings,
    extract_lines,
    heading_list,
    is_math_font,
    simple_flow,
)
from ..converter_utils.pdf._ocr import (
    LLM_OCR_RESOLUTION,
    OCR_ENGINES,
    OCR_PROMPT,
    TESSERACT_RESOLUTION,
    clean_ocr_text,
    ocr_warning,
    resolve_engine,
    tesseract_text,
)
from ..converter_utils.pdf._metadata import (
    build_metadata,
    read_metadata,
    read_outline,
)
from ..converter_utils.pdf._reading_order import layout_page
from ..converter_utils.pdf._reflow import build_vocabulary, render_flow
from ..converter_utils.pdf._tables import (
    PageTable,
    find_page_tables,
    point_in_bboxes,
    render_table,
)
from ..converter_utils.pdf._visual import (
    DEFAULT_MAX_LLM_CALLS,
    DEFAULT_MAX_RENDER_PIXELS,
    RegionTooLarge,
    VisualBudget,
    apply_inline_math,
    find_math_regions,
    page_image_blocks,
    page_images,
    render_region_png,
    transcribe_math_regions,
)
from ._llm_caption import llm_caption

logger = logging.getLogger(__name__)

PDF_BACKENDS = ("auto", "pdfplumber", "pdfminer", "docling")
PDF_READING_ORDERS = ("xycut", "simple")

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


_FALSE_WORDS = {"", "0", "false", "no", "off"}


def _flag(kwargs: Dict[str, Any], name: str, default: bool) -> bool:
    """A boolean option; strings such as "false", "0" or "off" mean False."""
    value = kwargs.get(name)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return bool(value)


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
    reading_order: str = "xycut"
    reflow: bool = True
    strip_furniture: bool = True
    outline_headings: bool = True
    merge_tables: bool = True
    table_format: str = "markdown"
    inline_math: bool = False
    extract_images: bool = False
    ocr_scanned_pages: bool = False
    ocr_engine: str = "auto"
    ocr_lang: Optional[str] = None
    # Only "progress" and "cancel_event", for report_progress/check_cancelled.
    hooks: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_kwargs(cls, kwargs: Dict[str, Any]) -> "_Options":
        order = str(kwargs.get("pdf_reading_order") or "xycut").strip().lower()
        if order not in PDF_READING_ORDERS:
            raise ValueError(
                f"Unknown pdf_reading_order {order!r}; use one of: "
                + ", ".join(PDF_READING_ORDERS)
            )
        engine = str(kwargs.get("ocr_engine") or "auto").strip().lower()
        if engine not in OCR_ENGINES:
            raise ValueError(
                f"Unknown ocr_engine {engine!r}; use one of: " + ", ".join(OCR_ENGINES)
            )
        lang = kwargs.get("ocr_lang")
        if lang is not None and not re.fullmatch(r"[A-Za-z0-9_+]{1,64}", str(lang)):
            raise ValueError(f"Bad ocr_lang {lang!r}; use e.g. 'eng' or 'eng+deu'")
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
            reading_order=order,
            reflow=_flag(kwargs, "pdf_reflow", True),
            strip_furniture=_flag(kwargs, "pdf_strip_headers_footers", True),
            outline_headings=_flag(kwargs, "pdf_outline_headings", True),
            merge_tables=_flag(kwargs, "pdf_merge_tables", True),
            table_format=str(kwargs.get("table_format") or "markdown"),
            inline_math=_flag(kwargs, "pdf_inline_math", False),
            extract_images=_flag(kwargs, "extract_images", False),
            ocr_scanned_pages=_flag(kwargs, "ocr_scanned_pages", False),
            ocr_engine=engine,
            ocr_lang=str(lang) if lang is not None else None,
            hooks={k: kwargs.get(k) for k in ("progress", "cancel_event")},
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

    @property
    def wants_images(self) -> bool:
        """Whether image links are placed in the output at all."""
        return self.can_describe or self.extract_images

    @property
    def legacy_text(self) -> bool:
        """Line-by-line output as before reading order and reflow existed:
        plain pages take their text from pdfminer."""
        return self.reading_order == "simple" and not self.reflow


@dataclass
class _PageRecord:
    """What one page produced; pages are closed before Markdown is assembled.

    kind is "form" (legacy form heuristics), "ocr" (text recognised from the
    rendered page), "structured" (tables, images or formulas placed among
    text lines) or "plain" (text only). ``flow`` holds the lines and blocks
    of a form-less, OCR-less page in reading order; "form" and "ocr" pages
    keep their final Markdown in ``text``.
    """

    index: int
    kind: str
    text: str = ""
    flow: Flow = field(default_factory=list)
    height: float = 0.0


@dataclass
class _State:
    """Document-wide data gathered while walking the pages."""

    sizes: Counter = field(default_factory=Counter)
    scan_pages: List[int] = field(default_factory=list)
    garbled_pages: List[int] = field(default_factory=list)
    sparse_pages: List[int] = field(default_factory=list)
    math_pages: List[int] = field(default_factory=list)
    assets: List[DocumentAsset] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    visual: VisualBudget = field(default_factory=VisualBudget)
    # OCR: the engine in use (None = no OCR), pages done, answers by digest.
    ocr_engine: Optional[str] = None
    ocr_pages: List[int] = field(default_factory=list)
    ocr_cache: Dict[str, str] = field(default_factory=dict)
    ocr_skipped: List[int] = field(default_factory=list)


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


_OCR_HINT = "Use ocr_scanned_pages=True (CLI --ocr) or the markitforge-ocr plugin."


def _layer_warnings(pages: List[int], one: str, many: str) -> List[str]:
    """One warning per page, or a single merged one for many pages."""
    if not pages:
        return []
    if len(pages) <= _MAX_SCAN_WARNINGS:
        return [f"Page {p} {one} {_OCR_HINT}" for p in pages]
    return [f"Pages {_page_list(pages)} {many} {_OCR_HINT}"]


def _scan_warnings(pages: List[int]) -> List[str]:
    return _layer_warnings(
        pages,
        "has no text layer; it may be a scan.",
        "have no text layer; they may be scans.",
    )


def _garbled_warnings(pages: List[int]) -> List[str]:
    return _layer_warnings(
        pages,
        "has an unreadable text layer (garbled characters); it may be a scan.",
        "have unreadable text layers (garbled characters); they may be scans.",
    )


def _sparse_warnings(pages: List[int]) -> List[str]:
    return _layer_warnings(
        pages,
        "has almost no text over a full-page image; it may be a scan.",
        "have almost no text over full-page images; they may be scans.",
    )


# A text layer is garbled when at least this share of its characters (and at
# least _GARBLED_MIN_CHARS of them) are unmapped glyphs, replacement
# characters, controls or private-use code points.
_GARBLED_SHARE = 0.5
_GARBLED_MIN_CHARS = 10
# A page is a scan with a stray text layer when one image covers this share
# of it while all characters together cover less than _SPARSE_TEXT_SHARE.
_FULL_PAGE_IMAGE_SHARE = 0.8
_SPARSE_TEXT_SHARE = 0.005
_BAD_CATEGORIES = {"Cc", "Cs", "Co", "Cn"}


def _is_garbled(texts: Sequence[str]) -> bool:
    """Whether the visible characters of a page are mostly unreadable."""
    if len(texts) < _GARBLED_MIN_CHARS:
        return False
    cache: Dict[str, bool] = {}
    bad = 0
    for text in texts:
        flag = cache.get(text)
        if flag is None:
            flag = (
                text.startswith("(cid:")
                or "\ufffd" in text
                or any(unicodedata.category(ch) in _BAD_CATEGORIES for ch in text)
            )
            cache[text] = flag
        bad += flag
    return bad >= _GARBLED_SHARE * len(texts)


def _is_sparse_over_image(page: Any, visible: Sequence[Dict[str, Any]]) -> bool:
    """A full-page image with only a sliver of text on top (a stamp, a
    page number): the real content is in the picture."""
    try:
        width, height = float(page.width), float(page.height)
        area = width * height
        if area <= 0:
            return False
        largest = 0.0
        for img in page.images:
            w = min(float(img["x1"]), width) - max(float(img["x0"]), 0.0)
            h = min(float(img["bottom"]), height) - max(float(img["top"]), 0.0)
            largest = max(largest, max(w, 0.0) * max(h, 0.0))
        if largest < _FULL_PAGE_IMAGE_SHARE * area:
            return False
        text_area = sum(
            (float(c["x1"]) - float(c["x0"])) * (float(c["bottom"]) - float(c["top"]))
            for c in visible
        )
    except (KeyError, TypeError, ValueError):
        return False
    return text_area < _SPARSE_TEXT_SHARE * area


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


def _place_formulas(
    flow: Flow, lines: Sequence[TextLine], done: Sequence[Tuple[Any, Block]]
) -> Flow:
    """Put transcribed formulas where their lines were in the flow."""
    replace: Dict[int, Optional[Block]] = {}
    for region, block in done:
        covered = lines[region.start : region.end]
        if not covered:
            continue
        block.x1 = max(line.x1 for line in covered)
        block.bottom = max(line.bottom for line in covered)
        replace[id(covered[0])] = block
        for line in covered[1:]:
            replace[id(line)] = None
    out: Flow = []
    for item in flow:
        if id(item) in replace:
            block_or_none = replace[id(item)]
            if block_or_none is not None:
                out.append(block_or_none)
        else:
            out.append(item)
    return out


def _replace_lines(
    flow: Flow, old: Sequence[TextLine], new: Sequence[TextLine]
) -> Flow:
    """Swap rewritten lines into a flow.

    ``new`` is ``old`` with some lines replaced by fresh objects and some
    dropped (folded into a neighbour), in the same order. A replacement takes
    the place of the first old line it covers.
    """
    kept = {id(x) for x in new}
    known = {id(x) for x in old}
    fresh = [x for x in new if id(x) not in known]
    placed: Dict[int, TextLine] = {}
    used = set()
    for line in old:
        if id(line) in kept:
            continue
        cx, cy = (line.x0 + line.x1) / 2, (line.top + line.bottom) / 2
        for k, cand in enumerate(fresh):
            if (
                k not in used
                and cand.x0 - 1 <= cx <= cand.x1 + 1
                and cand.top - 1 <= cy <= cand.bottom + 1
            ):
                placed[id(line)] = cand
                used.add(k)
                break
    out: Flow = []
    for item in flow:
        if not isinstance(item, TextLine) or id(item) in kept or id(item) not in known:
            out.append(item)
        elif id(item) in placed:
            out.append(placed[id(item)])
    # Anything that could not be placed keeps its text at the end of the page.
    out.extend(cand for k, cand in enumerate(fresh) if k not in used)
    return out


# A detected table with a cell longer than this is a layout frame around
# prose, not data; such pages keep the form heuristics.
_MAX_DATA_CELL_CHARS = 80
_SEPARATOR_ROW_RE = re.compile(r"^\|[\s|:-]+\|$")


def _tables_cover_form(form_markdown: str, tables: Sequence[PageTable]) -> bool:
    """Whether the detected tables account for every table row the form
    heuristics produced.

    The form heuristics turn any aligned rows into tables, which suits real
    forms (label/value grids all over the page). On a page whose only
    tabular content is the tables found by table detection, the detected
    tables are the better rendering (wrapped cells and merged headers stay
    in their rows) and the rest of the page is laid out as text.
    """
    if not tables:
        return False
    cells = [cell for t in tables for row in t.rows for cell in row if cell]
    if not cells or max(len(cell) for cell in cells) > _MAX_DATA_CELL_CHARS:
        return False
    normalised = [" ".join(cell.replace("\\|", "|").split()) for cell in cells]
    exact = set(normalised)
    for line in form_markdown.splitlines():
        line = line.strip()
        if not line.startswith("|") or _SEPARATOR_ROW_RE.match(line):
            continue
        for cell in line.strip("|").split("|"):
            text = " ".join(cell.split())
            if text and not _cell_covered(text, exact, normalised):
                return False
    return True


def _cell_covered(text: str, exact: set, cells: Sequence[str]) -> bool:
    """Whether a form cell is one of the table cells, or (for text of three
    characters or more) a whole-word part of one, as when a wrapped cell was
    split across form rows. "1" or "No" never match inside "100" or "Notes"."""
    if text in exact:
        return True
    if len(text) < 3:
        return False
    pattern = re.compile(r"(?<!\w)" + re.escape(text) + r"(?!\w)")
    return any(pattern.search(cell) for cell in cells)


class PdfConverter(DocumentConverter):
    """
    Converts PDFs to Markdown.

    Finds ruled (and clearly aligned) tables on every page, orders text with
    a recursive XY-cut, rebuilds paragraphs, drops running headers and
    footers, takes headings from the outline or infers them from fonts and
    spacing, keeps the form-style heuristics for form pages, and fills
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
        check_cancelled(opts.hooks)

        if backend == "docling":
            return self._convert_docling(pdf_bytes, opts)
        if backend == "pdfminer":
            return self._convert_pdfminer(pdf_bytes, opts, [])

        try:
            return self._convert_pdfplumber(pdf_bytes, opts)
        except (
            MissingDependencyException,
            ResourceLimitException,
            ConversionCancelled,
        ):
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
        self._report_all_pages(opts, len(sections))
        warnings = warnings + self._backend_option_warnings(opts, "pdfminer")
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
        self._report_all_pages(opts, len(sections))
        return DocumentConverterResult(
            markdown=markdown,
            title=metadata.get("title"),
            metadata=metadata,
            pages=sections,
            warnings=self._backend_option_warnings(opts, "docling"),
        )

    @staticmethod
    def _report_all_pages(opts: _Options, total: int) -> None:
        """Page events for backends that convert the document in one go."""
        for done in range(1, total + 1):
            report_progress(opts.hooks, "page", done, total)

    @staticmethod
    def _backend_option_warnings(opts: _Options, backend: str) -> List[str]:
        out: List[str] = []
        if opts.extract_images:
            out.append(
                f"extract_images is not supported by the {backend} backend; "
                "no images were exported."
            )
        if opts.ocr_scanned_pages:
            out.append(
                f"ocr_scanned_pages is not supported by the {backend} backend; "
                "scanned pages were not recognised."
            )
        return out

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
        """Layout-aware conversion in one pass over the pages."""
        hooks = opts.hooks

        def on_llm_call(count: int) -> None:
            report_progress(hooks, "llm", count, None, f"LLM call {count}")

        state = _State(
            visual=VisualBudget(
                max_llm_calls=opts.max_llm_calls,
                max_render_pixels=opts.max_render_pixels,
                on_llm_call=on_llm_call,
            )
        )
        if opts.ocr_scanned_pages:
            state.ocr_engine = resolve_engine(opts.ocr_engine, opts.has_llm)
        records: List[_PageRecord] = []
        outline: List[OutlineEntry] = []
        # page.close() after each page frees pdfplumber's cached objects and
        # keeps memory flat regardless of page count; only text lines survive.
        with pdfplumber.open(pdf_bytes) as pdf:
            info = dict(pdf.metadata or {})
            page_ids: Dict[int, int] = {}
            total = len(pdf.pages)
            for page_idx, page in enumerate(pdf.pages):
                check_cancelled(hooks)
                try:
                    page_id = getattr(getattr(page, "page_obj", None), "pageid", None)
                    if page_id is not None:
                        page_ids[page_id] = page_idx + 1
                    records.append(self._process_page(page, page_idx + 1, opts, state))
                finally:
                    page.close()  # Free cached page data immediately
                report_progress(hooks, "page", page_idx + 1, total)
            if opts.outline_headings:
                outline = [
                    OutlineEntry(level, title, page_no)
                    for level, title, page_no in read_outline(pdf.doc, page_ids)
                ]

        text_records = [r for r in records if r.kind not in ("form", "ocr")]
        flows: Dict[int, Flow] = {r.index: r.flow for r in text_records}

        # Running headers and footers, judged over the whole document.
        furniture: Dict[Tuple[str, str], str] = {}
        removed: List[str] = []
        if opts.strip_furniture and len(records) >= FURNITURE_MIN_PAGES:
            page_lines = [PageLines(r.index, r.height, r.flow) for r in text_records]
            furniture = find_furniture(page_lines)
            removed = strip_furniture(page_lines, furniture)

        assign_headings(flows, state.sizes, outline)

        texts: Dict[int, str] = {}
        raw: Optional[str] = None
        parts: List[str] = []
        all_plain = False
        if opts.legacy_text:
            raw, parts, all_plain = self._legacy_plain_texts(
                pdf_bytes, records, furniture, texts
            )
        vocab = None
        if opts.reflow:
            vocab = build_vocabulary(
                x.text
                for flow in flows.values()
                for x in flow
                if isinstance(x, TextLine)
            )

        sections: List[DocumentSection] = []
        chunks: List[str] = []
        page_texts: List[str] = []  # per page, before marker neutralising
        for rec in records:
            if rec.kind in ("form", "ocr"):
                md = rec.text
            elif rec.index in texts:
                md = texts[rec.index]
            else:
                md = render_flow(rec.flow, reflow=opts.reflow, vocab=vocab)
            chunks.append(md if rec.kind == "form" else md.strip())
            page_text = _merge_partial_numbering_lines(md.strip())
            page_texts.append(page_text)
            sections.append(
                DocumentSection(
                    index=rec.index,
                    # Page text must not forge the markers join_sections adds.
                    markdown=neutralize_page_markers(page_text),
                    kind="page",
                )
            )

        if all_plain and raw is not None:
            # Same text as plain pdfminer extraction, plus any headings.
            plain = [r for r in records if r.kind == "plain"]
            for i, rec in enumerate(plain):
                if i < len(parts):
                    parts[i] = texts[rec.index]
            markdown = "\x0c".join(parts)
        else:
            markdown = "\n\n".join(c for c in chunks if c.strip()).strip()
            if not markdown and raw is None and not state.sizes:
                logger.debug("No text from pdfplumber; trying pdfminer.")
                pdf_bytes.seek(0)
                markdown = pdfminer.high_level.extract_text(pdf_bytes)

        # Merge MasterFormat-style partial numbering with the following text
        markdown = _merge_partial_numbering_lines(markdown)

        if opts.merge_tables and not opts.page_markers:
            # Only the joined text gets continued tables merged: result.pages
            # keeps each page's own rows, so page selection stays exact, and
            # with page markers on the page boundaries stay visible.
            merged = self._merge_tables(
                [
                    DocumentSection(index=s.index, markdown=text, kind=s.kind)
                    for s, text in zip(sections, page_texts)
                ]
            )
            if merged is not None:
                markdown = "\n\n".join(
                    s.markdown.strip() for s in merged if s.markdown.strip()
                )
        if opts.page_markers:
            markdown = join_sections(sections, markers=True)

        metadata = build_metadata(info, len(records))
        if removed:
            metadata["removed_headers_footers"] = removed
        done = set(state.ocr_pages)
        warnings = (
            _scan_warnings([p for p in state.scan_pages if p not in done])
            + _garbled_warnings([p for p in state.garbled_pages if p not in done])
            + _sparse_warnings([p for p in state.sparse_pages if p not in done])
        )
        if opts.ocr_scanned_pages:
            metadata["ocr_pages"] = list(state.ocr_pages)
            if state.ocr_pages:
                metadata["ocr_engine"] = state.ocr_engine
                warnings.append(
                    ocr_warning(state.ocr_pages, str(state.ocr_engine), _page_list)
                )
        warnings.extend(state.warnings)
        warnings.extend(self._option_warnings(opts, state))
        return DocumentConverterResult(
            markdown=markdown,
            title=metadata.get("title"),
            metadata=metadata,
            pages=sections,
            assets=state.assets,
            warnings=warnings,
        )

    @staticmethod
    def _merge_tables(
        sections: List[DocumentSection],
    ) -> Optional[List[DocumentSection]]:
        """Merge tables continued across pages; None when nothing changed."""
        merge = getattr(_pdf_tables, "merge_continued_tables", None)
        if not callable(merge):
            return None
        before = [(s.index, s.markdown) for s in sections]
        try:
            merged = list(merge(sections))
        except Exception as exc:
            logger.debug("Could not merge continued tables: %s", exc, exc_info=True)
            return None
        if [(s.index, s.markdown) for s in merged] == before:
            return None
        return merged

    @staticmethod
    def _legacy_plain_texts(
        pdf_bytes: io.BytesIO,
        records: List[_PageRecord],
        furniture: Dict[Tuple[str, str], str],
        texts: Dict[int, str],
    ) -> Tuple[Optional[str], List[str], bool]:
        """Text of plain pages for line-by-line output (``legacy_text``).

        Without form pages, pdfminer gives better word spacing than
        pdfplumber's own text; documents with form pages keep pdfplumber
        text. Fills ``texts`` and returns (raw pdfminer text, its pages,
        whether every page is plain).
        """
        plain = [r for r in records if r.kind == "plain"]
        all_plain = bool(records) and len(plain) == len(records)
        has_form = any(r.kind == "form" for r in records)
        texts.update({r.index: r.text for r in plain})
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
            text = strip_furniture_text(texts[rec.index], furniture)
            texts[rec.index] = annotate_headings(text, heading_list(rec.flow))
        return raw, parts, all_plain

    # -- per page -----------------------------------------------------------

    def _process_page(
        self, page: Any, page_no: int, opts: _Options, state: _State
    ) -> _PageRecord:
        """Analyse one open page; everything that needs the page happens here."""
        chars = page.chars
        visible = [c for c in chars if str(c.get("text", "")).strip()]
        state.sizes.update(round(float(c.get("size", 0.0)), 1) for c in visible)
        no_text_layer = True
        if not visible:
            if page.images:
                state.scan_pages.append(page_no)
            else:
                no_text_layer = False
        elif _is_garbled([str(c.get("text", "")) for c in visible]):
            state.garbled_pages.append(page_no)
        elif page.images and _is_sparse_over_image(page, visible):
            state.sparse_pages.append(page_no)
        else:
            no_text_layer = False
        math_chars = sum(1 for c in visible if is_math_font(c.get("fontname", "")))
        if math_chars >= _MATH_WARNING_MIN_CHARS:
            state.math_pages.append(page_no)

        if no_text_layer and state.ocr_engine is not None:
            ocr_text = self._ocr_page(page, page_no, opts, state)
            if ocr_text is not None:
                if opts.extract_images:
                    # The scan image is the page just read: export it, but
                    # do not spend another LLM call captioning it.
                    images = self._describe_images(
                        page, page_no, opts, state, describe=False
                    )
                    ocr_text = "\n\n".join(
                        [ocr_text] + [b.markdown for b in images]
                    ).strip()
                return _PageRecord(page_no, "ocr", text=ocr_text)

        form_content = _extract_form_content_from_words(page)
        tables = find_page_tables(page)
        if form_content is not None and not _tables_cover_form(form_content, tables):
            if opts.wants_images:
                images = self._describe_images(page, page_no, opts, state)
                if images:
                    form_content = "\n\n".join(
                        [form_content.rstrip()] + [b.markdown for b in images]
                    )
            return _PageRecord(page_no, "form", text=form_content)

        bboxes = [t.bbox for t in tables]
        blocks = [
            Block(
                top=t.bbox[1],
                x0=t.bbox[0],
                markdown=render_table(t, opts.table_format),
                x1=t.bbox[2],
                bottom=t.bbox[3],
            )
            for t in tables
        ]
        if opts.wants_images:
            blocks.extend(self._describe_images(page, page_no, opts, state))

        text_chars = (
            [c for c in chars if not point_in_bboxes(c, bboxes)] if bboxes else chars
        )
        if opts.reading_order == "xycut":
            flow = layout_page(text_chars, self._with_image_boxes(page, blocks))
        else:
            text_page = page
            if bboxes:
                text_page = page.filter(lambda obj: not point_in_bboxes(obj, bboxes))
            flow = simple_flow(extract_lines(text_page), blocks)

        if opts.can_transcribe:
            lines = [x for x in flow if isinstance(x, TextLine)]
            done = transcribe_math_regions(
                page,
                page_no,
                find_math_regions(lines, text_chars),
                client=opts.llm_client,
                model=str(opts.llm_model),
                caption_fn=llm_caption,
                warnings=state.warnings,
                budget=state.visual,
            )
            flow = _place_formulas(flow, lines, done)
        n_inline = 0
        if opts.inline_math:
            lines = [x for x in flow if isinstance(x, TextLine) and not x.rotated]
            rewritten, n_inline = apply_inline_math(lines, text_chars)
            if n_inline:
                flow = _replace_lines(flow, lines, rewritten)

        structured = bool(n_inline) or any(
            isinstance(x, Block) and x.markdown.strip() for x in flow
        )
        text = ""
        if not structured and opts.legacy_text:
            text = (page.extract_text() or "").strip()
        return _PageRecord(
            page_no,
            "structured" if structured else "plain",
            text=text,
            flow=flow,
            height=float(page.height),
        )

    @staticmethod
    def _with_image_boxes(page: Any, blocks: List[Block]) -> List[Block]:
        """Blocks for the reading-order analysis.

        Described images get their full box, and images without a
        description are added as empty blocks: a figure between two groups
        of columns must separate them even when nothing is rendered for it.
        """
        try:
            boxes = page_images(page)
        except Exception as exc:
            logger.debug("Could not list page images: %s", exc, exc_info=True)
            return blocks
        out = list(blocks)
        for x0, top, x1, bottom in boxes:
            match = next(
                (
                    b
                    for b in blocks
                    if b.x1 is None and abs(b.x0 - x0) < 0.5 and abs(b.top - top) < 0.5
                ),
                None,
            )
            if match is not None:
                match.x1, match.bottom = x1, bottom
            else:
                out.append(Block(top=top, x0=x0, markdown="", x1=x1, bottom=bottom))
        return out

    def _describe_images(
        self,
        page: Any,
        page_no: int,
        opts: _Options,
        state: _State,
        describe: bool = True,
    ) -> List[Block]:
        """Image links (captioned and/or exported) for a page."""
        return page_image_blocks(
            page,
            page_no,
            describe=describe and opts.can_describe,
            export=opts.extract_images,
            client=opts.llm_client,
            model=str(opts.llm_model),
            prompt=opts.llm_prompt,
            caption_fn=llm_caption,
            assets=state.assets,
            warnings=state.warnings,
            budget=state.visual,
        )

    def _ocr_page(
        self, page: Any, page_no: int, opts: _Options, state: _State
    ) -> Optional[str]:
        """Recognised Markdown for a page without a usable text layer, or
        None when OCR failed or is over budget (the page keeps its scan
        warning)."""
        engine = state.ocr_engine
        budget = state.visual
        if engine == "llm" and budget.exhausted:
            state.ocr_skipped.append(page_no)
            return None
        check_cancelled(opts.hooks)
        resolution = LLM_OCR_RESOLUTION if engine == "llm" else TESSERACT_RESOLUTION
        try:
            bbox = tuple(float(v) for v in page.bbox)
            png = render_region_png(
                page,
                bbox,  # type: ignore[arg-type]
                resolution=resolution,
                max_pixels=opts.max_render_pixels,
            )
        except RegionTooLarge as exc:
            state.warnings.append(
                f"Could not OCR page {page_no}: it is too large to render "
                f"({exc}; see max_render_pixels)."
            )
            return None
        except Exception as exc:
            logger.warning("Could not render page %s: %s", page_no, exc, exc_info=True)
            state.warnings.append(f"Could not OCR page {page_no}: {exc}")
            return None
        if not png:
            return None
        key = f"{engine}:{hashlib.sha256(png).hexdigest()}"
        answer = state.ocr_cache.get(key)
        if answer is None:
            if engine == "llm" and not budget.can_call_llm(state.warnings):
                state.ocr_skipped.append(page_no)
                return None
            try:
                if engine == "llm":
                    raw = llm_caption(
                        io.BytesIO(png),
                        StreamInfo(mimetype="image/png", extension=".png"),
                        client=opts.llm_client,
                        model=str(opts.llm_model),
                        prompt=OCR_PROMPT,
                    )
                else:
                    raw = tesseract_text(png, opts.ocr_lang)
            except ConversionCancelled:
                raise
            except Exception as exc:
                logger.warning("OCR failed on page %s: %s", page_no, exc, exc_info=True)
                state.warnings.append(f"Could not OCR page {page_no}: {exc}")
                return None
            answer, truncated = clean_ocr_text(raw if isinstance(raw, str) else "")
            if truncated:
                state.warnings.append(
                    f"The OCR text of page {page_no} was cut to {len(answer)} "
                    "characters."
                )
            state.ocr_cache[key] = answer
        if not answer:
            return None
        state.ocr_pages.append(page_no)
        return answer

    @staticmethod
    def _option_warnings(opts: _Options, state: _State) -> List[str]:
        warnings: List[str] = []
        needs_ocr = state.scan_pages or state.garbled_pages or state.sparse_pages
        if opts.ocr_scanned_pages and needs_ocr and state.ocr_engine is None:
            if opts.ocr_engine == "llm":
                warnings.append(
                    "ocr_engine='llm' needs llm_client and llm_model; "
                    "scanned pages were not recognised."
                )
            else:
                warnings.append(
                    "ocr_scanned_pages found no OCR engine (pass llm_client and "
                    "llm_model, or install pytesseract and tesseract); scanned "
                    "pages were not recognised."
                )
        if state.ocr_skipped:
            warnings.append(
                f"Page(s) {_page_list(state.ocr_skipped)} were not OCR'd: the "
                f"LLM call budget (max_llm_calls={opts.max_llm_calls}) was spent."
            )
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
