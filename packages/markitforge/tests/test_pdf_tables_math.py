# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PDF tables (borderless, merged cells, continued over pages) and math
detection without an LLM.

Fixtures (tests/test_files/mf_tbl_*.pdf, mf_math_*.pdf) are built with
reportlab. They are regenerated when missing; run this file directly to
rebuild them all:

    python tests/test_pdf_tables_math.py --rebuild
"""

import os
import sys
from typing import Any, Dict, List, Optional

import pytest

from markitforge import MarkitForge
from markitforge._base_converter import DocumentSection
from markitforge.converter_utils._text_safety import safe_latex
from markitforge.converter_utils.pdf import _math
from markitforge.converter_utils.pdf._layout import TextLine, extract_lines
from markitforge.converter_utils.pdf._math import (
    apply_inline_math,
    display_math_flags,
    inline_math_text,
    is_display_math,
    math_lines,
)
from markitforge.converter_utils.pdf._tables import (
    PageTable,
    _looks_like_text_table,
    find_page_tables,
    find_text_tables,
    merge_continued_tables,
    normalize_rows,
    parse_markdown_table,
    point_in_bboxes,
    render_html_table,
    render_markdown_table,
    render_table,
    split_markdown_row,
    valid_spans,
)
from markitforge.converter_utils.pdf._visual import find_math_regions

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), "test_files")

BORDERLESS = "mf_tbl_borderless.pdf"
WRAPPED = "mf_tbl_wrapped.pdf"
NEGATIVES = "mf_tbl_negatives.pdf"
COLSPAN = "mf_tbl_colspan.pdf"
MATH_INLINE = "mf_math_inline.pdf"

PROSE = (
    "The committee reviewed the proposal in detail and agreed that the "
    "schedule was realistic, although several members asked for a clearer "
    "account of the assumptions behind the cost estimate and the staffing "
    "plan for the second phase of the work. "
)
BORDERLESS_ROWS = [
    ["Product", "Warehouse", "Units", "Revenue"],
    ["Copper fittings", "Rotterdam", "1,250", "18,430.00"],
    ["Steel brackets", "", "87", "2,114.50"],
    ["Rubber seals", "Antwerp", "12,400", "9,920.00"],
    ["Valve kits", "Hamburg", "310", "27,590.75"],
    ["Total", "", "14,047", "58,055.25"],
]
WRAPPED_ROWS = [
    ["Code", "Description", "Hours"],
    ["M-101", "Replace the intake filter", "1.5"],
    ["M-102", "Inspect conveyor belt tension and realign the drive rollers", "12.0"],
    ["M-103", "Lubricate bearings", "0.5"],
    ["M-104", "Calibrate the pressure sensors on both hydraulic lines", "3.25"],
]


# -- fixture builders -------------------------------------------------------


def _build_borderless(path: str) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, "Shipment Summary")
    c.setFont("Helvetica", 10)
    c.drawString(
        72, 700, "The figures below cover all shipments booked in the first quarter."
    )
    y = 670
    for i, (product, warehouse, units, revenue) in enumerate(BORDERLESS_ROWS):
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 10)
        c.drawString(72, y, product)
        c.drawString(200, y, warehouse)
        c.drawRightString(360, y, units)
        c.drawRightString(460, y, revenue)
        y -= 16
    c.setFont("Helvetica", 10)
    c.drawString(
        72, y - 14, "Revenue is reported in euros and excludes freight charges."
    )
    c.showPage()
    c.save()


def _build_wrapped(path: str) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "Open maintenance tasks are listed here.")
    rows = [
        [("Code", "Description", "Hours")],
        [("M-101", "Replace the intake filter", "1.5")],
        [
            ("M-102", "Inspect conveyor belt tension and", "12.0"),
            ("", "realign the drive rollers", ""),
        ],
        [("M-103", "Lubricate bearings", "0.5")],
        [
            ("M-104", "Calibrate the pressure sensors on", "3.25"),
            ("", "both hydraulic lines", ""),
        ],
    ]
    y = 690
    for group in rows:
        for code, description, hours in group:
            c.drawString(72, y, code)
            c.drawString(140, y, description)
            c.drawRightString(380, y, hours)
            y -= 12
        y -= 6
    c.drawString(72, y - 16, "Hours are estimates and may change after inspection.")
    c.showPage()
    c.save()


def _build_negatives(path: str) -> None:
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Frame, Paragraph

    c = canvas.Canvas(path, pagesize=letter, invariant=1)
    # page 1: justified prose in two columns
    style = ParagraphStyle(
        "justified",
        fontName="Times-Roman",
        fontSize=10,
        leading=12,
        alignment=TA_JUSTIFY,
    )
    for x in (72, 316):
        frame = Frame(
            x,
            380,
            224,
            340,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        frame.addFromList([Paragraph(PROSE * 3, style), Paragraph(PROSE * 2, style)], c)
    c.showPage()
    # page 2: two short address blocks side by side
    c.setFont("Helvetica", 10)
    left = [
        "Northwind Traders Ltd",
        "14 Harbour Road",
        "Bristol BS1 4QA",
        "United Kingdom",
    ]
    right = [
        "Fabrikam Supplies",
        "2200 Mission Street",
        "Springfield, IL 62704",
        "United States",
    ]
    y = 720
    for a, b in zip(left, right):
        c.drawString(72, y, a)
        c.drawString(330, y, b)
        y -= 13
    c.drawString(72, y - 20, "Dear customer, thank you for your recent order.")
    c.showPage()
    # page 3: table of contents with dot leaders
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, "Contents")
    c.setFont("Helvetica", 10)
    entries = [
        ("1 Introduction", "3"),
        ("2 Background and related work", "7"),
        ("3 Method", "12"),
        ("4 Results", "21"),
        ("5 Discussion", "34"),
        ("References", "41"),
    ]
    y = 694
    for title, page in entries:
        c.drawString(72, y, title)
        start = 72 + c.stringWidth(title, "Helvetica", 10) + 6
        end = 500 - c.stringWidth(page, "Helvetica", 10) - 6
        dots = int((end - start) / c.stringWidth(". ", "Helvetica", 10))
        c.drawString(start, y, ". " * dots)
        c.drawRightString(500, y, page)
        y -= 15
    c.showPage()
    c.save()


def _build_colspan(path: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    data = [
        ["Region", "2024", "", "Notes"],
        ["", "H1", "H2", ""],
        ["North", "120", "135", "a < b & c"],
        ["South", "95", "101", "stable"],
    ]
    table = Table(data, colWidths=[90, 60, 60, 110])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("SPAN", (1, 0), (2, 0)),
                ("SPAN", (0, 0), (0, 1)),
                ("SPAN", (3, 0), (3, 1)),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
            ]
        )
    )
    SimpleDocTemplate(path, pagesize=letter, invariant=1).build([table])


def _build_math_inline(path: str) -> None:
    # Greek letters and operators use the standard Symbol font, which
    # pdfminer reports as Latin-1 characters ("a" for alpha).
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=16)
    centred = ParagraphStyle("centred", parent=body, alignment=1)
    sym = '<font name="Symbol">%s</font>'
    story = [
        Paragraph(
            "Mass and energy are related by E = mc<super>2</super> in special "
            "relativity.",
            body,
        ),
        Paragraph(
            "Each term satisfies x<sub>i</sub> + y<sub>i</sub> = z<sub>i</sub> "
            "for every index.",
            body,
        ),
        Paragraph(
            "The test rejects when %s holds for the sample." % (sym % "α ≤ β"), body
        ),
        Paragraph(
            "A circle of radius r has area %sr<super>2</super>, as is well known."
            % (sym % "π"),
            body,
        ),
        Paragraph(
            "This was first reported by Okafor<super>3</super> and later confirmed.",
            body,
        ),
        Paragraph("The meeting is planned for the 21<super>st</super> of March.", body),
        Spacer(1, 10),
        Paragraph(
            "%s<super>2</super> = %s<sub>k</sub> (x<sub>k</sub> %s %s)<super>2</super>"
            % (sym % "σ", sym % "Σ", sym % "−", sym % "μ"),
            centred,
        ),
        Spacer(1, 10),
        Paragraph("Ordinary closing sentence without any mathematics at all.", body),
    ]
    SimpleDocTemplate(path, pagesize=letter, invariant=1).build(story)


BUILDERS = {
    BORDERLESS: _build_borderless,
    WRAPPED: _build_wrapped,
    NEGATIVES: _build_negatives,
    COLSPAN: _build_colspan,
    MATH_INLINE: _build_math_inline,
}


def _fixture(name: str) -> str:
    path = os.path.join(TEST_FILES_DIR, name)
    if not os.path.exists(path):
        pytest.importorskip("reportlab")
        BUILDERS[name](path)
    return path


def _page_tables(name: str, page_no: int = 1) -> List[PageTable]:
    import pdfplumber

    with pdfplumber.open(_fixture(name)) as pdf:
        return find_page_tables(pdf.pages[page_no - 1])


class _WordPage:
    """A stand-in for a pdfplumber page that only has words."""

    def __init__(self, words: List[Dict[str, Any]]):
        self._words = words

    def extract_words(self) -> List[Dict[str, Any]]:
        return self._words

    def find_tables(self, settings: Any) -> List[Any]:
        return []


def _word(text: str, x0: float, top: float, *, x1: Optional[float] = None, size=10.0):
    right = x0 + 5.0 * len(text) if x1 is None else x1
    return {"text": text, "x0": x0, "x1": right, "top": top, "bottom": top + size}


def _grid_words(rows, lefts, *, align="left", pitch=14.0, top=100.0):
    words = []
    for r, row in enumerate(rows):
        for text, x in zip(row, lefts):
            if not text:
                continue
            width = 5.0 * len(text)
            x0 = {"left": x, "right": x - width, "centre": x - width / 2}[align]
            words.append(_word(text, x0, top + r * pitch, x1=x0 + width))
    return words


# -- borderless tables --------------------------------------------------------


class TestBorderlessTables:
    def test_right_aligned_numbers_and_empty_cells(self):
        tables = _page_tables(BORDERLESS)
        assert [t.kind for t in tables] == ["text"]
        assert tables[0].rows == BORDERLESS_ROWS
        assert tables[0].spans == {}

    def test_table_bbox_excludes_surrounding_text(self):
        import pdfplumber

        with pdfplumber.open(_fixture(BORDERLESS)) as pdf:
            page = pdf.pages[0]
            (table,) = find_page_tables(page)
            outside = [
                w["text"]
                for w in page.extract_words()
                if not point_in_bboxes(w, [table.bbox])
            ]
        text = " ".join(outside)
        assert text.startswith("Shipment Summary The figures below")
        assert text.endswith("excludes freight charges.")
        assert "Rotterdam" not in text and "58,055.25" not in text

    def test_wrapped_cell_joins_the_row_above(self):
        (table,) = _page_tables(WRAPPED)
        assert table.kind == "text"
        assert table.rows == WRAPPED_ROWS

    @pytest.mark.parametrize(
        "page_no, what",
        [
            (1, "justified prose in two columns"),
            (2, "address blocks side by side"),
            # Decision: a table of contents with dot leaders is not a table.
            (3, "table of contents with dot leaders"),
        ],
    )
    def test_not_a_table(self, page_no, what):
        assert _page_tables(NEGATIVES, page_no) == [], what

    def test_centred_and_right_aligned_columns(self):
        rows = [
            ["Name", "Qty", "Price"],
            ["Bolt", "2", "0.40"],
            ["Washer", "150", "12.25"],
            ["Nut", "31", "3.10"],
        ]
        for align in ("left", "right", "centre"):
            page = _WordPage(_grid_words(rows, [120, 250, 380], align=align))
            (table,) = find_text_tables(page)
            assert table.rows == rows, align

    def test_exclude_skips_words_in_ruled_tables(self):
        rows = [["Name", "Qty", "Price"], ["Bolt", "2", "0.40"], ["Nut", "31", "3.10"]]
        page = _WordPage(_grid_words(rows, [100, 220, 340]))
        assert len(find_text_tables(page)) == 1
        assert find_text_tables(page, exclude=[(90, 90, 400, 150)]) == []

    def test_rows_far_apart_are_not_one_table(self):
        rows = [["Name", "Qty", "Price"], ["Bolt", "2", "0.40"], ["Nut", "31", "3.10"]]
        page = _WordPage(_grid_words(rows, [100, 220, 340], pitch=40.0))
        assert find_text_tables(page) == []

    def test_unaligned_columns_are_rejected(self):
        # Three "cells" per line whose edges drift from line to line, as
        # the gaps of justified prose do.
        words = []
        for r, shift in enumerate([0, 9, 21, 4, 15, 27]):
            for k, text in enumerate(["12", "345", "6"]):
                words.append(
                    _word(text, 100 + 120 * k + shift + 7 * k * r, 100 + 14 * r)
                )
        assert find_text_tables(_WordPage(words)) == []

    def test_group_label_row_is_kept_as_its_own_row(self):
        rows = [
            ["Item", "Qty", "Cost"],
            ["Bolt", "2", "0.40"],
            ["Fasteners", "", ""],
            ["Nut", "31", "3.10"],
            ["Washer", "7", "0.70"],
        ]
        (table,) = find_text_tables(_WordPage(_grid_words(rows, [100, 220, 340])))
        assert table.rows == rows

    def test_wrapped_first_column_at_row_distance(self):
        # Evenly spaced lines: the wrapped line continues the cell above
        # because that cell filled its column. The short line under the
        # table does not, and stays outside it.
        lefts = [100, 260, 340]
        words = _grid_words(
            [
                ["Item", "Qty", "Cost"],
                ["Hexagonal bolt, zinc plated,", "2", "0.40"],
                ["M8", "", ""],
                ["Nut", "31", "3.10"],
                ["Washer", "7", "0.70"],
                ["Notes", "", ""],
            ],
            lefts,
        )
        (table,) = find_text_tables(_WordPage(words))
        assert table.rows == [
            ["Item", "Qty", "Cost"],
            ["Hexagonal bolt, zinc plated, M8", "2", "0.40"],
            ["Nut", "31", "3.10"],
            ["Washer", "7", "0.70"],
        ]
        assert not point_in_bboxes(words[-1], [table.bbox])

    def test_text_checks(self):
        pairs = [["Name:", "Jane Roe"], ["Date:", "12 March"], ["Terms:", "30 days"]]
        assert not _looks_like_text_table(pairs)
        numbered = [["1.", "Open the valve"], ["2.", "Drain"], ["3.", "Close it"]]
        assert not _looks_like_text_table(numbered)
        amounts = [["Item", "Amount"], ["Rent", "1,200.00"], ["Power", "180.50"]]
        assert _looks_like_text_table(amounts)
        people = [
            ["Jane Roe", "John Doe", "Ada Poe"],
            ["Physics", "Chemistry", "Biology"],
            ["North", "South", "East"],
            ["jane@n.edu", "john@s.edu", "ada@e.edu"],
        ]
        assert not _looks_like_text_table(people)
        prose = [["word " * 9, "word " * 9, "7"]] * 4
        assert not _looks_like_text_table(normalize_rows(prose))

    def test_ruled_tables_still_come_first(self):
        import pdfplumber

        path = os.path.join(TEST_FILES_DIR, "SPARSE-2024-INV-1234_borderless_table.pdf")
        with pdfplumber.open(path) as pdf:
            tables = find_page_tables(pdf.pages[1])
        assert [(t.kind, len(t.rows), len(t.rows[0])) for t in tables] == [
            ("ruled", 11, 6)
        ]


# -- merged cells ---------------------------------------------------------------


COLSPAN_HTML = """<table>
<thead>
<tr><th rowspan="2">Region</th><th colspan="2">2024</th><th rowspan="2">Notes</th></tr>
<tr><th>H1</th><th>H2</th></tr>
</thead>
<tbody>
<tr><td>North</td><td>120</td><td>135</td><td>a &lt; b &amp; c</td></tr>
<tr><td>South</td><td>95</td><td>101</td><td>stable</td></tr>
</tbody>
</table>"""


class TestMergedCells:
    def test_spans_of_a_ruled_table(self):
        (table,) = _page_tables(COLSPAN)
        assert table.kind == "ruled"
        assert table.rows == [
            ["Region", "2024", "", "Notes"],
            ["", "H1", "H2", ""],
            ["North", "120", "135", "a < b & c"],
            ["South", "95", "101", "stable"],
        ]
        assert table.spans == {(0, 0): (2, 1), (0, 1): (1, 2), (0, 3): (2, 1)}

    def test_markdown_keeps_text_in_the_first_cell(self):
        (table,) = _page_tables(COLSPAN)
        md = render_table(table)
        assert (
            md == render_table(table, "markdown") == render_markdown_table(table.rows)
        )
        assert md.splitlines()[:3] == [
            "| Region | 2024 |     | Notes     |",
            "| ------ | ---- | --- | --------- |",
            "|        | H1   | H2  |           |",
        ]

    def test_html_has_colspan_and_rowspan(self):
        (table,) = _page_tables(COLSPAN)
        assert render_table(table, "html") == COLSPAN_HTML
        assert render_table(table, "auto") == COLSPAN_HTML
        assert render_table(table, "HTML") == COLSPAN_HTML

    def test_auto_and_unknown_formats_fall_back_to_markdown(self):
        table = PageTable(bbox=(0, 0, 1, 1), rows=[["a", "b"], ["1", "2"]])
        md = render_markdown_table(table.rows)
        assert render_table(table, "auto") == md
        assert render_table(table, "latex") == md
        assert render_table(table, None) == md  # type: ignore[arg-type]
        assert render_table(table, "html").startswith("<table>")

    def test_html_escapes_cell_text(self):
        rows = normalize_rows(
            [["<script>alert(1)</script>", 'a "b" | c'], ["<!-- page 2 -->", "x\n\ny"]]
        )
        out = render_html_table(rows)
        assert "<script>" not in out and "<!--" not in out
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
        assert "a &quot;b&quot; | c" in out
        assert "&lt;!-- page 2 --&gt;" in out
        assert "" not in out.split("\n")  # no blank line: stays one HTML block

    def test_spans_are_validated(self):
        rows = [["a", "", "c"], ["d", "e", "f"], ["g", "h", "i"]]
        bad: Dict[Any, Any] = {
            (0, 0): (1, 2),  # fine
            (1, 0): ("2", 1),  # not an int
            (1, 1): (True, 2),  # bool is not a span
            (2, 0): (1, 0),  # below 1
            (5, 0): (1, 2),  # outside the grid
            (0, 1): (2, 1),  # overlaps the first span
            (1, 2): (9, 9),  # would hide "i" once clipped
            (2, 1): (-1, 2),
        }
        assert valid_spans(rows, bad) == {(0, 0): (1, 2)}
        assert valid_spans(rows, {(0, 1): (1, 2)}) == {}  # would hide "c"
        assert valid_spans([["a", ""], ["", ""]], {(0, 0): (7, 7)}) == {(0, 0): (2, 2)}
        assert valid_spans(rows, None) == {}
        out = render_html_table(rows, bad)
        assert out.count("colspan") == 1 and "rowspan" not in out
        assert '<th colspan="2">a</th><th>c</th>' in out

    def test_spans_follow_dropped_rows_and_columns(self):
        from markitforge.converter_utils.pdf._tables import _normalize

        raw = [["Head", None, None, ""], ["", "", "", ""], ["a", "", "b", ""]]
        rows, spans = _normalize(raw, {(0, 0): (2, 3)})
        assert rows == [["Head", ""], ["a", "b"]]
        assert spans == {(0, 0): (1, 2)}


# -- tables continued over pages -------------------------------------------------


def _sections(*pages: str) -> List[DocumentSection]:
    return [DocumentSection(index=i, markdown=md) for i, md in enumerate(pages, 1)]


HEADER = ["Item", "Qty", "Price"]
PAGE1 = "Intro text.\n\n" + render_markdown_table([HEADER, ["Bolt", "2", "0.40"]])


class TestMergeContinuedTables:
    def test_repeated_header_is_dropped(self):
        page2 = render_markdown_table([HEADER, ["Nut", "31", "3.10"]]) + "\n\nClosing."
        out = merge_continued_tables(_sections(PAGE1, page2))
        assert out[0].markdown == "Intro text.\n\n" + render_markdown_table(
            [HEADER, ["Bolt", "2", "0.40"], ["Nut", "31", "3.10"]]
        )
        assert out[1].markdown == "Closing."
        assert [s.index for s in out] == [1, 2]

    def test_continuation_without_header(self):
        page2 = render_markdown_table([["Nut", "31", "3.10"], ["Washer", "7", "0.70"]])
        out = merge_continued_tables(_sections(PAGE1, "\n\n" + page2 + "\n"))
        rows = parse_markdown_table(out[0].markdown.split("\n\n")[1])
        assert rows == [
            HEADER,
            ["Bolt", "2", "0.40"],
            ["Nut", "31", "3.10"],
            ["Washer", "7", "0.70"],
        ]
        assert out[1].markdown == ""

    def test_new_table_with_another_header_is_not_merged(self):
        page2 = render_markdown_table([["Name", "Unit", "Rate"], ["Nut", "31", "3.10"]])
        sections = _sections(PAGE1, page2)
        out = merge_continued_tables(sections)
        assert [s.markdown for s in out] == [PAGE1, page2]

    def test_different_column_count_is_not_merged(self):
        page2 = render_markdown_table([["Item", "Qty"], ["Nut", "31"]])
        out = merge_continued_tables(_sections(PAGE1, page2))
        assert [s.markdown for s in out] == [PAGE1, page2]

    def test_text_between_the_tables_blocks_the_merge(self):
        table2 = render_markdown_table([HEADER, ["Nut", "31", "3.10"]])
        for page1, page2 in (
            (PAGE1 + "\n\nSee the next page.", table2),
            (PAGE1, "Continued from page 1.\n\n" + table2),
        ):
            out = merge_continued_tables(_sections(page1, page2))
            assert [s.markdown for s in out] == [page1, page2]

    def test_table_running_over_three_pages(self):
        page2 = render_markdown_table([HEADER, ["Nut", "31", "3.10"]])
        page3 = render_markdown_table([HEADER, ["Washer", "7", "0.70"]]) + "\n\nEnd."
        out = merge_continued_tables(_sections(PAGE1, page2, page3))
        assert parse_markdown_table(out[0].markdown.split("\n\n")[1]) == [
            HEADER,
            ["Bolt", "2", "0.40"],
            ["Nut", "31", "3.10"],
            ["Washer", "7", "0.70"],
        ]
        assert [s.markdown for s in out[1:]] == ["", "End."]

    def test_second_table_on_a_page_starts_a_new_chain(self):
        other = [["Name", "City"], ["Roe", "Oslo"]]
        page2 = (
            render_markdown_table([HEADER, ["Nut", "31", "3.10"]])
            + "\n\nStaff list.\n\n"
            + render_markdown_table(other)
        )
        page3 = render_markdown_table([["Name", "City"], ["Doe", "Bergen"]])
        out = merge_continued_tables(_sections(PAGE1, page2, page3))
        assert out[1].markdown == "Staff list.\n\n" + render_markdown_table(
            other + [["Doe", "Bergen"]]
        )
        assert out[2].markdown == ""

    def test_input_is_not_modified_and_fields_survive(self):
        sections = [
            DocumentSection(index=4, markdown=PAGE1, title="A", kind="page"),
            DocumentSection(
                index=5,
                markdown=render_markdown_table([HEADER, ["Nut", "31", "3.10"]]),
                title="B",
                kind="page",
            ),
        ]
        before = [s.markdown for s in sections]
        out = merge_continued_tables(sections)
        assert [s.markdown for s in sections] == before
        assert [(s.index, s.title, s.kind) for s in out] == [
            (4, "A", "page"),
            (5, "B", "page"),
        ]
        assert merge_continued_tables([]) == []
        assert merge_continued_tables(sections[:1])[0].markdown == PAGE1

    def test_escaped_pipes_stay_in_their_cell(self):
        page1 = render_markdown_table(normalize_rows([["Key", "Value"], ["a|b", "1"]]))
        page2 = render_markdown_table(normalize_rows([["Key", "Value"], ["c|d", "2"]]))
        out = merge_continued_tables(_sections(page1, page2))
        assert out[0].markdown.splitlines() == [
            "| Key  | Value |",
            "| ---- | ----- |",
            "| a\\|b | 1     |",
            "| c\\|d | 2     |",
        ]

    def test_markdown_table_helpers(self):
        assert split_markdown_row("| a | b\\|c |  |") == ["a", "b\\|c", ""]
        assert split_markdown_row("not a row") is None
        assert parse_markdown_table("| a | b |\n| :-- | --: |\n| 1 | 2 |") == [
            ["a", "b"],
            ["1", "2"],
        ]
        assert parse_markdown_table("| a | b |\n| 1 | 2 |") is None  # no separator
        assert parse_markdown_table("| a | b |\n| - | - |\n| 1 |") is None  # ragged


# -- math ----------------------------------------------------------------------------


def _chars(text: str, *, x=100.0, top=100.0, size=10.0, font="Helvetica", scripts=()):
    """pdfplumber-style chars for ``text``. ``scripts`` maps a character
    index to +1 (superscript) or -1 (subscript)."""
    shifts = dict(scripts)
    out = []
    for i, ch in enumerate(text):
        shift = shifts.get(i, 0)
        s = size * 0.7 if shift else size
        bottom = (
            top
            + size
            - (0.45 * size if shift > 0 else 0)
            + (0.3 * size if shift < 0 else 0)
        )
        out.append(
            {
                "text": ch,
                "x0": x,
                "x1": x + 0.5 * s,
                "top": bottom - s,
                "bottom": bottom,
                "size": s,
                "fontname": font,
            }
        )
        x += 0.5 * s
    return out


class TestInlineMath:
    def test_fixture_lines(self):
        import pdfplumber

        with pdfplumber.open(_fixture(MATH_INLINE)) as pdf:
            page = pdf.pages[0]
            lines, count = apply_inline_math(extract_lines(page), page.chars)
        text = "\n".join(line.text for line in lines)
        assert count == 5
        for expected in (
            "Mass and energy are related by $E = mc^{2}$ in special relativity.",
            "Each term satisfies $x_{i} + y_{i} = z_{i}$ for every index.",
            "The test rejects when $\\alpha \\leq \\beta$ holds for the sample.",
            "A circle of radius r has area $\\pi r^{2}$, as is well known.",
            "$\\sigma^{2} = \\Sigma_{k} (x_{k} - \\mu)^{2}$",
            "Ordinary closing sentence without any mathematics at all.",
        ):
            assert expected in text.split("\n"), expected
        # a footnote mark and an ordinal are not exponents
        assert "Okafor$" not in text and "21$" not in text
        assert text.count("$") == 10

    def test_formulas_pass_latex_validation(self):
        import pdfplumber

        with pdfplumber.open(_fixture(MATH_INLINE)) as pdf:
            page = pdf.pages[0]
            lines, _count = apply_inline_math(extract_lines(page), page.chars)
        formulas = [part for line in lines for part in line.text.split("$")[1::2]]
        assert len(formulas) == 5
        for latex in formulas:
            assert safe_latex(latex) == latex

    def test_form_pages_are_left_alone(self):
        import pdfplumber

        with pdfplumber.open(_fixture(MATH_INLINE)) as pdf:
            page = pdf.pages[0]
            lines = extract_lines(page)
            same, count = apply_inline_math(lines, page.chars, form_page=True)
        assert count == 0
        assert [l.text for l in same] == [l.text for l in lines]

    def test_default_conversion_has_no_inline_math(self):
        md = MarkitForge().convert(_fixture(MATH_INLINE)).markdown
        assert "$" not in md

    def test_baseline_shift(self):
        assert inline_math_text(_chars("so E = mc2 holds", scripts={9: 1})) == (
            "so $E = mc^{2}$ holds"
        )
        assert inline_math_text(
            _chars("then an+1 = 2an", scripts={6: -1, 7: -1, 8: -1, 14: -1})
        ) == ("then $a_{n+1} = 2a_{n}$")
        # the same characters on the baseline are plain text
        assert inline_math_text(_chars("so E = mc2 holds")) is None

    def test_unicode_ranges(self):
        assert inline_math_text(_chars("hence x² + y² ≥ 2xy.")) == (
            "hence $x^{2} + y^{2} \\geq 2xy$."
        )
        assert (
            inline_math_text(_chars("with 𝑥 ∈ ℝ given"))
            == "with $x \\in \\mathbb{R}$ given"
        )
        assert inline_math_text(_chars("angle θ and rate λ₀")) == (
            "angle $\\theta$ and rate $\\lambda_{0}$"
        )
        assert inline_math_text(_chars("if a < b then (x₁ + x₂) > 0")) == (
            "if a < b then $(x_{1} + x_{2}) \\gt 0$"
        )

    def test_symbol_font_is_decoded(self):
        chars = _chars("when ") + _chars("a \xa3 b", x=125.0, font="Symbol")
        assert inline_math_text(chars) == "when $\\alpha \\leq \\beta$"

    def test_math_font_variables(self, monkeypatch):
        chars = (
            _chars("where ")
            + _chars("x", x=130.0, font="ABCDEF+CMMI10")
            + _chars(" is real", x=135.0)
        )
        assert inline_math_text(chars) == "where $x$ is real"

    @pytest.mark.parametrize(
        "text, scripts",
        [
            ("plain words only, nothing else", ()),
            ("reported by Okafor3 and others", ((18, 1),)),
            ("on the 21st of March", ((9, 1), (10, 1))),
            ("see the note.4 Then", ((13, 1),)),
            ("a film 5 μm thick at 10 kΩ", ()),
            ("total 3 × 4 m, about 5 ± 1 units", ()),
            ("Η μέθοδος αυτή είναι απλή και γρήγορη", ()),
            ("costs $5 for x² items", ()),
            ("path C:\\x² and {y²}", ()),
            ("temperature −5 °C to +3 °C", ()),
        ],
    )
    def test_left_alone(self, text, scripts):
        assert inline_math_text(_chars(text, scripts=scripts)) is None

    def test_unsafe_runs_are_not_wrapped(self, monkeypatch):
        chars = _chars("hence x² + y² ≥ 2xy.")
        monkeypatch.setattr(_math, "safe_latex", lambda latex: None)
        assert inline_math_text(chars) is None

    def test_scripts_stay_on_their_line(self):
        chars = _chars("x2 + y2", scripts={1: -1, 6: -1}) + _chars(
            "next line", top=113.0
        )
        assert [line.text for line in math_lines(chars)] == ["x2 + y2", "next line"]


class TestDisplayMath:
    @pytest.mark.parametrize(
        "text, extra, expected",
        [
            ("σ² = Σₖ (xₖ − μ)²", 0, True),
            ("∑ aᵢ ≤ ∞", 0, True),
            ("E = mc2", 1, True),  # the exponent is a raised character
            ("E = mc2", 0, False),
            ("Total = 5", 0, False),
            ("p < 0.05", 0, False),
            ("The parameter α controls the learning rate of the model.", 0, False),
            ("Η μέθοδος αυτή είναι απλή", 0, False),
            ("H₂O", 0, False),
            ("• first item", 0, False),
        ],
    )
    def test_is_display_math(self, text, extra, expected):
        assert is_display_math(text, extra) is expected

    def test_regions_with_page_chars(self):
        import pdfplumber

        with pdfplumber.open(_fixture(MATH_INLINE)) as pdf:
            page = pdf.pages[0]
            lines = extract_lines(page)
            text_only = find_math_regions(lines)
            regions = find_math_regions(lines, page.chars)
        # The formula's Greek letters are Symbol-font "s", "S" and "m" and
        # its scripts are only visible as baseline shifts.
        assert text_only == []
        assert len(regions) == 1
        covered = [l.text for l in lines[regions[0].start : regions[0].end]]
        assert any("=" in text for text in covered)
        assert "k k" in covered  # the subscripts line extraction split off
        assert not any("sentence" in text or "March" in text for text in covered)
        x0, top, x1, bottom = regions[0].bbox
        assert 250 < x0 < x1 < 360 and bottom - top < 30

    def test_unicode_formula_without_chars(self):
        lines = [
            TextLine("The variance is defined as", 72, 100, 300, 110, 10, False),
            TextLine("σ² = Σₖ (xₖ − μ)²", 150, 120, 260, 130, 10, False),
            TextLine("for all samples.", 72, 140, 200, 150, 10, False),
        ]
        assert display_math_flags(lines) == [False, True, False]
        (region,) = find_math_regions(lines)
        assert (region.start, region.end) == (1, 2)

    def test_math_font_rule_still_applies(self):
        line = TextLine(
            "E = mc2", 150, 100, 200, 112, 12, False, math_chars=5, total_chars=5
        )
        prose = TextLine("Here c is the speed of light.", 72, 130, 250, 140, 10, False)
        (region,) = find_math_regions([line, prose])
        assert (region.start, region.end) == (0, 1)


if __name__ == "__main__":
    if "--rebuild" in sys.argv:
        for fixture_name, builder in BUILDERS.items():
            builder(os.path.join(TEST_FILES_DIR, fixture_name))
            print("built", fixture_name)
