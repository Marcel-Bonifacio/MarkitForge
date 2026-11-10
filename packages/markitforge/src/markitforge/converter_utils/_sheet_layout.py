# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Table detection for spreadsheet grids.

A sheet is handed over as a 2-D grid of cell values (row 0 is the sheet's
first row, column 0 its first column; ``None``, ``""``, whitespace and NaN
are empty) plus its merged ranges. :func:`detect_blocks` returns the tables
and free-standing text found on it, ordered top to bottom, left to right.

The grid is cut recursively:

* at bands of empty rows, when what follows the band starts a table of its
  own (a header row over typed data, a repeat of the header above, or a row
  of labels after two or more empty rows). Other empty rows are taken to be
  spacing inside one table and stay in it;
* at bands of empty columns, when every piece is recognisable on its own (a
  table with a header, or a line of text) and at least one is a table.

Each block is trimmed to its used range. Inside a block, leading rows that
hold a single text cell are titles when a header row follows them, and the
header is the first row made mostly of text whose columns below it are of
consistent types.
"""

import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: A merged range as (top, left, bottom, right), zero-based and inclusive.
MergedRange = Tuple[int, int, int, int]

Grid = Sequence[Sequence[Any]]

#: Rows examined below a header candidate to judge the column types.
_LOOKAHEAD_ROWS = 25
#: Title rows allowed above a header.
_MAX_TITLE_ROWS = 5
#: Share of a column's cells that must agree for it to count as one type.
_CONSISTENT_SHARE = 0.8
#: A block of at most this many single-cell text rows is text, not a table.
_MAX_TEXT_ROWS = 3
#: Sheets with more runs of rows than this are not cut at empty rows.
_MAX_ROW_RUNS = 2000
#: Merged cells tracked per sheet; ranges beyond the budget are ignored.
_MAX_MERGED_CELLS = 200_000
_MAX_DEPTH = 12


def is_blank(value: Any) -> bool:
    """True for ``None``, NaN, and empty or whitespace-only text."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return value != value
    return False


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (datetime.date, datetime.time, datetime.timedelta)):
        return "date"
    return "other"


@dataclass(frozen=True)
class Block:
    """One table, or a few lines of free-standing text, on a sheet.

    Bounds are zero-based and inclusive. ``kind`` is ``"table"`` or
    ``"text"``. For a table, ``titles`` are the caption lines found above the
    header, ``header`` is the header's row index (``None`` when no header was
    found; the table's rows then start at ``top``), and ``header_strength``
    is ``"strong"`` (text over typed columns), ``"weak"`` (a full row of
    labels over text) or ``None``. For text, ``titles`` holds the lines.
    """

    top: int
    left: int
    bottom: int
    right: int
    kind: str = "table"
    titles: Tuple[str, ...] = ()
    header: Optional[int] = None
    header_strength: Optional[str] = None

    @property
    def body_top(self) -> int:
        """First data row of a table."""
        return self.top if self.header is None else self.header + 1

    @property
    def structured(self) -> bool:
        """Whether the block was recognised (text, or a table with a header)."""
        return self.kind == "text" or self.header is not None


def _runs(indices: Sequence[int]) -> List[Tuple[int, int]]:
    """Maximal runs of consecutive integers in a sorted sequence."""
    runs: List[Tuple[int, int]] = []
    start = previous = indices[0]
    for index in indices[1:]:
        if index != previous + 1:
            runs.append((start, previous))
            start = index
        previous = index
    runs.append((start, previous))
    return runs


class _Sheet:
    def __init__(self, grid: Grid, merges: Sequence[MergedRange]) -> None:
        self.grid = grid
        self.height = len(grid)
        self.width = max((len(row) for row in grid), default=0)
        #: Occupied columns of each row, ascending.
        self.occupied: List[List[int]] = [
            [column for column, value in enumerate(row) if not is_blank(value)]
            for row in grid
        ]
        #: Cells covered by a merged range -> the range's top-left cell.
        self.owner: Dict[Tuple[int, int], Tuple[int, int]] = {}
        covered: Dict[int, List[int]] = {}
        for top, left, bottom, right in merges:
            bottom = min(bottom, self.height - 1)
            right = min(right, self.width - 1)
            if top < 0 or left < 0 or top > bottom or left > right:
                continue
            if is_blank(self._raw(top, left)):
                continue
            cells = (bottom - top + 1) * (right - left + 1)
            if len(self.owner) + cells > _MAX_MERGED_CELLS:
                continue
            for row in range(top, bottom + 1):
                for column in range(left, right + 1):
                    if (row, column) != (top, left):
                        self.owner[(row, column)] = (top, left)
                        covered.setdefault(row, []).append(column)
        for row, columns in covered.items():
            self.occupied[row] = sorted(set(self.occupied[row]).union(columns))

    def _raw(self, row: int, column: int) -> Any:
        cells = self.grid[row]
        return cells[column] if column < len(cells) else None

    def value(self, row: int, column: int) -> Any:
        """The cell's value; a merged cell reports its range's value."""
        origin = self.owner.get((row, column))
        if origin is not None:
            return self._raw(*origin)
        return self._raw(row, column)

    def columns(self, row: int, left: int, right: int) -> List[int]:
        """Occupied columns of ``row`` within ``left..right``."""
        occupied = self.occupied[row]
        if not occupied or (left <= occupied[0] and occupied[-1] <= right):
            return occupied
        return [column for column in occupied if left <= column <= right]

    def _extent(self, top: int, bottom: int, left: int, right: int) -> Tuple[int, int]:
        low, high = right, left
        for row in range(top, bottom + 1):
            columns = self.columns(row, left, right)
            if columns:
                low = min(low, columns[0])
                high = max(high, columns[-1])
        return low, high

    # ------------------------------------------------------------- headers

    def _single_text(self, row: int, left: int, right: int) -> Optional[str]:
        """The row's text if it holds exactly one (possibly merged) text cell."""
        origins = {
            self.owner.get((row, column), (row, column))
            for column in self.columns(row, left, right)
        }
        if len(origins) != 1:
            return None
        value = self._raw(*next(iter(origins)))
        return value.strip() if isinstance(value, str) else None

    def _header_strength(
        self, row: int, bottom: int, left: int, right: int
    ) -> Optional[str]:
        width = right - left + 1
        labels = [self.value(row, column) for column in range(left, right + 1)]
        texts = sum(1 for label in labels if isinstance(label, str) and label.strip())
        if 2 * texts <= width:
            return None

        below: List[int] = []
        for candidate in range(row + 1, bottom + 1):
            if self.columns(candidate, left, right):
                below.append(candidate)
                if len(below) >= _LOOKAHEAD_ROWS:
                    break
        if not below:
            return None

        with_data = consistent = contrast = 0
        for offset, label in enumerate(labels):
            column = left + offset
            counts: Dict[str, int] = {}
            for candidate in below:
                # A merged cell is judged once, where it starts.
                value = self._raw(candidate, column)
                if not is_blank(value):
                    kind = _kind(value)
                    counts[kind] = counts.get(kind, 0) + 1
            if not counts:
                continue
            with_data += 1
            kind, share = max(counts.items(), key=lambda item: item[1])
            if share >= _CONSISTENT_SHARE * sum(counts.values()):
                consistent += 1
                if kind != "text" and isinstance(label, str) and label.strip():
                    contrast += 1
        if 2 * consistent < with_data:
            return None
        if contrast:
            return "strong"
        own = [self._raw(row, column) for column in range(left, right + 1)]
        if (
            width >= 2
            and all(isinstance(value, str) and value.strip() for value in own)
            and len(set(own)) == width
        ):
            return "weak"
        return None

    def find_header(
        self, top: int, bottom: int, left: int, right: int
    ) -> Optional[Tuple[Tuple[str, ...], int, str]]:
        """(titles, header row, strength) for the table starting at ``top``."""
        width = right - left + 1
        titles: List[str] = []
        row = top
        while row <= bottom and len(titles) <= _MAX_TITLE_ROWS:
            if not self.columns(row, left, right):
                row += 1
                continue
            strength = self._header_strength(row, bottom, left, right)
            if strength == "strong" or (strength == "weak" and row == top):
                return tuple(titles), row, strength
            title = self._single_text(row, left, right) if width >= 2 else None
            if title is None:
                return None
            titles.append(title)
            row += 1
        return None

    # -------------------------------------------------------------- cutting

    def _starts_table(
        self,
        above: int,
        run: Tuple[int, int],
        gap: int,
        bottom: int,
        left: int,
        right: int,
    ) -> bool:
        """Whether the rows of ``run``, ``gap`` empty rows below the table
        starting at row ``above``, begin a table of their own."""
        start, end = run
        low, high = self._extent(start, end, left, right)
        found = self.find_header(start, bottom, low, high)
        if found is not None and found[2] == "strong":
            return True
        first = [self._raw(start, column) for column in range(left, right + 1)]
        if sum(1 for value in first if not is_blank(value)) >= 2 and first == [
            self._raw(above, column) for column in range(left, right + 1)
        ]:
            return True
        return gap >= 2 and end > start and found is not None

    def split(
        self, top: int, bottom: int, left: int, right: int, depth: int = 0
    ) -> List[Block]:
        rows = [row for row in range(top, bottom + 1) if self.columns(row, left, right)]
        if not rows:
            return []
        runs = _runs(rows)
        groups: List[List[int]] = [[runs[0][0], runs[0][1]]]
        if len(runs) <= _MAX_ROW_RUNS:
            for previous, run in zip(runs, runs[1:]):
                gap = run[0] - previous[1] - 1
                if self._starts_table(groups[-1][0], run, gap, bottom, left, right):
                    groups.append([run[0], run[1]])
                else:
                    groups[-1][1] = run[1]
        else:
            groups[-1][1] = runs[-1][1]
        if len(groups) > 1:
            return [
                block
                for first, last in groups
                for block in self.split(first, last, left, right, depth + 1)
            ]

        top, bottom = groups[0]
        used = sorted(
            {
                column
                for row in range(top, bottom + 1)
                for column in self.columns(row, left, right)
            }
        )
        column_runs = _runs(used)
        if len(column_runs) > 1 and depth < _MAX_DEPTH:
            pieces = [
                block
                for first, last in column_runs
                for block in self.split(top, bottom, first, last, depth + 1)
            ]
            if all(block.structured for block in pieces) and any(
                block.kind == "table" for block in pieces
            ):
                return pieces
        return [self._leaf(top, bottom, used[0], used[-1])]

    def _leaf(self, top: int, bottom: int, left: int, right: int) -> Block:
        lines: List[str] = []
        for row in range(top, bottom + 1):
            if not self.columns(row, left, right):
                continue
            text = self._single_text(row, left, right)
            if text is None or len(lines) >= _MAX_TEXT_ROWS:
                break
            lines.append(text)
        else:
            return Block(top, left, bottom, right, kind="text", titles=tuple(lines))

        found = self.find_header(top, bottom, left, right)
        if found is None:
            return Block(top, left, bottom, right)
        titles, header, strength = found
        return Block(
            top,
            left,
            bottom,
            right,
            titles=titles,
            header=header,
            header_strength=strength,
        )


def detect_blocks(grid: Grid, merges: Sequence[MergedRange] = ()) -> List[Block]:
    """Find the tables and text blocks of a sheet.

    ``merges`` are the sheet's merged ranges. A merged range counts as
    occupied throughout and carries the value of its top-left cell when rows
    are judged, so a merged title or group header is read as one cell.
    An empty grid gives an empty list.
    """
    sheet = _Sheet(grid, merges)
    if not sheet.height or not sheet.width:
        return []
    return sheet.split(0, sheet.height - 1, 0, sheet.width - 1)
