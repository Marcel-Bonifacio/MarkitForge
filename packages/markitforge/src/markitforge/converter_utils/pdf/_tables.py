# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Table detection on PDF pages (pdfplumber) and Markdown table rendering."""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

BBox = Tuple[float, float, float, float]

# Ruled tables: cell borders drawn as lines or rectangles.
LINES_SETTINGS: Dict[str, Any] = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}

# Borderless tables: columns inferred from word alignment. This finds a lot
# of false positives on prose, so its results go through strict checks.
TEXT_SETTINGS: Dict[str, Any] = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "min_words_vertical": 3,
    "min_words_horizontal": 2,
}


@dataclass
class PageTable:
    """A table found on a page: its bounding box and cleaned cell text."""

    bbox: BBox
    rows: List[List[str]]

    @property
    def top(self) -> float:
        return self.bbox[1]


def _clean_cell(cell: Optional[str]) -> str:
    """Single-line cell text with Markdown pipes escaped."""
    if cell is None:
        return ""
    text = re.sub(r"\s+", " ", str(cell)).strip()
    return text.replace("|", "\\|")


def normalize_rows(rows: Sequence[Sequence[Optional[str]]]) -> List[List[str]]:
    """Clean cells and drop rows and columns that are entirely empty."""
    cleaned = [[_clean_cell(c) for c in row] for row in rows if row is not None]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    keep = [i for i in range(width) if any(row[i] for row in cleaned)]
    return [[row[i] for i in keep] for row in cleaned]


def _looks_like_ruled_table(rows: List[List[str]]) -> bool:
    """A ruled grid that is a real table, not a frame or a boxed paragraph."""
    if len(rows) < 2 or len(rows[0]) < 2:
        return False
    cells = [c for row in rows for c in row if c]
    return len(cells) >= 3


def _looks_like_text_table(rows: List[List[str]]) -> bool:
    """Strict checks for tables found by text alignment alone."""
    if len(rows) < 3:
        return False
    n_cols = len(rows[0])
    if not 3 <= n_cols <= 10:
        return False
    cells = [c for row in rows for c in row]
    filled = [c for c in cells if c]
    if len(filled) / len(cells) < 0.6:
        return False
    # Tables hold short values; prose split into columns does not.
    if sum(len(c) for c in filled) / len(filled) > 25:
        return False
    if sum(1 for c in filled if len(c) > 40) > 0.1 * len(filled):
        return False
    # Every column needs real content and most rows must span columns.
    for col in range(n_cols):
        if sum(1 for row in rows if row[col]) < 2:
            return False
    multi = sum(1 for row in rows if sum(1 for c in row if c) >= 2)
    return multi / len(rows) >= 0.8


def _splits_words(table: Any, words: List[Dict[str, Any]]) -> bool:
    """Whether column boundaries cut through words, as they do in prose."""
    x0, top, x1, bottom = table.bbox
    edges = sorted({float(col.bbox[0]) for col in table.columns})[1:]
    inside = [
        w
        for w in words
        if w["top"] >= top - 1
        and w["bottom"] <= bottom + 1
        and w["x0"] >= x0 - 1
        and w["x1"] <= x1 + 1
    ]
    if not inside or not edges:
        return False
    crossing = sum(
        1 for w in inside if any(w["x0"] + 1 < e < w["x1"] - 1 for e in edges)
    )
    return crossing > 0.02 * len(inside)


def _collect(page: Any, settings: Dict[str, Any], strict: bool) -> List[PageTable]:
    check = _looks_like_text_table if strict else _looks_like_ruled_table
    found: List[PageTable] = []
    words: Optional[List[Dict[str, Any]]] = None
    for table in page.find_tables(settings):
        rows = normalize_rows(table.extract())
        if not rows or not check(rows):
            continue
        if strict:
            if words is None:
                words = page.extract_words()
            if _splits_words(table, words):
                continue
        found.append(PageTable(bbox=tuple(table.bbox), rows=rows))  # type: ignore[arg-type]
    return found


def find_page_tables(page: Any) -> List[PageTable]:
    """Tables on a page in top-to-bottom order.

    Ruled tables are tried first; the text-alignment strategy is only used
    when a page has no ruled table, and its results must pass strict checks.
    """
    tables: List[PageTable] = []
    try:
        tables = _collect(page, LINES_SETTINGS, strict=False)
        if not tables:
            tables = _collect(page, TEXT_SETTINGS, strict=True)
    except Exception as exc:  # pdfplumber can trip over odd geometry
        logger.debug(
            "Table detection failed on page %s: %s",
            getattr(page, "page_number", "?"),
            exc,
            exc_info=True,
        )
        return []
    return sorted(tables, key=lambda t: (t.bbox[1], t.bbox[0]))


def render_markdown_table(rows: List[List[str]]) -> str:
    """A padded GitHub-style Markdown table; the first row is the header."""
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    widths = [max(3, max(len(row[i]) for row in rows)) for i in range(width)]

    def fmt(row: List[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(row, widths)) + " |"

    lines = [fmt(rows[0]), "| " + " | ".join("-" * w for w in widths) + " |"]
    lines.extend(fmt(row) for row in rows[1:])
    return "\n".join(lines)


def point_in_bboxes(obj: Dict[str, Any], bboxes: Sequence[BBox]) -> bool:
    """True when the centre of a pdfplumber object lies inside any bbox."""
    try:
        cx = (float(obj["x0"]) + float(obj["x1"])) / 2
        cy = (float(obj["top"]) + float(obj["bottom"])) / 2
    except (KeyError, TypeError, ValueError):
        return False
    for x0, top, x1, bottom in bboxes:
        if x0 - 1 <= cx <= x1 + 1 and top - 1 <= cy <= bottom + 1:
            return True
    return False
