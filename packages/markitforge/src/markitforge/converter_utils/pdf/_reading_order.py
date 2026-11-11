# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reading order for a PDF page: a recursive XY-cut over word boxes.

The page is cut along whitespace: a block is first sliced into horizontal
bands, runs of bands that share a column gutter are split vertically, and
each column is analysed again. What remains are blocks of text that read
top to bottom; they are emitted column by column, left to right.

Tables, images and formulas take part as opaque boxes, so a full-width table
ends the columns above it and the columns below start a new group.
"""

import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ._layout import (
    Block,
    Flow,
    TextLine,
    is_bold_font,
    is_math_font,
    is_mono_font,
)

# A gutter must be this many median character widths wide ...
MIN_GUTTER_CHAR_WIDTHS = 1.8
# ... and never narrower than this (points).
MIN_GUTTER_POINTS = 6.0
# Gutters at least this many character widths wide need fewer lines to count.
WIDE_GUTTER_CHAR_WIDTHS = 3.0
# Lines a column needs beside a wide / narrow gutter.
MIN_COLUMN_LINES = 3
MIN_COLUMN_LINES_NARROW = 5
# Columns of prose have lines of some length; label/value pairs and list
# markers do not, and those are read row by row instead.
MIN_COLUMN_LINE_CHARS = 15
MIN_WIDER_COLUMN_LINE_CHARS = 25
# Below this many lines a pair of columns may be a small header table, so
# both sides must then have prose-length lines.
TALL_COLUMN_LINES = 6
MAX_DEPTH = 8
# A gap between words of one line wider than this marks tabular text.
TABULAR_GAP_SIZES = 3.0
TABULAR_GAP_POINTS = 15.0
MARKER_MAX_CHARS = 4
# Characters closer than this share a word (fraction of the font size).
WORD_GAP = 0.1

_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}


class Run:
    """Consecutive characters of one word (or word fragment)."""

    __slots__ = (
        "parts",
        "x0",
        "x1",
        "top",
        "bottom",
        "size",
        "n",
        "bold",
        "math",
        "mono",
        "tag",
        "space_before",
        "sizes",
    )

    def __init__(
        self, x0: float, x1: float, top: float, bottom: float, size: float
    ) -> None:
        self.parts: List[str] = []
        self.x0 = x0
        self.x1 = x1
        self.top = top
        self.bottom = bottom
        self.size = size
        self.n = 0
        self.bold = 0
        self.math = 0
        self.mono = 0
        self.tag: Optional[str] = None
        self.space_before = False
        self.sizes: Optional[Dict[float, int]] = None

    @property
    def text(self) -> str:
        return "".join(self.parts)


Item = Union[Run, Block]


def _box(item: Item) -> Tuple[float, float, float, float]:
    """(x0, top, x1, bottom) of a run or block."""
    if isinstance(item, Run):
        return item.x0, item.top, item.x1, item.bottom
    x1 = item.x1 if item.x1 is not None else item.x0
    bottom = item.bottom if item.bottom is not None else item.top
    return item.x0, item.top, max(x1, item.x0), max(bottom, item.top)


def build_runs(
    chars: Sequence[Dict[str, Any]],
) -> Tuple[List[Run], List[Dict[str, Any]]]:
    """Group characters into word runs, in content-stream order.

    Returns the runs of horizontal text and the rotated characters (margin
    stamps, sideways tables), which are laid out separately.
    """
    runs: List[Run] = []
    rotated: List[Dict[str, Any]] = []
    fonts: Dict[str, Tuple[bool, bool, bool]] = {}
    cur: Optional[Run] = None
    space = False
    for c in chars:
        text = c.get("text") or ""
        if c.get("upright") is False:
            rotated.append(c)
            continue
        if not text.strip():
            space = True
            continue
        if text in _LIGATURES:
            text = _LIGATURES[text]
        x0 = float(c["x0"])
        x1 = float(c["x1"])
        top = float(c["top"])
        bottom = float(c["bottom"])
        size = float(c.get("size") or (bottom - top) or 1.0)
        fontname = c.get("fontname") or ""
        flags = fonts.get(fontname)
        if flags is None:
            flags = (
                is_bold_font(fontname),
                is_math_font(fontname),
                is_mono_font(fontname),
            )
            fonts[fontname] = flags
        joined = False
        if cur is not None and not space:
            gap = x0 - cur.x1
            if -0.5 * size <= gap <= WORD_GAP * max(size, cur.size):
                overlap = min(bottom, cur.bottom) - max(top, cur.top)
                if overlap > 0.5 * min(bottom - top, cur.bottom - cur.top):
                    joined = True
        if not joined or cur is None:
            new = Run(x0, x1, top, bottom, size)
            new.space_before = space and cur is not None
            new.tag = c.get("tag")
            runs.append(new)
            cur = new
        else:
            if x1 > cur.x1:
                cur.x1 = x1
            if top < cur.top:
                cur.top = top
            if bottom > cur.bottom:
                cur.bottom = bottom
            if size != cur.size:
                if cur.sizes is None:
                    cur.sizes = {cur.size: cur.n}
                cur.sizes[size] = cur.sizes.get(size, 0) + 1
            elif cur.sizes is not None:
                cur.sizes[size] = cur.sizes.get(size, 0) + 1
        space = False
        cur.parts.append(text)
        cur.n += 1
        if flags[0]:
            cur.bold += 1
        if flags[1]:
            cur.math += 1
        if flags[2]:
            cur.mono += 1
    return runs, rotated


# -- XY-cut ---------------------------------------------------------------------


def _bands(items: Sequence[Item]) -> List[List[Item]]:
    """Horizontal bands: groups of items whose (shrunken) boxes overlap in y."""
    spans: List[Tuple[float, float, Item]] = []
    for item in items:
        _x0, top, _x1, bottom = _box(item)
        inset = 0.2 * (bottom - top) if isinstance(item, Run) else 0.0
        spans.append((top + inset, bottom - inset, item))
    spans.sort(key=lambda s: s[0])
    bands: List[List[Item]] = []
    band_bottom = float("-inf")
    for top, bottom, item in spans:
        if bands and top < band_bottom:
            bands[-1].append(item)
            band_bottom = max(band_bottom, bottom)
        else:
            bands.append([item])
            band_bottom = bottom
    return bands


def _occupied(items: Sequence[Item], min_gap: float) -> List[List[float]]:
    """Merged x-intervals covered by items; gaps under min_gap are closed."""
    spans = sorted((_box(i)[0], _box(i)[2]) for i in items)
    return _merge(spans, min_gap)


def _merge(spans: Sequence[Sequence[float]], min_gap: float) -> List[List[float]]:
    out: List[List[float]] = []
    for a, b in spans:
        if out and a - out[-1][1] < min_gap:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return out


@dataclass
class _Column:
    items: List[Item] = field(default_factory=list)
    x0: float = 0.0
    x1: float = 0.0
    lines: int = 0
    chars: int = 0
    obstacle: bool = False

    def measure(self) -> None:
        runs = [i for i in self.items if isinstance(i, Run)]
        self.obstacle = len(runs) != len(self.items)
        self.lines = len(_bands(runs)) if runs else 0
        self.chars = sum(r.n for r in runs)

    @property
    def line_chars(self) -> float:
        return self.chars / self.lines if self.lines else 0.0


def _is_gutter(left: _Column, right: _Column, char_width: float) -> bool:
    """Whether the gap between two candidate columns separates text columns."""
    gap = right.x0 - left.x1
    wide = gap >= WIDE_GUTTER_CHAR_WIDTHS * char_width
    need = MIN_COLUMN_LINES if wide else MIN_COLUMN_LINES_NARROW
    for col in (left, right):
        if col.obstacle:
            continue
        if col.lines < need or col.line_chars < MIN_COLUMN_LINE_CHARS:
            return False
    if left.obstacle or right.obstacle:
        return True
    lengths = (left.line_chars, right.line_chars)
    tall = max(left.lines, right.lines) >= TALL_COLUMN_LINES
    return (max(lengths) if tall else min(lengths)) >= MIN_WIDER_COLUMN_LINE_CHARS


def _columns(
    items: Sequence[Item], occupied: Sequence[Sequence[float]], char_width: float
) -> List[List[Item]]:
    """Split items at the valid gutters between the occupied intervals."""
    cols = [_Column(x0=a, x1=b) for a, b in occupied]
    for item in items:
        x0, _top, x1, _bottom = _box(item)
        centre = (x0 + x1) / 2
        for col in cols:
            if centre <= col.x1 or col is cols[-1]:
                col.items.append(item)
                break
    for col in cols:
        col.measure()
    i = 0
    while i < len(cols) - 1:
        if _is_gutter(cols[i], cols[i + 1], char_width):
            i += 1
            continue
        merged = _Column(
            items=cols[i].items + cols[i + 1].items, x0=cols[i].x0, x1=cols[i + 1].x1
        )
        merged.measure()
        cols[i : i + 2] = [merged]
        i = max(i - 1, 0)
    return [col.items for col in cols]


def _segments(
    bands: Sequence[List[Item]], min_gutter: float, char_width: float
) -> List[List[List[Item]]]:
    """Group bands top to bottom into segments.

    A segment is a list of columns: one column for full-width content, more
    for a run of bands that share at least one valid gutter.
    """
    segments: List[List[List[Item]]] = []
    n = len(bands)
    i = 0
    while i < n:
        occupied = _occupied(bands[i], min_gutter)
        columns: Optional[List[List[Item]]] = None
        j = i + 1
        if len(occupied) > 1:
            while j < n:
                wider = _merge(
                    sorted(occupied + _occupied(bands[j], min_gutter)), min_gutter
                )
                if len(wider) < 2:
                    break
                occupied = wider
                j += 1
            items = [item for band in bands[i:j] for item in band]
            split = _columns(items, occupied, char_width)
            if len(split) > 1:
                columns = split
        if columns is not None:
            segments.append(columns)
            i = j
        else:
            if segments and len(segments[-1]) == 1:
                segments[-1][0].extend(bands[i])
            else:
                segments.append([list(bands[i])])
            i += 1
    return segments


def xy_cut(
    items: Sequence[Item], char_width: float, depth: int = 0
) -> List[List[Item]]:
    """Blocks of items in reading order."""
    if not items:
        return []
    if len(items) < 2 * MIN_COLUMN_LINES or depth >= MAX_DEPTH:
        return [list(items)]
    min_gutter = max(MIN_GUTTER_CHAR_WIDTHS * char_width, MIN_GUTTER_POINTS)
    segments = _segments(_bands(items), min_gutter, char_width)
    if len(segments) == 1 and len(segments[0]) == 1:
        return [list(items)]
    blocks: List[List[Item]] = []
    for columns in segments:
        if len(columns) == 1:
            blocks.append(columns[0])
            continue
        for column in columns:
            blocks.extend(xy_cut(column, char_width, depth + 1))
    return blocks


# -- lines ------------------------------------------------------------------------


def _group_lines(runs: Sequence[Run]) -> List[List[Run]]:
    """Runs of one block grouped into visual lines, top to bottom."""
    lines: List[Tuple[List[float], List[Run]]] = []
    for run in sorted(runs, key=lambda r: (r.top + r.bottom, r.x0)):
        height = run.bottom - run.top
        placed = False
        for box, members in reversed(lines[-3:]):
            overlap = min(run.bottom, box[1]) - max(run.top, box[0])
            line_height = box[1] - box[0]
            # Sub- and superscripts are small and shifted: less overlap will do.
            share = 0.3 if height < 0.8 * line_height else 0.5
            if overlap > share * min(height, line_height):
                members.append(run)
                if height > box[1] - box[0]:
                    box[0], box[1] = run.top, run.bottom
                placed = True
                break
        if not placed:
            lines.append(([run.top, run.bottom], [run]))
    lines.sort(key=lambda entry: entry[0][0])
    return [members for _box_, members in lines]


def _line_from_runs(runs: List[Run], block: int) -> TextLine:
    runs.sort(key=lambda r: r.x0)
    parts: List[str] = []
    sizes: Counter = Counter()
    tags: Counter = Counter()
    tabular = False
    first_word_end = runs[0].x1
    in_first_word = True
    prev: Optional[Run] = None
    width = 0.0
    for run in runs:
        if prev is not None:
            gap = run.x0 - prev.x1
            size = max(run.size, prev.size)
            if run.space_before or gap > WORD_GAP * size:
                # A wide gap after a short first word is a list marker set
                # off from its text (".1   The intent ..."), not a table.
                marker = in_first_word and sum(map(len, parts)) <= MARKER_MAX_CHARS
                parts.append(" ")
                in_first_word = False
                if (
                    gap > max(TABULAR_GAP_SIZES * size, TABULAR_GAP_POINTS)
                    and not marker
                ):
                    tabular = True
            if in_first_word:
                first_word_end = run.x1
        parts.append(run.text)
        if run.sizes is not None:
            for size_, count in run.sizes.items():
                sizes[round(size_, 1)] += count
        else:
            sizes[round(run.size, 1)] += run.n
        if run.tag:
            tags[run.tag] += run.n
        width += run.x1 - run.x0
        prev = run
    total = sum(r.n for r in runs)
    text = unicodedata.normalize("NFC", "".join(parts)).strip()
    bottoms: Counter = Counter()
    for run in runs:
        bottoms[round(run.bottom, 1)] += run.n
    return TextLine(
        text=text,
        x0=runs[0].x0,
        top=min(r.top for r in runs),
        x1=max(r.x1 for r in runs),
        bottom=max(r.bottom for r in runs),
        size=float(sizes.most_common(1)[0][0]) if sizes else 0.0,
        bold=total > 0 and sum(r.bold for r in runs) >= 0.8 * total,
        math_chars=sum(r.math for r in runs),
        total_chars=total,
        block=block,
        mono=total > 0 and sum(r.mono for r in runs) >= 0.8 * total,
        tabular=tabular,
        tag=tags.most_common(1)[0][0] if tags else None,
        char_width=width / total if total else 0.0,
        first_word_width=first_word_end - runs[0].x0,
        baseline=float(bottoms.most_common(1)[0][0]),
    )


def _median_char_width(runs: Sequence[Run]) -> float:
    widths = sorted((r.x1 - r.x0) / r.n for r in runs if r.n)
    return widths[len(widths) // 2] if widths else 5.0


def layout_page(chars: Sequence[Dict[str, Any]], blocks: Sequence[Block] = ()) -> Flow:
    """Lines and blocks of a page in reading order.

    ``chars`` are pdfplumber character dicts (text inside tables already
    removed); ``blocks`` are the tables, images and formulas placed on the
    page. Every line records the reading-order block it belongs to.
    """
    runs, rotated = build_runs(chars)
    items: List[Item] = list(runs)
    items.extend(blocks)
    flow: Flow = []
    char_width = _median_char_width(runs)
    for number, group in enumerate(xy_cut(items, char_width)):
        entries: List[Tuple[float, float, Union[TextLine, Block]]] = []
        text_runs = [i for i in group if isinstance(i, Run)]
        for members in _group_lines(text_runs):
            line = _line_from_runs(members, number)
            if line.text:
                entries.append((line.top, line.x0, line))
        for item in group:
            if isinstance(item, Block):
                entries.append((item.top, item.x0, item))
        entries.sort(key=lambda e: (e[0], e[1]))
        flow.extend(entry[2] for entry in entries)
    flow.extend(rotated_lines(rotated))
    return flow


def rotated_lines(chars: Sequence[Dict[str, Any]]) -> List[TextLine]:
    """Sideways text as separate lines in reading order.

    Characters turned a quarter turn are grouped into lines by their x
    position (a sideways line is a vertical strip). Text turned
    counter-clockwise reads bottom to top with its first line on the left;
    clockwise text reads top to bottom with its first line on the right.
    Each line keeps ``rotated=True`` and goes after the page's upright text.
    """
    if not chars:
        return []
    columns: List[Tuple[float, float, int, List[Dict[str, Any]]]] = []
    for c in chars:
        x0, x1 = float(c.get("x0", 0.0)), float(c.get("x1", 0.0))
        centre = (x0 + x1) / 2
        width = max(x1 - x0, float(c.get("size") or 0.0) * 0.5, 1.0)
        matrix = c.get("matrix") or (1, 0, 0, 1, 0, 0)
        direction = 1 if float(matrix[1]) >= 0 else -1
        for k, (col_centre, col_width, col_dir, members) in enumerate(columns):
            if col_dir == direction and abs(col_centre - centre) <= 0.5 * max(
                width, col_width
            ):
                members.append(c)
                break
        else:
            columns.append((centre, width, direction, [c]))
    lines: List[Tuple[int, float, TextLine]] = []
    for centre, _width, direction, members in columns:
        # Counter-clockwise text runs upward: the first character is lowest.
        members.sort(key=lambda c: float(c.get("top", 0.0)), reverse=direction > 0)
        text = " ".join("".join(str(c.get("text") or "") for c in members).split())
        if not text:
            continue
        lines.append(
            (
                direction,
                centre,
                TextLine(
                    text=text,
                    x0=min(float(c.get("x0", 0.0)) for c in members),
                    top=float("inf"),
                    x1=max(float(c.get("x1", 0.0)) for c in members),
                    bottom=float("inf"),
                    size=float(members[0].get("size") or 0.0),
                    bold=False,
                    total_chars=len(text),
                    block=-1,
                    rotated=True,
                ),
            )
        )
    # Counter-clockwise lines read left to right, clockwise ones right to left.
    lines.sort(key=lambda entry: (-entry[0], entry[1] * entry[0]))
    return [line for _d, _c, line in lines]
