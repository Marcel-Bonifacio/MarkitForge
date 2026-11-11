# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import datetime
import html
import io
import posixpath
import re
import sys
import zipfile
from contextlib import contextmanager
from typing import BinaryIO, Any, Dict, Iterator, List, Optional, Sequence, Tuple

from defusedxml import ElementTree as ET

from ._html_converter import HtmlConverter
from ..converter_utils.docx.core_properties import read_core_properties_from_zip
from .._base_converter import (
    DocumentConverter,
    DocumentConverterResult,
    DocumentSection,
    page_marker,
)
from .._exceptions import (
    MissingDependencyException,
    MISSING_DEPENDENCY_MESSAGE,
    ResourceLimitException,
)
from .._progress import check_cancelled, report_progress
from .._stream_info import StreamInfo
from ..converter_utils._archive_limits import (
    ArchiveBudget,
    ImageAssets,
    check_archive_stream,
    copy_member,
    get_budget,
    read_member,
    wants_images,
)
from ..converter_utils._sheet_layout import Block, MergedRange, detect_blocks
from ..converter_utils._xlsx_images import _relationships

# Try loading optional (but in this case, required) dependencies
# Save reporting of any exceptions for later
_xlsx_dependency_exc_info = None
try:
    import pandas as pd
    import openpyxl  # noqa: F401
    from pandas.errors import EmptyDataError
    from pandas.io.parsers import TextParser
except ImportError:
    _xlsx_dependency_exc_info = sys.exc_info()

_xls_dependency_exc_info = None
try:
    import pandas as pd  # noqa: F811
    import xlrd  # noqa: F401
except ImportError:
    _xls_dependency_exc_info = sys.exc_info()

ACCEPTED_XLSX_MIME_TYPE_PREFIXES = [
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
]
ACCEPTED_XLSX_FILE_EXTENSIONS = [".xlsx"]

ACCEPTED_XLS_MIME_TYPE_PREFIXES = [
    "application/vnd.ms-excel",
    "application/excel",
]
ACCEPTED_XLS_FILE_EXTENSIONS = [".xls"]


# Some producers write the legacy attribute "showZeroes" on <sheetView>, where the
# schema calls it "showZeros". openpyxl rejects the unknown attribute outright, so the
# workbook is repaired by renaming it. The rename is confined to <sheetView> start tags.
_SHEET_VIEW_START_TAG = re.compile(rb"<sheetView(?=[\s/>])[^>]*>")
_SHOW_ZEROES_ATTRIBUTE = re.compile(rb"(?<=[\s])showZeroes(\s*=)")


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_HIDDEN_STATES = ("hidden", "veryHidden")


def _read_workbook_info(
    file_stream: BinaryIO,
) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """Return ([(sheet name, state), ...] in workbook order, core properties).

    State is "visible", "hidden" or "veryHidden" (openpyxl's ``sheet_state``
    values), read straight from the package without loading any sheet. The
    stream position is restored. On any problem the sheet list is empty, so
    callers fall back to reading every sheet.
    """
    position = file_stream.tell()
    sheets: List[Tuple[str, str]] = []
    props: Dict[str, Any] = {}
    try:
        with zipfile.ZipFile(file_stream) as archive:
            props = read_core_properties_from_zip(archive)
            workbook_part = "xl/workbook.xml"
            try:
                rels = ET.fromstring(archive.read("_rels/.rels"))
                for rel in rels:
                    if rel.get("Type", "").endswith("/officeDocument"):
                        workbook_part = posixpath.normpath(
                            rel.get("Target", workbook_part)
                        ).lstrip("/")
                        break
            except Exception:
                pass
            root = ET.fromstring(archive.read(workbook_part))
            container = root.find(_SHEET_NS + "sheets")
            if container is not None:
                for sheet in container.findall(_SHEET_NS + "sheet"):
                    name = sheet.get("name")
                    if name is not None:
                        sheets.append((name, sheet.get("state") or "visible"))
    except Exception:
        sheets = []
    finally:
        file_stream.seek(position)
    return sheets, props


@contextmanager
def _read_xlsx_sheets(
    file_stream: BinaryIO,
    sheet_name: Optional[List[str]] = None,
    budget: Optional[ArchiveBudget] = None,
) -> Iterator[tuple[dict[str, Any], BinaryIO]]:
    if sheet_name is not None and not sheet_name:
        # Every sheet was filtered out; there is nothing to read.
        yield {}, file_stream
        return
    start_pos = file_stream.tell()
    repaired_stream = None
    try:
        try:
            sheets = pd.read_excel(
                file_stream, sheet_name=sheet_name, engine="openpyxl"
            )
        except TypeError as exc:
            if "showZeroes" not in str(exc):
                raise
            repaired_stream = _repair_sheetview_show_zeroes(
                file_stream, start_pos, budget
            )
            sheets = pd.read_excel(
                repaired_stream, sheet_name=sheet_name, engine="openpyxl"
            )
        yield sheets, repaired_stream if repaired_stream is not None else file_stream
    finally:
        if repaired_stream is not None:
            repaired_stream.close()


def _rename_show_zeroes_attribute(data: bytes) -> bytes:
    return _SHEET_VIEW_START_TAG.sub(
        lambda match: _SHOW_ZEROES_ATTRIBUTE.sub(rb"showZeros\1", match.group(0)),
        data,
    )


def _repair_sheetview_show_zeroes(
    file_stream: BinaryIO,
    start_pos: int = 0,
    budget: Optional[ArchiveBudget] = None,
) -> io.BytesIO:
    """Copy the workbook, renaming ``showZeroes`` in worksheet parts.

    Only worksheet parts are read into memory; every other member is
    stream-copied in chunks, within the archive member limits.
    """
    file_stream.seek(start_pos)
    repaired_stream = io.BytesIO()
    if budget is None:
        budget = ArchiveBudget()

    with zipfile.ZipFile(file_stream) as source:
        with zipfile.ZipFile(repaired_stream, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                budget.check_member(item)
                if item.filename.startswith(
                    "xl/worksheets/"
                ) and item.filename.endswith(".xml"):
                    data = read_member(source, item, budget)
                    if b"showZeroes" in data:
                        data = _rename_show_zeroes_attribute(data)
                    target.writestr(item, data)
                else:
                    copy_member(source, item, target, budget)

    repaired_stream.seek(0)
    return repaired_stream


def _select_sheets(
    states: List[Tuple[str, str]], include_hidden: bool
) -> Tuple[Optional[List[str]], List[str]]:
    """Return (sheet names to read, or None for all; hidden sheet names)."""
    hidden = [name for name, state in states if state in _HIDDEN_STATES]
    if not states or include_hidden:
        return None, hidden
    return [name for name, state in states if state not in _HIDDEN_STATES], hidden


def _hidden_sheet_warning(hidden: List[str], include_hidden: bool) -> List[str]:
    if not hidden or include_hidden:
        return []
    return [
        f"Skipped {len(hidden)} hidden sheet(s): {', '.join(hidden)}. "
        "Use include_hidden_sheets=True to include them."
    ]


# Whole numbers at or above this size are left to pandas' float formatting.
_MAX_EXACT_INT = 1e15


def _is_whole(value: Any) -> bool:
    return (
        isinstance(value, float)
        and value == value  # not NaN
        and value.is_integer()
        and abs(value) < _MAX_EXACT_INT
    )


def _format_float(value: float) -> str:
    return str(int(value)) if _is_whole(value) else format(value, ".15g")


def _whole_numbers_as_ints(frame: Any) -> Any:
    """Undo pandas' float upcasting of integer columns that contain blanks.

    A column of integers with an empty cell is read as float64, so 4 would
    print as "4.0". In float columns and in mixed (object) columns, whole
    numbers are printed as integers ("4") and fractions are kept ("4.5").
    Rewritten columns hold strings, with "" for empty cells.
    """
    out = None
    for position in range(len(frame.columns)):
        series = frame.iloc[:, position]
        kind = series.dtype.kind
        if kind == "f":
            if series.isna().all():
                continue
            cells = ["" if v != v else _format_float(float(v)) for v in series]
        elif kind == "O":
            if not any(_is_whole(v) for v in series):
                continue
            cells = [
                (
                    ""
                    if v is None or (isinstance(v, float) and v != v)
                    else str(int(v))
                    if _is_whole(v)
                    else v
                )
                for v in series
            ]
        else:
            continue
        if out is None:
            out = frame.copy()
        out.isetitem(position, pd.Series(cells, index=series.index, dtype=object))
    return frame if out is None else out


def _frame_to_html(frame: Any) -> str:
    # Empty cells must stay empty: na_rep covers NaN, None, NaT and pd.NA.
    return _whole_numbers_as_ints(frame).to_html(index=False, na_rep="")


# Accepted values for the ``sheet_layout`` option.
#   auto:   find the tables on each sheet (titles, headers, several tables)
#   simple: one table per sheet with the first row as its header
SHEET_LAYOUTS = ("auto", "simple")

_SPAN_TABLE_FORMATS = ("html", "auto")

_MERGE_CELL = re.compile(
    rb"<(?:\w+:)?mergeCell\s[^>]*?ref=\"([A-Za-z]{1,3})(\d{1,7})"
    rb"(?::([A-Za-z]{1,3})(\d{1,7}))?\""
)
_MANGLED_LABEL = re.compile(r"^(.*)\.(\d+)$")

# A sheet whose first cell is a title above its only table keeps the first-row
# header it always had ("Title | Unnamed: 1"); output for such sheets is relied
# on downstream. Set to False to caption the title and start the table at its
# real header instead, as is already done on sheets with several tables.
_FIRST_CELL_TITLE_STAYS_HEADER = False

# Merged ranges read per sheet; a sheet with more keeps the first ones.
_MAX_MERGED_RANGES = 20000


def _sheet_layout_option(kwargs: Dict[str, Any]) -> str:
    layout = kwargs.get("sheet_layout") or "auto"
    if layout not in SHEET_LAYOUTS:
        raise ValueError(
            f"sheet_layout must be one of {', '.join(SHEET_LAYOUTS)}; got {layout!r}"
        )
    return layout


def _column_index(letters: bytes) -> int:
    index = 0
    for letter in letters.upper():
        index = index * 26 + letter - 64
    return index - 1


def _read_merged_ranges(
    file_stream: BinaryIO, names: Sequence[str], budget: ArchiveBudget
) -> Dict[str, List[MergedRange]]:
    """Merged ranges of the named sheets, read from the worksheet parts.

    The workbook is not loaded a second time: the ``<mergeCell>`` references
    are picked out of each sheet part, read within the archive member limits.
    The stream position is restored. A package that cannot be walked gives no
    ranges; a limit hit is raised.
    """
    wanted = set(names)
    merged: Dict[str, List[MergedRange]] = {}
    position = file_stream.tell()
    try:
        with zipfile.ZipFile(file_stream) as archive:
            workbook_part = next(
                iter(_relationships(archive, "", "officeDocument").values())
            )
            parts = _relationships(archive, workbook_part)
            root = ET.fromstring(archive.read(workbook_part))
            for sheet in root.findall(f"{_SHEET_NS}sheets/{_SHEET_NS}sheet"):
                name = sheet.get("name")
                part = parts.get(sheet.get(_REL_NS + "id") or "")
                if name not in wanted or part is None:
                    continue
                ranges = _scan_merge_cells(archive, archive.getinfo(part), budget)
                if ranges:
                    merged[name] = ranges
    except ResourceLimitException:
        raise
    except Exception:
        return {}
    finally:
        file_stream.seek(position)
    return merged


# Longest <mergeCell .../> reference worth matching across a chunk boundary.
_MERGE_SCAN_OVERLAP = 256
_MERGE_SCAN_CHUNK = 1 << 20


def _scan_merge_cells(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: ArchiveBudget
) -> List[MergedRange]:
    """Merged ranges of one worksheet part, read in chunks.

    The part is streamed (it can be hundreds of MB) and only the matched
    references are kept. The same member caps apply as for ``read_member``.
    """
    cap = min(info.file_size, budget.limits.max_archive_member_bytes)
    ranges: List[MergedRange] = []
    total = 0
    tail = b""
    with archive.open(info) as source:
        while len(ranges) < _MAX_MERGED_RANGES:
            chunk = source.read(_MERGE_SCAN_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise ResourceLimitException(
                    f"Archive member {info.filename!r} inflates past {cap} bytes "
                    "(its declared size or max_archive_member_bytes)."
                )
            window = tail + chunk
            for match in _MERGE_CELL.finditer(window):
                if match.end() <= len(tail):
                    continue  # already matched in the previous window
                left, top, right, bottom = match.groups()
                first_row, first_column = int(top) - 1, _column_index(left)
                last_row = int(bottom) - 1 if bottom else first_row
                last_column = _column_index(right) if right else first_column
                ranges.append((first_row, first_column, last_row, last_column))
                if len(ranges) >= _MAX_MERGED_RANGES:
                    break
            tail = window[-_MERGE_SCAN_OVERLAP:]
    return ranges


def _header_labels(columns: Sequence[Any]) -> List[Any]:
    """The first row's cells, recovered from the column labels pandas made.

    pandas names an empty header cell ``Unnamed: <position>`` and numbers
    repeated labels (``Name``, ``Name.1``); both are undone here.
    """
    labels: List[Any] = []
    seen: Dict[str, int] = {}
    for position, label in enumerate(columns):
        if isinstance(label, str):
            if label == f"Unnamed: {position}":
                labels.append(None)
                continue
            match = _MANGLED_LABEL.match(label)
            if match and seen.get(match.group(1)) == int(match.group(2)):
                label = match.group(1)
            seen[label] = seen.get(label, 0) + 1
        labels.append(label)
    return labels


def _frame_grid(frame: Any) -> List[List[Any]]:
    """The sheet as rows of cell values (``None`` for empty), header row first."""
    columns: List[List[Any]] = []
    for position in range(frame.shape[1]):
        series = frame.iloc[:, position]
        blank = series.isna().tolist()
        values = series.tolist()
        kind = series.dtype.kind
        if kind == "f":
            # Integers were upcast to float by an empty cell in the column.
            cells = [
                None if empty else int(value) if _is_whole(value) else value
                for value, empty in zip(values, blank)
            ]
        elif kind == "M":
            cells = [
                None if empty else value.to_pydatetime()
                for value, empty in zip(values, blank)
            ]
        else:
            cells = [None if empty else value for value, empty in zip(values, blank)]
        columns.append(cells)
    grid = [_header_labels(frame.columns.tolist())]
    grid.extend(list(row) for row in zip(*columns))
    return grid


def _is_plain_table(frame: Any) -> bool:
    """Whether the sheet is evidently one table with a full header row.

    True when every header cell is text and no row or column is empty; such
    a sheet needs no layout analysis.
    """
    if frame.shape[1] == 0:
        return True
    for position, label in enumerate(frame.columns.tolist()):
        if (
            not isinstance(label, str)
            or not label.strip()
            or label == f"Unnamed: {position}"
        ):
            return False
    if frame.shape[0] == 0:
        return True
    filled = frame.notna().to_numpy()
    return bool(filled.any(axis=1).all() and filled.any(axis=0).all())


def _label_text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    return value


def _caption_html(lines: Sequence[str]) -> str:
    return "".join(f"<p><strong>{html.escape(line)}</strong></p>" for line in lines)


Span = Tuple[int, int, int, int]  # table row, table column, rowspan, colspan


def _table_spans(
    merges: Sequence[MergedRange],
    first_row: int,
    header_row: Optional[int],
    bottom: int,
    left: int,
    right: int,
) -> List[Span]:
    """Merged ranges as spans of the table whose first sheet row is ``first_row``.

    Without a header row the table starts with a generated header, so its
    rows are one further down. A range is clipped to the table, and a range
    starting in the header row is clipped to that row.
    """
    shift = 1 if header_row is None else 0
    spans: List[Span] = []
    for top, low, last, high in merges:
        if not (first_row <= top <= bottom and left <= low <= right):
            continue
        last = top if top == header_row else min(last, bottom)
        high = min(high, right)
        if last > top or high > low:
            spans.append(
                (top - first_row + shift, low - left, last - top + 1, high - low + 1)
            )
    return spans


def _with_spans(table_html: str, spans: Sequence[Span]) -> str:
    """Add ``rowspan``/``colspan`` to a rectangular HTML table.

    The top-left cell of each span is extended and the cells it covers are
    removed. Spans that overlap an earlier one are skipped.
    """
    if not spans:
        return table_html
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(table_html, "html.parser")
    rows = [row.find_all(["th", "td"], recursive=False) for row in soup.find_all("tr")]
    covered: set[Tuple[int, int]] = set()
    for row, column, rowspan, colspan in spans:
        if row >= len(rows) or column >= len(rows[row]) or (row, column) in covered:
            continue
        cells = [
            (r, c)
            for r in range(row, min(row + rowspan, len(rows)))
            for c in range(column, min(column + colspan, len(rows[r])))
            if (r, c) != (row, column)
        ]
        if any(cell in covered for cell in cells):
            continue
        if rowspan > 1:
            rows[row][column]["rowspan"] = str(rowspan)
        if colspan > 1:
            rows[row][column]["colspan"] = str(colspan)
        covered.update(cells)
    for r, c in covered:
        rows[r][c].decompose()
    return str(soup)


def _block_html(
    grid: List[List[Any]],
    block: Block,
    merges: Sequence[MergedRange],
    spans: bool,
) -> str:
    """One detected table as HTML, typed and formatted like a whole sheet."""
    left, right = block.left, block.right
    body = [
        ["" if value is None else value for value in grid[row][left : right + 1]]
        for row in range(block.body_top, block.bottom + 1)
    ]
    try:
        frame = TextParser(body, header=None, skip_blank_lines=False).read()
    except EmptyDataError:
        frame = pd.DataFrame(index=range(len(body)), columns=range(right - left + 1))
    if block.header is None:
        frame.columns = [f"Column {number}" for number in range(1, frame.shape[1] + 1)]
    else:
        frame.columns = [
            _label_text(value) for value in grid[block.header][left : right + 1]
        ]
    table_html = _frame_to_html(frame)
    if spans and merges:
        first_row = block.top if block.header is None else block.header
        table_html = _with_spans(
            table_html,
            _table_spans(merges, first_row, block.header, block.bottom, left, right),
        )
    return table_html


def _is_first_row_table(blocks: Sequence[Block]) -> bool:
    """Whether the sheet is rendered as one table headed by its first row."""
    if blocks[0].top != 0 or blocks[0].left != 0:
        return False
    if len(blocks) == 1 and (blocks[0].kind == "text" or blocks[0].header in (None, 0)):
        return True
    if not _FIRST_CELL_TITLE_STAYS_HEADER:
        return False
    # Title lines in the first column, then exactly one table below them.
    tables = [block for block in blocks if block.kind == "table"]
    return len(tables) == 1 and all(
        block.left == 0 and (block is tables[0] or block.bottom < tables[0].top)
        for block in blocks
    )


def _sheet_fragments(
    frame: Any,
    merges: Sequence[MergedRange] = (),
    layout: str = "auto",
    spans: bool = False,
) -> List[str]:
    """HTML fragments for one sheet: one per table or text block, in order.

    With ``layout="simple"``, and for a sheet that is one table starting in
    its first cell, this is the single table pandas read, with the first row
    as header (see also ``_FIRST_CELL_TITLE_STAYS_HEADER``). Otherwise each
    detected block becomes a fragment: its title lines in bold, then its
    table. A table without a header row gets ``Column 1``, ``Column 2``, ...
    as header; empty cells of a detected header stay empty. With ``spans``,
    merged ranges become ``rowspan``/``colspan``.
    """
    if layout == "simple" or (not merges and _is_plain_table(frame)):
        return [_frame_to_html(frame)]

    grid = _frame_grid(frame)
    blocks = detect_blocks(grid, merges)
    if not blocks or _is_first_row_table(blocks):
        shown = frame
        if (
            len(blocks) == 1
            and blocks[0].header == 0
            and blocks[0].header_strength == "strong"
            and None in grid[0]
        ):
            # A real header with gaps: the gaps are empty cells, not names.
            shown = frame.copy(deep=False)
            shown.columns = [
                "" if cell is None else label
                for cell, label in zip(grid[0], frame.columns.tolist())
            ]
        table_html = _frame_to_html(shown)
        if spans and merges:
            table_html = _with_spans(
                table_html,
                _table_spans(merges, 0, 0, len(grid) - 1, 0, frame.shape[1] - 1),
            )
        return [table_html]

    fragments: List[str] = []
    for block in blocks:
        caption = _caption_html(block.titles)
        if block.kind == "text":
            fragments.append(caption)
        else:
            fragments.append(caption + _block_html(grid, block, merges, spans))
    return fragments


class XlsxConverter(DocumentConverter):
    """
    Converts XLSX files to Markdown, with each sheet presented as a separate Markdown table.

    A sheet that holds several tables (stacked or side by side), titles above
    a table, or a table that does not start in the first cell is laid out
    table by table; ``sheet_layout="simple"`` restores one table per sheet
    with the first row as header.

    The workbook is checked against the archive resource limits before any
    part is read; a hit raises ``ResourceLimitException``.

    With ``extract_images`` every distinct image anchored in a sheet becomes
    an asset (``sheet1_img1.png``), listed after the sheet's tables.
    """

    def __init__(self):
        super().__init__()
        self._html_converter = HtmlConverter()

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_XLSX_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_XLSX_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        # Check the dependencies
        if _xlsx_dependency_exc_info is not None:
            raise MissingDependencyException(
                MISSING_DEPENDENCY_MESSAGE.format(
                    converter=type(self).__name__,
                    extension=".xlsx",
                    feature="xlsx",
                )
            ) from _xlsx_dependency_exc_info[1].with_traceback(  # type: ignore[union-attr]
                _xlsx_dependency_exc_info[2]
            )

        # Reject archive bombs before anything reads a member.
        budget = get_budget(kwargs)
        check_archive_stream(file_stream, budget)

        include_hidden = bool(kwargs.get("include_hidden_sheets", False))
        markers = bool(kwargs.get("page_markers", False))
        layout = _sheet_layout_option(kwargs)
        spans = kwargs.get("table_format") in _SPAN_TABLE_FORMATS
        states, props = _read_workbook_info(file_stream)
        wanted, hidden = _select_sheets(states, include_hidden)

        md_content = ""
        sections: List[DocumentSection] = []
        with _read_xlsx_sheets(file_stream, wanted, budget) as (
            sheets,
            workbook_stream,
        ):
            images = None
            hooked = type(self)._image_to_html is not XlsxConverter._image_to_html
            export = ImageAssets() if wants_images(kwargs) else None
            if hooked or export is not None:
                from ..converter_utils._xlsx_images import _XlsxImages

                images = _XlsxImages(workbook_stream)

            merged: Dict[str, List[MergedRange]] = {}
            if layout == "auto" and sheets:
                merged = _read_merged_ranges(
                    workbook_stream, [str(s) for s in sheets], budget
                )

            for index, s in enumerate(sheets, start=1):
                check_cancelled(kwargs)
                chunk = f"## {s}\n"
                for html_content in _sheet_fragments(
                    sheets[s], merged.get(str(s), ()), layout, spans
                ):
                    chunk += (
                        self._html_converter.convert_string(
                            html_content, **kwargs
                        ).markdown.strip()
                        + "\n\n"
                    )
                if images is not None and export is not None:
                    names = [
                        export.add(
                            data,
                            info.mimetype,
                            prefix=f"sheet{index}_img",
                            page=index,
                        )
                        for data, info in images._sheets.get(str(s), [])
                    ]
                    if names and not hooked:
                        chunk += (
                            "### Images in this sheet:\n\n"
                            + "\n\n".join(
                                f"![]({name})" for name in dict.fromkeys(names)
                            )
                            + "\n\n"
                        )
                if images is not None and hooked:
                    image_content = images.to_html(s, self._image_to_html, kwargs)
                    if image_content:
                        chunk += (
                            self._html_converter.convert_string(
                                image_content, **kwargs
                            ).markdown.strip()
                            + "\n\n"
                        )
                if markers:
                    md_content += page_marker(index, "sheet") + "\n"
                md_content += chunk
                sections.append(
                    DocumentSection(
                        index=index, markdown=chunk.strip(), title=str(s), kind="sheet"
                    )
                )
                report_progress(kwargs, "sheet", index, len(sheets))

        metadata: Dict[str, Any] = dict(props)
        metadata["sheet_names"] = (
            [name for name, _ in states] if states else [str(s) for s in sheets]
        )
        metadata["hidden_sheets"] = hidden
        metadata["sheet_count"] = len(metadata["sheet_names"])
        return DocumentConverterResult(
            markdown=md_content.strip(),
            title=props.get("title"),
            metadata=metadata,
            pages=sections,
            assets=export.assets if export is not None else [],
            warnings=_hidden_sheet_warning(hidden, include_hidden),
        )

    def _image_to_html(
        self,
        image_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> Optional[str]:
        """Override to render an embedded image as an HTML fragment.

        The stream is borrowed, seekable, and positioned at zero; do not close
        or retain it. StreamInfo describes the image, not the workbook. Existing
        conversion options are forwarded through kwargs.

        Return None or blank text to retain the native representation (no image
        output). Otherwise return HTML, escaping any literal text. Images appear
        after their sheet's table, in the existing worksheet image order. Linked
        images are not fetched. Hook failures propagate through the normal
        conversion failure path.
        """
        return None


def _read_xls_merged_ranges(contents: bytes) -> Dict[str, List[MergedRange]]:
    """Merged ranges per sheet of a legacy workbook.

    xlrd only records them when the workbook is opened with its formatting,
    so the file is parsed once more; this is done only when spans are wanted.
    A workbook xlrd cannot open that way gives no ranges.
    """
    try:
        book = xlrd.open_workbook(file_contents=contents, formatting_info=True)
        return {
            sheet.name: [
                (top, left, bottom - 1, right - 1)
                for top, bottom, left, right in sheet.merged_cells[:_MAX_MERGED_RANGES]
            ]
            for sheet in book.sheets()
            if sheet.merged_cells
        }
    except Exception:
        return {}


class XlsConverter(DocumentConverter):
    """
    Converts XLS files to Markdown, with each sheet presented as a separate Markdown table.

    Sheets are laid out as for XLSX (see ``sheet_layout``). Merged cells are
    only read for ``table_format="html"`` or ``"auto"``.
    """

    def __init__(self):
        super().__init__()
        self._html_converter = HtmlConverter()

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_XLS_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_XLS_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        # Load the dependencies
        if _xls_dependency_exc_info is not None:
            raise MissingDependencyException(
                MISSING_DEPENDENCY_MESSAGE.format(
                    converter=type(self).__name__,
                    extension=".xls",
                    feature="xls",
                )
            ) from _xls_dependency_exc_info[1].with_traceback(  # type: ignore[union-attr]
                _xls_dependency_exc_info[2]
            )

        include_hidden = bool(kwargs.get("include_hidden_sheets", False))
        markers = bool(kwargs.get("page_markers", False))
        layout = _sheet_layout_option(kwargs)
        spans = kwargs.get("table_format") in _SPAN_TABLE_FORMATS

        # Open the book once with xlrd so sheet visibility is known, then hand
        # the same Book to pandas. xlrd visibility: 0 visible, 1 hidden,
        # 2 very hidden.
        contents = file_stream.read()
        book = xlrd.open_workbook(file_contents=contents)
        states: List[Tuple[str, str]] = []
        for index in range(book.nsheets):
            visibility = getattr(book.sheet_by_index(index), "visibility", 0)
            state = {1: "hidden", 2: "veryHidden"}.get(visibility, "visible")
            states.append((book.sheet_names()[index], state))
        wanted, hidden = _select_sheets(states, include_hidden)
        author = (getattr(book, "user_name", "") or "").strip()

        sheets: Dict[str, Any] = (
            {}
            if wanted == []
            else pd.read_excel(book, sheet_name=wanted, engine="xlrd")
        )
        merged: Dict[str, List[MergedRange]] = {}
        if layout == "auto" and spans and sheets:
            merged = _read_xls_merged_ranges(contents)
        md_content = ""
        sections: List[DocumentSection] = []
        for index, s in enumerate(sheets, start=1):
            check_cancelled(kwargs)
            chunk = f"## {s}\n"
            for html_content in _sheet_fragments(
                sheets[s], merged.get(str(s), ()), layout, spans
            ):
                chunk += (
                    self._html_converter.convert_string(
                        html_content, **kwargs
                    ).markdown.strip()
                    + "\n\n"
                )
            if markers:
                md_content += page_marker(index, "sheet") + "\n"
            md_content += chunk
            sections.append(
                DocumentSection(
                    index=index, markdown=chunk.strip(), title=str(s), kind="sheet"
                )
            )
            report_progress(kwargs, "sheet", index, len(sheets))

        metadata: Dict[str, Any] = {
            "sheet_names": [name for name, _ in states],
            "hidden_sheets": hidden,
            "sheet_count": len(states),
        }
        if author:
            metadata["author"] = author
        return DocumentConverterResult(
            markdown=md_content.strip(),
            metadata=metadata,
            pages=sections,
            warnings=_hidden_sheet_warning(hidden, include_hidden),
        )
