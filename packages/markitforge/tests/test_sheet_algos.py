# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Spreadsheet table detection (``sheet_layout``) and the DOCX page count.

mf_sheet_layouts.xlsx/.xls are built by
tests/test_files/mf_sheet_generate_fixtures.py; every other workbook is made
with openpyxl at test time.
"""

import datetime
import io
import zipfile
from pathlib import Path
from typing import Any, List, Optional, Sequence

import pytest
from openpyxl import Workbook

from markitforge import MarkitForge, StreamInfo
from markitforge.converter_utils import _sheet_layout
from markitforge.converter_utils._sheet_layout import Block, detect_blocks
from markitforge.converter_utils.docx import page_breaks
from markitforge.converters import DocxConverter, XlsConverter, XlsxConverter
from markitforge.converters import _libreoffice_converter, _xlsx_converter

FILES = Path(__file__).parent / "test_files"
HAS_SOFFICE = _libreoffice_converter.find_soffice() is not None
needs_soffice = pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")

PARTS: List[List[Any]] = [
    ["Part", "Qty", "Price"],
    ["Bolt", 10, 0.5],
    ["Nut", 4, 0.25],
    ["Gear", None, 12],
]
CITIES: List[List[Any]] = [
    ["City", "Population", "Twinned"],
    ["Oslo", 700000, datetime.datetime(2001, 3, 4)],
    ["Bergen", 285000, datetime.datetime(2010, 5, 2)],
]
PARTS_MD = (
    "| Part | Qty | Price |\n"
    "| --- | --- | --- |\n"
    "| Bolt | 10 | 0.5 |\n"
    "| Nut | 4 | 0.25 |\n"
    "| Gear |  | 12 |"
)
CITIES_MD = (
    "| City | Population | Twinned |\n"
    "| --- | --- | --- |\n"
    "| Oslo | 700000 | 2001-03-04 |\n"
    "| Bergen | 285000 | 2010-05-02 |"
)


def _grid(height: int, width: int, *tables: Any) -> List[List[Any]]:
    """An empty grid with (top, left, rows) tables written into it."""
    grid: List[List[Any]] = [[None] * width for _ in range(height)]
    for top, left, rows in tables:
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                grid[top + i][left + j] = value
    return grid


def _bounds(blocks: Sequence[Block]) -> List[Any]:
    return [(b.top, b.left, b.bottom, b.right, b.kind, b.header) for b in blocks]


def _put(ws: Any, top: int, left: int, rows: Sequence[Sequence[Any]]) -> None:
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            if value is not None:
                ws.cell(top + i, left + j, value)


def _workbook(*fills: Any) -> bytes:
    """A workbook with one sheet (S1, S2, ...) per ``fill(ws)`` callback."""
    wb = Workbook()
    for index, fill in enumerate(fills, start=1):
        ws = wb.active if index == 1 else wb.create_sheet()
        ws.title = f"S{index}"
        fill(ws)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _xlsx(data: bytes, **kwargs: Any) -> Any:
    return XlsxConverter().convert(
        io.BytesIO(data), StreamInfo(extension=".xlsx"), **kwargs
    )


def _fixture(name: str, **kwargs: Any) -> Any:
    converter = XlsxConverter() if name.endswith(".xlsx") else XlsConverter()
    with open(FILES / name, "rb") as fh:
        return converter.convert(
            fh, StreamInfo(extension=Path(name).suffix, filename=name), **kwargs
        )


# ----------------------------------------------------------- block detection


def test_blocks_trim_to_the_used_range() -> None:
    grid = _grid(12, 9, (2, 3, PARTS))
    grid[10][7] = "   "  # whitespace is as empty as None
    grid[11][8] = float("nan")
    assert _bounds(detect_blocks(grid)) == [(2, 3, 5, 5, "table", 2)]
    assert detect_blocks([]) == [] and detect_blocks([[None, ""], [None, None]]) == []


def test_blocks_split_side_by_side_and_stacked_tables_in_reading_order() -> None:
    side = detect_blocks(_grid(4, 8, (0, 0, PARTS), (0, 5, CITIES)))
    assert _bounds(side) == [(0, 0, 3, 2, "table", 0), (0, 5, 2, 7, "table", 0)]

    stacked = detect_blocks(_grid(9, 3, (0, 0, PARTS), (6, 0, CITIES)))
    assert _bounds(stacked) == [(0, 0, 3, 2, "table", 0), (6, 0, 8, 2, "table", 6)]

    # Two rows of two tables: top-left, top-right, bottom-left, bottom-right.
    four = detect_blocks(
        _grid(10, 8, (0, 0, PARTS), (0, 5, CITIES), (6, 0, CITIES), (6, 5, PARTS))
    )
    assert [(b.top, b.left) for b in four] == [(0, 0), (0, 5), (6, 0), (6, 5)]


def test_blocks_keep_spacing_rows_inside_one_table() -> None:
    # An empty row followed by more data rows is not a new table ...
    grid = _grid(6, 3, (0, 0, PARTS), (5, 0, [["Washer", 3, 0.1]]))
    assert _bounds(detect_blocks(grid)) == [(0, 0, 5, 2, "table", 0)]
    # ... and scattered label/value cells are not cut into strips.
    form = _grid(
        3, 3, (0, 0, [["Name:", None, "Ada"]]), (2, 0, [["Dept:", None, "R&D"]])
    )
    assert _bounds(detect_blocks(form)) == [(0, 0, 2, 2, "table", None)]


def test_blocks_split_a_repeated_header_and_text_tables_after_a_wide_gap() -> None:
    people = [["Name", "Town"], ["Ada", "Oslo"], ["Bo", "Bergen"]]
    one_gap = detect_blocks(_grid(7, 2, (0, 0, people), (4, 0, people)))
    assert [b.top for b in one_gap] == [0, 4]  # the header repeats

    pets = [["Pet", "Owner"], ["Rex", "Ada"]]
    assert len(detect_blocks(_grid(7, 2, (0, 0, people), (4, 0, pets)))) == 1
    assert [
        b.top for b in detect_blocks(_grid(8, 2, (0, 0, people), (5, 0, pets)))
    ] == [
        0,
        5,
    ]


def test_title_rows_and_header_detection() -> None:
    grid = _grid(7, 3, (0, 0, [["Stock report"], ["As of March"]]), (2, 0, PARTS))
    (block,) = detect_blocks(grid)
    assert block.titles == ("Stock report", "As of March")
    assert (block.header, block.header_strength, block.body_top) == (2, "strong", 3)

    # A title separated by an empty row is a text block of its own.
    grid = _grid(7, 4, (0, 1, [["Stock report"]]), (2, 1, PARTS))
    text, table = detect_blocks(grid)
    assert (text.kind, text.titles) == ("text", ("Stock report",))
    assert (table.kind, table.top, table.header) == ("table", 2, 2)

    # Numbers only: no header. Text over text: a weak header.
    (bare,) = detect_blocks(_grid(2, 3, (0, 0, [[1, 2, 3], [4, 5, 6]])))
    assert bare.header is None and not bare.structured
    (weak,) = detect_blocks(_grid(2, 2, (0, 0, [["Name", "Town"], ["Ada", "Oslo"]])))
    assert (weak.header, weak.header_strength) == (0, "weak")
    # A data row of mostly text is not mistaken for a header.
    (rows,) = detect_blocks(
        _grid(2, 3, (0, 0, [["Bolt", "steel", 1], ["Nut", "zinc", 2]]))
    )
    assert rows.header is None


def test_merged_ranges_count_as_one_cell() -> None:
    grid = _grid(4, 3, (0, 0, [["Stock report"]]), (1, 0, PARTS[:3]))
    (block,) = detect_blocks(grid, [(0, 0, 0, 2)])
    assert block.titles == ("Stock report",) and block.header == 1

    # A merged group header keeps its columns together.
    halves = [["Region", "H1", None, "H2", None], ["North", 1, 2, 3, 4]]
    (block,) = detect_blocks(_grid(2, 5, (0, 0, halves)), [(0, 1, 0, 2), (0, 3, 0, 4)])
    assert (block.left, block.right, block.header) == (0, 4, 0)

    # A merged footnote under a table does not hide the table's header.
    grid = _grid(7, 3, (0, 0, PARTS), (6, 0, [["Prices in EUR"]]))
    (block,) = detect_blocks(grid, [(6, 0, 6, 2)])
    assert (block.header, block.header_strength) == (0, "strong")


# ------------------------------------------------------ XLSX / XLS rendering


@pytest.mark.parametrize("name", ["mf_sheet_layouts.xlsx", "mf_sheet_layouts.xls"])
def test_layout_fixture_renders_each_table(name: str) -> None:
    result = _fixture(name)
    assert result.markdown == (
        f"## Plain\n{PARTS_MD}\n\n"
        f"## Side\n{PARTS_MD}\n\n{CITIES_MD}\n\n"
        f"## Stacked\n{PARTS_MD}\n\n{CITIES_MD}\n\n"
        f"## Titled\n**Stock report**\n\n{PARTS_MD}\n\n"
        "## Bare\n"
        "| Column 1 | Column 2 | Column 3 |\n"
        "| --- | --- | --- |\n"
        "| 1 | 2 | 3 |\n"
        "| 4 | 5.5 | 6 |\n\n"
        "## Merged\n"
        "| Region | H1 |  | H2 |  |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| North | 1 | 2 | 3 | 4 |\n"
        "| South | 5 | 6 | 7 | 8 |\n"
        "|  | 9 | 10 | 11 | 12 |"
    )
    assert "Unnamed" not in result.markdown and "nan" not in result.markdown.lower()
    # Still one section per sheet, whatever the number of tables on it.
    assert [page.title for page in result.pages] == [
        "Plain",
        "Side",
        "Stacked",
        "Titled",
        "Bare",
        "Merged",
    ]
    assert result.pages[1].markdown == f"## Side\n{PARTS_MD}\n\n{CITIES_MD}"
    assert result.metadata["sheet_count"] == 6

    marked = _fixture(name, page_markers=True).markdown
    assert marked.count("<!-- sheet ") == 6
    assert f"<!-- sheet 2 -->\n## Side\n{PARTS_MD}" in marked


@pytest.mark.parametrize("name", ["mf_sheet_layouts.xlsx", "mf_sheet_layouts.xls"])
def test_merged_cells_become_spans_only_when_asked(name: str) -> None:
    spans = (
        '<tr><th>Region</th><th colspan="2">H1</th><th colspan="2">H2</th></tr>\n'
        "</thead>\n<tbody>\n"
        "<tr><td>North</td><td>1</td><td>2</td><td>3</td><td>4</td></tr>\n"
        '<tr><td rowspan="2">South</td><td>5</td><td>6</td><td>7</td><td>8</td></tr>\n'
        "<tr><td>9</td><td>10</td><td>11</td><td>12</td></tr>"
    )
    auto = _fixture(name, table_format="auto").markdown
    assert spans in auto
    assert auto.count("<table>") == 1  # only the block with merged cells
    assert f"## Side\n{PARTS_MD}\n\n{CITIES_MD}" in auto

    html = _fixture(name, table_format="html").markdown
    assert spans in html and html.count("<table>") == 8 and "|" not in html
    assert "<strong>" not in html and "**Stock report**" in html
    assert "span" not in _fixture(name).markdown


@pytest.mark.parametrize(
    "name", ["mf_sheet_layouts.xlsx", "mf_sheet_layouts.xls", "mf_sheets.xlsx"]
)
def test_simple_layout_is_the_single_first_row_table(name: str) -> None:
    simple = _fixture(name, sheet_layout="simple")
    separators = [
        line for line in simple.markdown.splitlines() if line.startswith("| ---")
    ]
    assert len(separators) == len(simple.pages)
    if name.startswith("mf_sheet_layouts"):
        assert (
            "| Part | Qty | Price | Unnamed: 3 | Unnamed: 4 | City |" in simple.markdown
        )
        assert "**" not in simple.markdown
    with pytest.raises(ValueError, match="sheet_layout"):
        _fixture(name, sheet_layout="smart")


@pytest.mark.parametrize(
    "name",
    ["test.xlsx", "test.xls", "mf_sheets.xlsx", "mf_sheets.xls"],
)
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"table_format": "html"},
        {"page_markers": True, "include_hidden_sheets": True},
    ],
)
def test_first_row_tables_are_unchanged_by_layout_detection(
    name: str, options: Any
) -> None:
    auto = _fixture(name, **options)
    simple = _fixture(name, sheet_layout="simple", **options)
    assert auto.markdown == simple.markdown
    assert [p.markdown for p in auto.pages] == [p.markdown for p in simple.pages]
    assert auto.metadata == simple.metadata and auto.warnings == simple.warnings


def _plain_sheets() -> bytes:
    def typed(ws: Any) -> None:
        ws.append(["Id", "Name", "Qty", "Price", "When", "Done", "Note"])
        for i in range(40):
            ws.append(
                [
                    i,
                    f"item_{i}",
                    i * 3 if i % 4 else None,
                    i / 8,
                    datetime.datetime(2024, 1, 1) + datetime.timedelta(days=i),
                    bool(i % 2),
                    "a|b *c*" if i % 3 else None,
                ]
            )

    def repeated_labels(ws: Any) -> None:
        _put(ws, 1, 1, [["Name", "Name", "Total"], ["a", "b", 1], ["c", "d", 2.5]])

    def text_only(ws: Any) -> None:
        _put(
            ws,
            1,
            1,
            [["Name", "Town"], ["Ada", "Oslo"], [None, None], ["Bo", "Bergen"]],
        )

    def gaps(ws: Any) -> None:
        _put(ws, 1, 1, PARTS)
        _put(ws, 6, 1, [["Washer", 3, 0.1], [None, None, None], ["Pin", None, 7]])

    def single_column(ws: Any) -> None:
        _put(ws, 1, 1, [["Other"], ["End"]])

    def numbers(ws: Any) -> None:
        _put(ws, 1, 1, [[2023, 2024], [1.5, 2], [3, None]])

    return _workbook(typed, repeated_labels, text_only, gaps, single_column, numbers)


@pytest.mark.parametrize("options", [{}, {"table_format": "auto"}])
def test_generated_first_row_tables_are_byte_identical(options: Any) -> None:
    data = _plain_sheets()
    auto = _xlsx(data, **options)
    simple = _xlsx(data, sheet_layout="simple", **options)
    assert auto.markdown == simple.markdown
    assert "| Name | Name.1 | Total |" in auto.markdown


def test_title_above_key_values_is_not_a_header() -> None:
    def key_values(ws: Any) -> None:
        _put(ws, 1, 1, [["Report", None], ["Revenue", "$5"], [None, None], ["Cost", 4]])

    data = _workbook(key_values)
    auto = _xlsx(data).markdown
    assert auto.startswith("## S1\n**Report**\n")
    assert "Unnamed" not in auto
    assert "| Report | Unnamed: 1 |" in _xlsx(data, sheet_layout="simple").markdown


def test_plain_table_skips_layout_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _workbook(lambda ws: _put(ws, 1, 1, PARTS[:3]))
    calls: List[int] = []
    real = _xlsx_converter.detect_blocks

    def counting(grid: Any, merges: Any = ()) -> Any:
        calls.append(len(grid))
        return real(grid, merges)

    monkeypatch.setattr(_xlsx_converter, "detect_blocks", counting)
    _xlsx(data)
    assert calls == []
    _xlsx(_workbook(lambda ws: _put(ws, 1, 1, PARTS)))  # has an empty cell...
    assert calls == []  # ... but no empty row or column
    _xlsx(_workbook(lambda ws: _put(ws, 2, 2, PARTS)))
    assert calls == [5]


def test_header_gaps_and_missing_headers_never_print_unnamed() -> None:
    def gap_in_header(ws: Any) -> None:
        _put(
            ws,
            1,
            1,
            [["Part", None, "Price", "Qty"], ["Bolt", "x", 0.5, 1], ["Nut", "y", 2, 2]],
        )

    def offset_numbers(ws: Any) -> None:
        _put(ws, 3, 2, [[1, 2], [3, 4]])
        _put(ws, 7, 2, CITIES)

    def repeated_labels(ws: Any) -> None:
        _put(ws, 1, 1, [["Name", "Val", None, "Name", "Val"], ["a", 1, None, "b", 2]])

    markdown = _xlsx(_workbook(gap_in_header, offset_numbers, repeated_labels)).markdown
    assert markdown == (
        "## S1\n"
        "| Part |  | Price | Qty |\n| --- | --- | --- | --- |\n"
        "| Bolt | x | 0.5 | 1 |\n| Nut | y | 2 | 2 |\n\n"
        "## S2\n"
        "| Column 1 | Column 2 |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n"
        f"{CITIES_MD}\n\n"
        "## S3\n"
        "| Name | Val |\n| --- | --- |\n| a | 1 |\n\n"
        "| Name | Val |\n| --- | --- |\n| b | 2 |"
    )


def test_blocks_keep_blanks_integers_and_iso_dates() -> None:
    def fill(ws: Any) -> None:
        _put(
            ws,
            2,
            2,
            [
                ["Item", "Units", "Share", "Seen", "Stamp"],
                [
                    "Lamp",
                    4,
                    0.25,
                    datetime.datetime(2024, 2, 29),
                    datetime.datetime(2024, 2, 29, 13, 5),
                ],
                ["Desk", None, 3.0, None, datetime.datetime(2024, 3, 1, 8, 0)],
                [None, 9, None, datetime.datetime(2024, 3, 2), None],
            ],
        )

    markdown = _xlsx(_workbook(fill)).markdown
    assert markdown == (
        "## S1\n"
        "| Item | Units | Share | Seen | Stamp |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Lamp | 4 | 0.25 | 2024-02-29 | 2024-02-29 13:05:00 |\n"
        "| Desk |  | 3 | NaT | 2024-03-01 08:00:00 |\n"
        "|  | 9 |  | 2024-03-02 |  |"
    ).replace("NaT", "")


def test_first_cell_title_above_the_only_table(monkeypatch: pytest.MonkeyPatch) -> None:
    def fill(ws: Any) -> None:
        _put(ws, 1, 1, [["Annual report"]])
        _put(ws, 3, 1, PARTS)

    data = _workbook(fill)
    assert _xlsx(data).markdown == f"## S1\n**Annual report**\n\n{PARTS_MD}"
    legacy = _xlsx(data, sheet_layout="simple").markdown
    assert legacy.startswith("## S1\n| Annual report | Unnamed: 1 | Unnamed: 2 |")

    monkeypatch.setattr(_xlsx_converter, "_FIRST_CELL_TITLE_STAYS_HEADER", True)
    assert _xlsx(data).markdown == legacy


def test_title_is_escaped_and_hidden_sheets_stay_hidden() -> None:
    def titled(ws: Any) -> None:
        _put(ws, 2, 2, [["Q1_plan <draft> *v2*"]])
        _put(ws, 4, 2, PARTS)

    def hidden(ws: Any) -> None:
        _put(ws, 2, 2, CITIES)
        ws.sheet_state = "hidden"

    data = _workbook(titled, hidden)

    result = _xlsx(data)
    assert result.markdown == f"## S1\n**Q1\\_plan <draft> \\*v2\\***\n\n{PARTS_MD}"
    assert result.metadata["hidden_sheets"] == ["S2"] and len(result.warnings) == 1
    shown = _xlsx(data, include_hidden_sheets=True)
    assert shown.markdown.endswith(f"## S2\n{CITIES_MD}") and len(shown.pages) == 2


def test_show_zeroes_repair_and_image_hook_still_work_with_several_tables() -> None:
    from openpyxl.drawing.image import Image as SheetImage
    from PIL import Image

    def fill(ws: Any) -> None:
        ws.merge_cells("A1:H1")
        ws["A1"] = "Overview"
        _put(ws, 3, 1, PARTS)
        _put(ws, 3, 6, CITIES)
        picture = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(picture, format="PNG")
        picture.seek(0)
        ws.add_image(SheetImage(picture), "K2")

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_workbook(fill))) as source:
        with zipfile.ZipFile(out, "w") as target:
            for info in source.infolist():
                content = source.read(info)
                if info.filename == "xl/worksheets/sheet1.xml":
                    content = content.replace(
                        b"<sheetView ", b'<sheetView showZeroes="0" ', 1
                    )
                    assert b"showZeroes" in content
                target.writestr(info, content)

    seen: List[Optional[str]] = []

    class Hooked(XlsxConverter):
        def _image_to_html(
            self, image_stream: Any, stream_info: Any, **kwargs: Any
        ) -> str:
            seen.append(stream_info.mimetype)
            return "<p>picture text</p>"

    result = Hooked().convert(io.BytesIO(out.getvalue()), StreamInfo(extension=".xlsx"))
    assert result.markdown == (
        f"## S1\n**Overview**\n\n{PARTS_MD}\n\n{CITIES_MD}\n\n"
        "### Images in this sheet:\n\npicture text"
    )
    assert seen == ["image/png"]


def test_merged_ranges_respect_the_member_limit() -> None:
    from markitforge._exceptions import ResourceLimitException
    from markitforge.converter_utils._archive_limits import ArchiveBudget, ArchiveLimits

    def fill(ws: Any) -> None:
        _put(ws, 2, 2, PARTS)
        ws.merge_cells("B8:D8")

    data = _workbook(fill)
    ranges = _xlsx_converter._read_merged_ranges(
        io.BytesIO(data), ["S1"], ArchiveBudget()
    )
    assert ranges == {"S1": [(7, 1, 7, 3)]}
    tight = ArchiveBudget(ArchiveLimits(max_archive_member_bytes=64))
    with pytest.raises(ResourceLimitException):
        _xlsx_converter._read_merged_ranges(io.BytesIO(data), ["S1"], tight)
    assert (
        _xlsx_converter._read_merged_ranges(io.BytesIO(b"not a zip"), ["S1"], tight)
        == {}
    )


def test_many_empty_rows_stay_cheap() -> None:
    # Thousands of row runs: no quadratic work, and still one table.
    rows: List[List[Any]] = [["Name", "Qty"]]
    for i in range(6000):
        rows.append([f"n{i}", i] if i % 2 else [None, None])
    blocks = detect_blocks(rows)
    assert _bounds(blocks) == [(0, 0, 6000, 1, "table", 0)]
    assert len(rows) // 2 > _sheet_layout._MAX_ROW_RUNS


# ------------------------------------------------------------ DOCX page count


def _long_docx(paragraphs: int) -> bytes:
    from docx import Document

    document = Document()
    for index in range(paragraphs):
        document.add_paragraph(
            f"Paragraph {index}. " + "Lorem ipsum dolor sit amet. " * 12
        )
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _docx(data: bytes, **kwargs: Any) -> Any:
    return DocxConverter().convert(
        io.BytesIO(data), StreamInfo(extension=".docx"), **kwargs
    )


def test_docx_page_count_is_off_by_default_and_validated() -> None:
    data = (FILES / "mf_pages.docx").read_bytes()
    result = _docx(data)
    assert "page_count" not in result.metadata
    assert "page_count_source" not in result.metadata
    with pytest.raises(ValueError, match="docx_page_count"):
        _docx(data, docx_page_count="word")


def test_docx_page_count_without_libreoffice_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_libreoffice_converter, "find_soffice", lambda: None)
    data = (FILES / "mf_pages.docx").read_bytes()
    result = _docx(data, docx_page_count="libreoffice")
    assert result.metadata["page_count"] == len(result.pages) == 3
    assert result.metadata["page_count_source"] == "stored_breaks"
    assert len(result.warnings) == 1 and "LibreOffice" in result.warnings[0]
    assert result.markdown == _docx(data).markdown


@needs_soffice
def test_docx_page_count_from_libreoffice() -> None:
    data = (FILES / "mf_pages.docx").read_bytes()
    result = _docx(data, docx_page_count="libreoffice")
    assert result.metadata["page_count"] == 3
    assert result.metadata["page_count_source"] == "libreoffice"
    assert len(result.pages) == 3 and result.warnings == []
    assert result.markdown == _docx(data).markdown

    via_api = MarkitForge().convert(
        str(FILES / "mf_pages.docx"), docx_page_count="libreoffice"
    )
    assert via_api.metadata["page_count"] == 3


@needs_soffice
def test_docx_page_count_warns_when_stored_breaks_disagree() -> None:
    # python-docx stores no rendered page breaks, so the file claims one page.
    data = _long_docx(60)
    plain = _docx(data)
    assert len(plain.pages) == 1 and plain.warnings == []

    result = _docx(data, docx_page_count="libreoffice")
    assert result.metadata["page_count"] > 1
    assert result.metadata["page_count_source"] == "libreoffice"
    assert len(result.pages) == 1
    assert len(result.warnings) == 1
    assert f"{result.metadata['page_count']} page(s)" in result.warnings[0]


def test_count_pdf_pages(tmp_path: Path) -> None:
    from PIL import Image

    pdf = tmp_path / "three.pdf"
    pages = [Image.new("RGB", (40, 40), color) for color in ("red", "green", "blue")]
    pages[0].save(pdf, format="PDF", save_all=True, append_images=pages[1:])
    assert page_breaks.count_pdf_pages(pdf) == 3
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 nothing here")
    assert page_breaks.count_pdf_pages(broken) is None
    assert page_breaks.count_pdf_pages(tmp_path / "missing.pdf") is None
