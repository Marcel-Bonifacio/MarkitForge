# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Table detection on PDF pages (pdfplumber) and Markdown table rendering.

Ruled tables come from pdfplumber's line strategy and keep their merged
cells as spans. Borderless tables are found by alignment: words are grouped
into lines and cells, a region grows row by row while the cells keep landing
in the same columns, and the result must look tabular (aligned columns,
short cells) before it is accepted.
"""

import dataclasses
import html
import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..._base_converter import DocumentSection

logger = logging.getLogger(__name__)

BBox = Tuple[float, float, float, float]
# (row, column) of a merged cell's top-left corner -> (rowspan, colspan)
Spans = Dict[Tuple[int, int], Tuple[int, int]]

TABLE_FORMATS = ("markdown", "html", "auto")

# Ruled tables: cell borders drawn as lines or rectangles.
LINES_SETTINGS: Dict[str, Any] = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}

# pdfplumber's own text strategy. Detection no longer uses it (see
# find_text_tables); kept for callers that pass it to page.find_tables().
TEXT_SETTINGS: Dict[str, Any] = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "min_words_vertical": 3,
    "min_words_horizontal": 2,
}

# -- borderless detection limits ---------------------------------------------
TEXT_TABLE_MIN_ROWS = 3
TEXT_TABLE_MAX_COLS = 12
# A borderless table without a single column of numbers needs this many
# rows; shorter ones are usually address or author blocks side by side.
TEXT_ONLY_TABLE_MIN_ROWS = 6
# A horizontal gap wider than this many font sizes separates two cells.
CELL_GAP_RATIO = 0.75
CELL_GAP_MIN = 5.0
# Lines further apart than this many font sizes, or than twice the usual
# distance between the table's rows, are not in the same table.
ROW_GAP_RATIO = 1.6
# Cells whose left edges, right edges or centres differ by less are aligned.
ALIGN_TOLERANCE = 2.5
# Share of a column's cells that must share one alignment.
ALIGN_MIN_SHARE = 0.6
# Pages with more words than this skip borderless detection.
MAX_TEXT_TABLE_WORDS = 20000

_NUMERIC_RE = re.compile(r"^\(?[-+−]?\s?[$€£¥]?\s?\d[\d.,' ]*\s?(?:%|[$€£¥])?\)?$")
# Dot leaders ("Chapter 1 . . . . . 7"), underscores and ellipsis runs.
_LEADER_RE = re.compile(r"(?:\.\s?){4,}|(?:_\s?){6,}|…{2,}")


@dataclass
class PageTable:
    """A table found on a page: its bounding box and cleaned cell text.

    ``spans`` maps the top-left (row, column) of each merged cell to its
    (rowspan, colspan); the cells it covers are empty strings in ``rows``.
    ``kind`` is "ruled" or "text" (borderless).
    """

    bbox: BBox
    rows: List[List[str]]
    spans: Spans = field(default_factory=dict)
    kind: str = "ruled"

    @property
    def top(self) -> float:
        return self.bbox[1]


def _clean_cell(cell: Optional[str]) -> str:
    """Single-line cell text with Markdown pipes escaped."""
    if cell is None:
        return ""
    text = re.sub(r"\s+", " ", str(cell)).strip()
    return text.replace("|", "\\|")


def _normalize(
    rows: Sequence[Sequence[Optional[str]]], spans: Optional[Spans] = None
) -> Tuple[List[List[str]], Spans]:
    """Clean cells, drop empty rows and columns and re-index the spans."""
    cleaned = [[_clean_cell(c) for c in (row or [])] for row in rows]
    if not any(any(row) for row in cleaned):
        return [], {}
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    keep_rows = [r for r, row in enumerate(cleaned) if any(row)]
    keep_cols = [c for c in range(width) if any(cleaned[r][c] for r in keep_rows)]
    out = [[cleaned[r][c] for c in keep_cols] for r in keep_rows]
    new_spans: Spans = {}
    if spans:
        row_at = {r: i for i, r in enumerate(keep_rows)}
        col_at = {c: i for i, c in enumerate(keep_cols)}
        for (r, c), (rowspan, colspan) in spans.items():
            if r not in row_at or c not in col_at:
                continue
            rs = sum(1 for k in range(r, r + rowspan) if k in row_at)
            cs = sum(1 for k in range(c, c + colspan) if k in col_at)
            if rs > 1 or cs > 1:
                new_spans[(row_at[r], col_at[c])] = (rs, cs)
    return out, new_spans


def normalize_rows(rows: Sequence[Sequence[Optional[str]]]) -> List[List[str]]:
    """Clean cells and drop rows and columns that are entirely empty."""
    return _normalize(rows)[0]


def is_numeric_cell(text: str) -> bool:
    """Whether a cell holds a number, amount or percentage."""
    return bool(_NUMERIC_RE.match(text.strip()))


# ---------------------------------------------------------------- ruled tables


def _looks_like_ruled_table(rows: List[List[str]]) -> bool:
    """A ruled grid that is a real table, not a frame or a boxed paragraph."""
    if len(rows) < 2 or len(rows[0]) < 2:
        return False
    cells = [c for row in rows for c in row if c]
    return len(cells) >= 3


def _cell_spans(table: Any, n_rows: int, n_cols: int) -> Spans:
    """Merged cells of a pdfplumber table, on the grid ``extract()`` uses.

    pdfplumber lays a table out on the distinct left edges and tops of its
    cells; a cell that reaches past the next edge spans several columns or
    rows.
    """
    cells = [tuple(float(v) for v in cell) for cell in table.cells]
    xs = sorted({cell[0] for cell in cells})
    tops = sorted({cell[1] for cell in cells})
    if len(tops) != n_rows or len(xs) != n_cols:
        return {}
    spans: Spans = {}
    for x0, top, x1, bottom in cells:
        col = xs.index(x0)
        row = tops.index(top)
        colspan = sum(1 for x in xs[col:] if x < x1 - 0.5)
        rowspan = sum(1 for t in tops[row:] if t < bottom - 0.5)
        if rowspan > 1 or colspan > 1:
            spans[(row, col)] = (rowspan, colspan)
    return spans


def _ruled_tables(page: Any) -> List[PageTable]:
    found: List[PageTable] = []
    for table in page.find_tables(LINES_SETTINGS):
        raw = table.extract()
        spans: Spans = {}
        try:
            spans = _cell_spans(table, len(raw), max((len(r) for r in raw), default=0))
        except (TypeError, ValueError) as exc:
            logger.debug("Could not read merged cells: %s", exc)
        rows, spans = _normalize(raw, spans)
        if not rows or not _looks_like_ruled_table(rows):
            continue
        found.append(PageTable(bbox=tuple(table.bbox), rows=rows, spans=spans))  # type: ignore[arg-type]
    return found


# ----------------------------------------------------------- borderless tables


@dataclass
class _Cell:
    """A run of words on one line with no wide gap inside."""

    x0: float
    x1: float
    text: str

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class _Line:
    top: float
    bottom: float
    size: float
    cells: List[_Cell]

    @property
    def text(self) -> str:
        return " ".join(cell.text for cell in self.cells)


@dataclass
class _Column:
    """Horizontal extent of the cells assigned to one column so far."""

    x0: float
    x1: float

    def absorb(self, cell: _Cell) -> None:
        self.x0 = min(self.x0, cell.x0)
        self.x1 = max(self.x1, cell.x1)


_Entry = List[Tuple[_Column, _Cell]]


def _text_lines(words: Sequence[Dict[str, Any]]) -> List[_Line]:
    """Group words into visual lines and split each line into cells."""
    boxes: List[Tuple[float, float, float, float, str]] = []
    for word in words:
        if not word.get("upright", True):
            continue
        try:
            box = (
                float(word["x0"]),
                float(word["top"]),
                float(word["x1"]),
                float(word["bottom"]),
                str(word.get("text", "")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if box[4].strip() and box[3] > box[1]:
            boxes.append(box)
    boxes.sort(key=lambda b: ((b[1] + b[3]) / 2, b[0]))

    groups: List[List[Tuple[float, float, float, float, str]]] = []
    band = (0.0, 0.0)
    for box in boxes:
        mid = (box[1] + box[3]) / 2
        if groups and band[0] <= mid <= band[1]:
            groups[-1].append(box)
        else:
            groups.append([box])
            band = (box[1], box[3])

    lines: List[_Line] = []
    for group in groups:
        group.sort(key=lambda b: b[0])
        size = statistics.median(b[3] - b[1] for b in group)
        max_gap = max(CELL_GAP_RATIO * size, CELL_GAP_MIN)
        cells: List[_Cell] = []
        for x0, _top, x1, _bottom, text in group:
            if cells and x0 - cells[-1].x1 <= max_gap:
                last = cells[-1]
                last.x1 = max(last.x1, x1)
                last.text = f"{last.text} {text}"
            else:
                cells.append(_Cell(x0=x0, x1=x1, text=text))
        lines.append(
            _Line(
                top=min(b[1] for b in group),
                bottom=max(b[3] for b in group),
                size=size,
                cells=cells,
            )
        )
    return lines


def _place(line: _Line, columns: List[_Column]) -> Optional[List[Optional[_Column]]]:
    """The column of each cell on a line (None for a cell that sits between
    the known columns), or None when the line does not fit the columns: a
    cell reaches into two of them, or two cells land in the same one."""
    placed: List[Optional[_Column]] = []
    used: Set[int] = set()
    for cell in line.cells:
        hits = [
            col for col in columns if min(cell.x1, col.x1) - max(cell.x0, col.x0) > 1.0
        ]
        if len(hits) > 1:
            return None
        if not hits:
            placed.append(None)
            continue
        if id(hits[0]) in used:
            return None
        used.add(id(hits[0]))
        placed.append(hits[0])
    return placed


def _grow(lines: Sequence[_Line], start: int) -> Tuple[int, List[_Entry]]:
    """Grow a region down from ``lines[start]`` while lines fit its columns.

    Returns the index after the last line taken and one entry per line.
    """
    seed = lines[start]
    columns = [_Column(cell.x0, cell.x1) for cell in seed.cells]
    entries: List[_Entry] = [list(zip(columns, seed.cells))]
    gaps: List[float] = []
    j = start + 1
    while j < len(lines):
        line, prev = lines[j], lines[j - 1]
        gap = line.top - prev.bottom
        if gap > ROW_GAP_RATIO * max(line.size, prev.size):
            break
        if len(gaps) >= 2 and gap > 2.0 * statistics.median(gaps) + 2.0:
            break
        placed = _place(line, columns)
        if placed is None:
            break
        known = sum(1 for col in placed if col is not None)
        # Most cells must land in columns the region already has; a line
        # with a single cell (a wrapped cell) must continue a known column.
        if known == 0 or known * 2 < len(placed):
            break
        if len(columns) + len(placed) - known > TEXT_TABLE_MAX_COLS:
            break
        entry: _Entry = []
        for col, cell in zip(placed, line.cells):
            if col is None:
                col = _Column(cell.x0, cell.x1)
                columns.append(col)
            else:
                col.absorb(cell)
            entry.append((col, cell))
        entries.append(entry)
        gaps.append(gap)
        j += 1
    return j, entries


@dataclass
class _Row:
    """A table row: the cells of one line plus any wrapped lines below."""

    cells: Dict[int, _Cell]
    texts: Dict[int, str]
    first: int  # index of the row's first line within the region
    last: int


def _build_rows(
    region: Sequence[_Line], entries: Sequence[_Entry]
) -> Tuple[List[_Row], int]:
    """Turn region lines into rows, merging wrapped cells into the row above.

    A line with text in one column only continues the cell above it when it
    sits closer than a normal row, or when that cell filled its column (or
    is not the first column) and the line follows at row distance. Trailing
    single-cell lines that do not qualify are cut off; the second value is
    the number of region lines kept.
    """
    columns: List[_Column] = []
    for entry in entries:
        for col, _cell in entry:
            if not any(col is known for known in columns):
                columns.append(col)
    columns.sort(key=lambda col: col.x0)
    index = {id(col): i for i, col in enumerate(columns)}

    multi = [len(entry) > 1 for entry in entries]
    gaps = [0.0] + [region[k].top - region[k - 1].bottom for k in range(1, len(region))]
    row_gaps = [gaps[k] for k in range(1, len(region)) if multi[k] and multi[k - 1]]
    row_gap = statistics.median(row_gaps) if row_gaps else None
    last_multi = max(k for k, flag in enumerate(multi) if flag)

    rows: List[_Row] = []
    kept = len(region)
    for k, entry in enumerate(entries):
        if multi[k] or not rows:
            cells = {index[id(col)]: cell for col, cell in entry}
            texts = {i: cell.text for i, cell in cells.items()}
            rows.append(_Row(cells=cells, texts=texts, first=k, last=k))
            continue
        col, cell = entry[0]
        i = index[id(col)]
        above = rows[-1]
        size = region[k].size
        if row_gap is not None and row_gap > 0:
            tight = gaps[k] < 0.75 * row_gap
            near = gaps[k] <= 1.25 * row_gap + 1.0
        else:
            tight = gaps[k] <= 0.35 * size
            near = False
        anchor = above.cells.get(i)
        filled = (
            anchor is not None and anchor.x1 >= col.x1 - 0.15 * (col.x1 - col.x0) - 1.0
        )
        wraps = (
            above.last == k - 1
            and bool(above.texts.get(i))
            and (tight or (near and (i > 0 or filled)))
        )
        if wraps:
            above.texts[i] = f"{above.texts[i]} {cell.text}"
            above.last = k
        elif k > last_multi:
            kept = k
            break
        else:
            rows.append(_Row(cells={i: cell}, texts={i: cell.text}, first=k, last=k))
    return rows, kept


def _aligned_share(values: Sequence[float]) -> float:
    centre = statistics.median(values)
    return sum(1 for v in values if abs(v - centre) <= ALIGN_TOLERANCE) / len(values)


def _columns_aligned(rows: Sequence[_Row], n_cols: int) -> bool:
    """Most columns keep one alignment (left, right or centred) below the
    header row; cells of prose cut at random gaps do not."""
    body = rows[1:] if len(rows) > TEXT_TABLE_MIN_ROWS else rows
    aligned = 0
    for i in range(n_cols):
        cells = [row.cells[i] for row in body if i in row.cells]
        if len(cells) < 2:
            continue
        share = max(
            _aligned_share([cell.x0 for cell in cells]),
            _aligned_share([cell.x1 for cell in cells]),
            _aligned_share([cell.centre for cell in cells]),
        )
        if share >= ALIGN_MIN_SHARE:
            aligned += 1
    return aligned >= n_cols - n_cols // 4


def _looks_like_text_table(rows: List[List[str]]) -> bool:
    """Checks on the cell text of a table found by alignment alone."""
    if len(rows) < TEXT_TABLE_MIN_ROWS:
        return False
    n_cols = len(rows[0])
    if not 2 <= n_cols <= TEXT_TABLE_MAX_COLS:
        return False
    cells = [c for row in rows for c in row]
    filled = [c for c in cells if c]
    if len(filled) / len(cells) < 0.5:
        return False
    # Every column needs real content and most rows must span columns.
    columns = [[row[i] for row in rows if row[i]] for i in range(n_cols)]
    if any(len(col) < 2 for col in columns):
        return False
    multi = sum(1 for row in rows if sum(1 for c in row if c) >= 2)
    if multi < TEXT_TABLE_MIN_ROWS or multi / len(rows) < 0.7:
        return False
    # Tables hold short values; prose laid out in columns does not. One
    # descriptive column is fine, several are text columns.
    if sum(len(c) for c in filled) / len(filled) > 40:
        return False
    wordy = sum(
        1
        for col in columns
        if statistics.median(len(c) for c in col) > 35
        or statistics.median(len(c.split()) for c in col) >= 6
    )
    if wordy > max(1, (n_cols - 1) // 2):
        return False
    numeric_cols = 0
    for i in range(n_cols):
        values = [row[i] for row in rows[1:] if row[i]]
        if values and sum(1 for v in values if is_numeric_cell(v)) * 2 >= len(values):
            numeric_cols += 1
    if not numeric_cols and len(rows) < TEXT_ONLY_TABLE_MIN_ROWS:
        return False
    if n_cols == 2:
        # Label/value pairs, numbered lists and side-by-side blocks have two
        # aligned columns too: require a column of numbers on the right.
        values = [row[1] for row in rows[1:] if row[1]]
        numeric = sum(1 for v in values if is_numeric_cell(v))
        if wordy or len(values) < 2 or numeric < 0.6 * len(values):
            return False
    return True


def _has_leaders(region: Sequence[_Line]) -> bool:
    """Dot leaders mark a table of contents or an index, not a table."""
    hits = sum(1 for line in region if _LEADER_RE.search(line.text))
    return hits >= 0.4 * len(region)


def find_text_tables(page: Any, exclude: Sequence[BBox] = ()) -> List[PageTable]:
    """Borderless tables on a page, found by column alignment.

    ``exclude`` lists areas (ruled tables) whose words are ignored.
    """
    words = page.extract_words()
    if exclude:
        words = [w for w in words if not point_in_bboxes(w, exclude)]
    if len(words) < 2 * TEXT_TABLE_MIN_ROWS or len(words) > MAX_TEXT_TABLE_WORDS:
        return []
    lines = _text_lines(words)
    found: List[PageTable] = []
    start = 0
    while start < len(lines):
        if len(lines[start].cells) < 2:
            start += 1
            continue
        end, entries = _grow(lines, start)
        if end - start < TEXT_TABLE_MIN_ROWS:
            start += 1
            continue
        rows, kept = _build_rows(lines[start:end], entries)
        region = lines[start : start + kept]
        n_cols = 1 + max(i for row in rows for i in row.texts)
        grid = [[_clean_cell(row.texts.get(i)) for i in range(n_cols)] for row in rows]
        if (
            _looks_like_text_table(grid)
            and _columns_aligned(rows, n_cols)
            and not _has_leaders(region)
        ):
            cells = [cell for entry in entries[:kept] for _col, cell in entry]
            bbox = (
                min(cell.x0 for cell in cells) - 1.0,
                min(line.top for line in region) - 1.0,
                max(cell.x1 for cell in cells) + 1.0,
                max(line.bottom for line in region) + 1.0,
            )
            found.append(PageTable(bbox=bbox, rows=grid, kind="text"))
            start += kept
        else:
            # Lines of a rejected region are not tried again as seeds.
            start = end
    return found


def find_page_tables(page: Any) -> List[PageTable]:
    """Tables on a page in top-to-bottom order.

    Ruled tables are found first; text outside them is then searched for
    borderless tables, which must pass the alignment and content checks.
    """
    tables: List[PageTable] = []
    try:
        tables = _ruled_tables(page)
        tables.extend(find_text_tables(page, [t.bbox for t in tables]))
    except Exception as exc:  # pdfplumber can trip over odd geometry
        logger.debug(
            "Table detection failed on page %s: %s",
            getattr(page, "page_number", "?"),
            exc,
            exc_info=True,
        )
        return tables
    return sorted(tables, key=lambda t: (t.bbox[1], t.bbox[0]))


# ------------------------------------------------------------------- rendering


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


def valid_spans(rows: Sequence[Sequence[str]], spans: Optional[Spans]) -> Spans:
    """The spans that are safe to render: integer row/column spans of at
    least 1, clipped to the grid, not overlapping each other and not hiding
    a cell that has text."""
    if not rows or not spans:
        return {}
    n_rows = len(rows)
    n_cols = max(len(row) for row in rows)
    taken: Set[Tuple[int, int]] = set()
    out: Spans = {}
    for key, value in sorted(spans.items(), key=lambda item: repr(item[0])):
        if not isinstance(key, tuple) or not isinstance(value, tuple):
            continue
        numbers = (*key, *value)
        if len(key) != 2 or len(value) != 2:
            continue
        if not all(type(n) is int for n in numbers):
            continue
        r, c, rowspan, colspan = numbers
        if not (0 <= r < n_rows and 0 <= c < n_cols):
            continue
        rowspan = min(rowspan, n_rows - r)
        colspan = min(colspan, n_cols - c)
        if rowspan < 1 or colspan < 1 or (rowspan == 1 and colspan == 1):
            continue
        area = {(r + i, c + j) for i in range(rowspan) for j in range(colspan)}
        hidden = area - {(r, c)}
        if area & taken:
            continue
        if any(j < len(rows[i]) and rows[i][j] for i, j in hidden):
            continue
        taken |= area
        out[(r, c)] = (rowspan, colspan)
    return out


def render_html_table(rows: List[List[str]], spans: Optional[Spans] = None) -> str:
    """An HTML table with ``colspan``/``rowspan`` for merged cells.

    Cell text is HTML-escaped and the block has no blank lines, so it stays
    one HTML block inside Markdown. The first row (and the rows its merged
    cells reach into) is the header.
    """
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    spans = valid_spans(rows, spans)
    covered = {
        (r + i, c + j)
        for (r, c), (rowspan, colspan) in spans.items()
        for i in range(rowspan)
        for j in range(colspan)
    } - set(spans)
    head = max([1] + [rowspan for (r, _c), (rowspan, _cs) in spans.items() if r == 0])
    if head >= len(rows):
        head = 1

    def fmt(r: int, tag: str) -> str:
        cells = []
        for c, text in enumerate(rows[r]):
            if (r, c) in covered:
                continue
            rowspan, colspan = spans.get((r, c), (1, 1))
            attrs = ""
            if rowspan > 1:
                attrs += f' rowspan="{int(rowspan)}"'
            if colspan > 1:
                attrs += f' colspan="{int(colspan)}"'
            # Cells carry Markdown pipe escapes; HTML needs the plain text.
            body = html.escape(text.replace("\\|", "|"), quote=True)
            cells.append(f"<{tag}{attrs}>{body}</{tag}>")
        return "<tr>" + "".join(cells) + "</tr>"

    lines = ["<table>", "<thead>"]
    lines.extend(fmt(r, "th") for r in range(head))
    lines.append("</thead>")
    if len(rows) > head:
        lines.append("<tbody>")
        lines.extend(fmt(r, "td") for r in range(head, len(rows)))
        lines.append("</tbody>")
    lines.append("</table>")
    return "\n".join(lines)


def render_table(table: PageTable, table_format: str = "markdown") -> str:
    """Render a page table as Markdown or HTML.

    ``table_format`` is "markdown" (merged cells keep their text in the
    first cell, the rest stay empty), "html" (always an HTML table) or
    "auto" (HTML only for tables with merged cells). Unknown values mean
    "markdown".
    """
    fmt = str(table_format or "markdown").strip().lower()
    if fmt == "html" or (fmt == "auto" and valid_spans(table.rows, table.spans)):
        return render_html_table(table.rows, table.spans)
    return render_markdown_table(table.rows)


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


# ------------------------------------------------- tables continued over pages

_ROW_SPLIT_RE = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")


def split_markdown_row(line: str) -> Optional[List[str]]:
    """Cells of one ``| a | b |`` line (escaped pipes stay in their cell),
    or None when the line is not a table row."""
    text = line.strip()
    if not text.startswith("|") or not text.endswith("|") or len(text) < 2:
        return None
    if text.endswith("\\|"):
        return None
    return [cell.strip() for cell in _ROW_SPLIT_RE.split(text[1:-1])]


def _is_separator(cells: Optional[List[str]]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(c) for c in cells or [])


def parse_markdown_table(text: str) -> Optional[List[List[str]]]:
    """Rows of a Markdown table (header first, separator dropped).

    Returns None unless every line is a table row, the second line is the
    only separator and all rows have the header's number of cells.
    """
    parsed = [split_markdown_row(line) for line in text.strip().split("\n")]
    if len(parsed) < 2 or any(cells is None for cells in parsed):
        return None
    rows = [cells for cells in parsed if cells is not None]
    if not _is_separator(rows[1]) or _is_separator(rows[0]):
        return None
    body = rows[2:]
    if any(_is_separator(cells) for cells in body):
        return None
    if any(len(cells) != len(rows[0]) for cells in rows):
        return None
    return [rows[0]] + body


def _trailing_table(markdown: str) -> Optional[Tuple[str, List[List[str]]]]:
    """(text before, rows) when the last block of a page is a table."""
    lines = markdown.rstrip().split("\n")
    start = len(lines)
    while start > 0 and lines[start - 1].lstrip().startswith("|"):
        start -= 1
    if start == len(lines):
        return None
    rows = parse_markdown_table("\n".join(lines[start:]))
    if rows is None:
        return None
    return "\n".join(lines[:start]), rows


def _leading_table(markdown: str) -> Optional[Tuple[List[List[str]], str]]:
    """(rows, text after) when the first block of a page is a table."""
    lines = markdown.lstrip("\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    end = 0
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    if end == 0:
        return None
    rows = parse_markdown_table("\n".join(lines[:end]))
    if rows is None:
        return None
    return rows, "\n".join(lines[end:]).lstrip("\n")


def _looks_like_data(
    first: Sequence[str], header: Sequence[str], body: Sequence[Sequence[str]]
) -> bool:
    """Whether the first row of a continued table is data, not a header:
    it has numbers in the columns where the table above has numbers under
    a text header, and text nowhere a number is expected."""
    numeric_cols = []
    for i, name in enumerate(header):
        values = [row[i] for row in body if row[i]]
        if not values or is_numeric_cell(name):
            continue
        if sum(1 for v in values if is_numeric_cell(v)) >= 0.6 * len(values):
            numeric_cols.append(i)
    if not numeric_cols:
        return False
    cells = [first[i] for i in numeric_cols if first[i]]
    return bool(cells) and all(is_numeric_cell(c) for c in cells)


def merge_continued_tables(sections: List[DocumentSection]) -> List[DocumentSection]:
    """Join tables that continue from one page onto the next.

    When a page ends with a Markdown table and the next page starts with
    one of the same width, the next page's rows are appended to the first
    table if its header row repeats the first table's header (the repeat is
    dropped) or its first row is data rather than a header. The next page
    keeps whatever follows its table. Tables with different column counts,
    or with text between them, are left alone. The input is not modified.
    """
    out = [dataclasses.replace(sec) for sec in sections]
    anchor = 0
    for nxt in range(1, len(out)):
        tail = _trailing_table(out[anchor].markdown)
        lead = _leading_table(out[nxt].markdown)
        if tail is None or lead is None:
            if out[nxt].markdown.strip():
                anchor = nxt
            continue
        before, rows = tail
        more, rest = lead
        header, first = rows[0], more[0]
        if len(first) != len(header):
            anchor = nxt
            continue
        if [c.lower() for c in first] == [c.lower() for c in header]:
            added = more[1:]
        elif _looks_like_data(first, header, rows[1:]):
            added = more
        else:
            anchor = nxt
            continue
        table = render_markdown_table(rows + added)
        out[anchor].markdown = f"{before}\n{table}" if before.strip() else table
        out[nxt].markdown = rest
        if rest.strip():
            anchor = nxt
    return out
