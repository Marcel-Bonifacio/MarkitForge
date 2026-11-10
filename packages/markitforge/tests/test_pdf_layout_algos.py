# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PDF layout algorithms: reading order, headings, running headers and
footers, paragraph reflow and scan detection.

Fixtures (tests/test_files/mf_layout_*.pdf) are built with reportlab. They
are regenerated when missing; run this file directly to rebuild them all:

    python tests/test_pdf_layout_algos.py --rebuild
"""

import io
import os
import re
import sys
from collections import Counter

import pytest

from markitforge import MarkitForge
from markitforge.converters._pdf_converter import (
    _garbled_warnings,
    _is_garbled,
    _sparse_warnings,
)
from markitforge.converter_utils.pdf._furniture import (
    PageLines,
    find_furniture,
    furniture_key,
    strip_furniture,
    strip_furniture_text,
)
from markitforge.converter_utils.pdf._layout import (
    Block,
    OutlineEntry,
    SizeClusters,
    TextLine,
    assign_headings,
    has_numbering,
    heading_tag_level,
    score_heading,
)
from markitforge.converter_utils.pdf._metadata import read_outline
from markitforge.converter_utils.pdf._reading_order import (
    Run,
    layout_page,
    xy_cut,
)
from markitforge.converter_utils.pdf._reflow import (
    build_vocabulary,
    is_list_item,
    join_lines,
    render_flow,
)

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), "test_files")

TWO_COL = "mf_layout_two_col.pdf"
THREE_COL = "mf_layout_three_col.pdf"
SIDEBAR = "mf_layout_sidebar.pdf"
FURNITURE = "mf_layout_furniture.pdf"
SHORT_DOC = "mf_layout_two_pages.pdf"
REFLOW = "mf_layout_reflow.pdf"
OUTLINE = "mf_layout_outline.pdf"
NUMBERED = "mf_layout_numbered.pdf"
TAGGED = "mf_layout_tagged.pdf"
GARBAGE = "mf_layout_garbage.pdf"
LONG_TABLE = "mf_layout_long_table.pdf"
# Built by tests/test_pdf_tables_math.py.
TBL_BORDERLESS = "mf_tbl_borderless.pdf"
TBL_WRAPPED = "mf_tbl_wrapped.pdf"
TBL_COLSPAN = "mf_tbl_colspan.pdf"
TBL_NEGATIVES = "mf_tbl_negatives.pdf"
MATH_INLINE = "mf_math_inline.pdf"

PAGE_W, PAGE_H = 612.0, 792.0

FILLER = (
    "The survey covered many sites and each team wrote down what it saw "
    "during the season. Results were checked twice before anyone drew a "
    "conclusion from them. "
)
TABLE_ROWS = [
    ["Site", "Visits", "Score"],
    ["Harbour", "14", "0.82"],
    ["Ridge", "9", "0.67"],
    ["Valley", "21", "0.91"],
]


# -- fixture builders -------------------------------------------------------


def _styles():
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.styles import ParagraphStyle

    body = ParagraphStyle(
        "body", fontName="Helvetica", fontSize=9, leading=11, alignment=TA_JUSTIFY
    )
    small = ParagraphStyle("small", fontName="Helvetica", fontSize=7.5, leading=9)
    return body, small


def _draw_paragraphs(c, texts, style, x, top, width, gap=6.0):
    """Draw paragraphs downwards from ``top`` (PDF y); returns the new y."""
    from reportlab.platypus import Paragraph

    y = top
    for text in texts:
        para = Paragraph(text, style)
        _w, h = para.wrapOn(c, width, 2000)
        para.drawOn(c, x, y - h)
        y -= h + gap
    return y


def _draw_table(c, x, top, width):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(TABLE_ROWS, colWidths=[width / 3] * 3)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    _w, h = table.wrapOn(c, width, 2000)
    table.drawOn(c, x, top - h)
    return top - h


def _canvas(path: str, title: str):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setTitle(title)
    return c


def _build_two_col(path: str) -> None:
    """Title and abstract over two columns, a full-width table in the middle
    of the page, two more columns and a full-width footnote."""
    body, small = _styles()
    c = _canvas(path, "Two Column Fixture")
    left, gutter = 54.0, 18.0
    full = PAGE_W - 2 * left
    col = (full - gutter) / 2
    right = left + col + gutter

    c.setFont("Helvetica-Bold", 18)
    c.drawString(left, 730, "Coastal Survey Report")
    y = _draw_paragraphs(
        c,
        ["Abstract opens the report across both columns. " + FILLER],
        body,
        left,
        712,
        full,
    )

    top = y - 8
    _draw_paragraphs(
        c,
        [
            "Leftupper starts the first column. " + FILLER * 2,
            "Leftsecond continues it. " + FILLER,
        ],
        body,
        left,
        top,
        col,
    )
    y_after = _draw_paragraphs(
        c,
        [
            "Rightupper starts the second column. " + FILLER * 2,
            "Rightsecond continues it. " + FILLER,
        ],
        body,
        right,
        top,
        col,
    )

    y = _draw_table(c, left, y_after - 10, full)

    top = y - 14
    _draw_paragraphs(
        c, ["Leftlower follows the table. " + FILLER * 2], body, left, top, col
    )
    _draw_paragraphs(
        c, ["Rightlower ends the article. " + FILLER * 2], body, right, top, col
    )
    _draw_paragraphs(
        c,
        [
            "Footnote one explains the scoring method used for every site in the table above the lower columns."
        ],
        small,
        left,
        110,
        full,
    )
    c.showPage()
    c.save()


def _build_three_col(path: str) -> None:
    body, _small = _styles()
    c = _canvas(path, "Three Column Fixture")
    left, gutter = 48.0, 15.0
    col = (PAGE_W - 2 * left - 2 * gutter) / 3
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(PAGE_W / 2, 735, "Newsletter Of The Three Valleys")
    for i, name in enumerate(("Colone", "Coltwo", "Colthree")):
        _draw_paragraphs(
            c,
            [
                f"{name} opens this column. " + FILLER * 2,
                f"{name} closes this column. " + FILLER,
            ],
            body,
            left + i * (col + gutter),
            710,
            col,
        )
    c.showPage()
    c.save()


def _build_sidebar(path: str) -> None:
    body, small = _styles()
    c = _canvas(path, "Sidebar Fixture")
    left = 54.0
    main = 330.0
    side_x = left + main + 24
    side_w = PAGE_W - left - side_x
    _draw_paragraphs(
        c,
        [
            "Mainfirst is the main story. " + FILLER * 3,
            "Mainsecond carries on below. " + FILLER * 3,
        ],
        body,
        left,
        730,
        main,
    )
    _draw_paragraphs(
        c,
        ["Sidebar facts stand beside the story. " + FILLER],
        small,
        side_x,
        730,
        side_w,
    )
    c.showPage()
    c.save()


def _build_furniture(path: str, pages: int = 6) -> None:
    body, _small = _styles()
    c = _canvas(path, "Furniture Fixture")
    for n in range(1, pages + 1):
        c.setFont("Helvetica", 8)
        c.drawString(54, PAGE_H - 36, "Acme Annual Report")
        c.drawRightString(PAGE_W - 54, PAGE_H - 36, "10 November 2026")
        c.drawCentredString(PAGE_W / 2, 30, f"Page {n} of {pages}")
        _draw_paragraphs(
            c,
            [f"Chapter{n} body text is unique to page {n}. " + FILLER * 2],
            body,
            54,
            700,
            PAGE_W - 108,
        )
        c.showPage()
    c.save()


def _build_short_doc(path: str) -> None:
    _build_furniture(path, pages=2)


def _justified(c, x, y, width, text, font="Helvetica", size=10.0):
    """One line stretched to ``width`` with word spacing, like justified text."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    spaces = text.count(" ")
    extra = (width - stringWidth(text, font, size)) / spaces if spaces else 0.0
    t = c.beginText(x, y)
    t.setFont(font, size)
    t.setWordSpace(extra)
    t.textLine(text)
    t.setWordSpace(0)
    c.drawText(t)


REFLOW_PARA_1 = [
    "The review board examined every submission and agreed that the infor-",
    "mation was complete. Several reviewers praised the new state-of-the-",
    "art prototype that was presented at the spring meeting by Jean-",
    "Paul Martin, and they noted that the archive of every project e-",
    "mail was well organised.",
]
REFLOW_PARA_2 = [
    "A second paragraph follows after a wider gap and it has to stay sepa-",
    "rate from the first one.",
]


def _build_reflow(path: str) -> None:
    c = _canvas(path, "Reflow Fixture")
    x, width = 72.0, 440.0
    y = 720.0
    for lines in (REFLOW_PARA_1, REFLOW_PARA_2):
        for line in lines[:-1]:
            _justified(c, x, y, width, line)
            y -= 13
        c.setFont("Helvetica", 10)
        c.drawString(x, y, lines[-1])
        y -= 13 + 9  # paragraph gap
    c.setFont("Helvetica", 10)
    for text, dx in (
        ("- First item of the list", 0),
        ("- Second item is long enough that it wraps onto the following line and", 0),
        ("continues there with a hanging indent", 9),
        ("1. Numbered entry", 0),
        ("(a) Lettered entry", 0),
    ):
        c.drawString(x + dx, y, text)
        y -= 13
    y -= 9
    c.setFont("Courier", 9)
    for text in ("def add(a, b):", "    total = a + b", "    return total"):
        c.drawString(x, y, text)
        y -= 11
    c.showPage()
    c.save()


def _build_outline(path: str) -> None:
    """Bookmarked headings set in the body font: only the outline tells."""
    c = _canvas(path, "Outline Fixture")
    c.setFont("Helvetica", 10)
    sections = [
        ("Overview", 0, "The overview explains why the study was carried out."),
        ("Scope of Work", 1, "The scope lists the sites that were visited."),
        (None, None, None),
        ("Closing Notes", 0, "The notes thank everyone who took part."),
    ]
    y = 720.0
    for index, (title, level, text) in enumerate(sections):
        if title is None:
            c.showPage()
            c.setFont("Helvetica", 10)
            y = 720.0
            continue
        key = f"sec{index}"
        c.bookmarkPage(key)
        c.addOutlineEntry(title, key, level=level)
        c.drawString(72, y, title)
        y -= 13
        for i in range(3):
            c.drawString(
                72, y, f"{text} Sentence {i + 1} adds a little more detail here."
            )
            y -= 13
    c.showPage()
    c.save()


def _build_numbered(path: str) -> None:
    """No outline; headings differ from the body by weight, size and spacing."""
    c = _canvas(path, "Numbered Fixture")
    y = 730.0

    def body(n: int, tag: str) -> None:
        nonlocal y
        c.setFont("Helvetica", 10)
        for i in range(n):
            c.drawString(
                72,
                y,
                f"{tag} sentence {i + 1} is ordinary body text that fills the line.",
            )
            y -= 13

    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, y, "Field Manual")
    y -= 26
    body(3, "Preface")
    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, y, "1 Equipment")
    y -= 18
    body(3, "Equipment")
    y -= 10
    c.setFont("Helvetica-Bold", 10)
    c.drawString(72, y, "1.1 Radios")
    y -= 14
    body(3, "Radio")
    y -= 10
    c.setFont("Helvetica", 10.5)
    c.drawString(
        72, y, "This slightly larger sentence is still body text, not a heading."
    )
    y -= 14
    body(2, "Closing")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(72, y, "*BARCODE1234567890*")
    c.showPage()
    c.save()


def _build_tagged(path: str) -> None:
    """Marked content with heading tags, all in the body font."""
    c = _canvas(path, "Tagged Fixture")
    c.setFont("Helvetica", 10)
    y = 720.0
    mcid = 0
    for tag, text in (
        ("H1", "Tagged Title"),
        ("P", "A paragraph of ordinary text follows the title."),
        ("H2", "Tagged Section"),
        ("P", "Another paragraph of ordinary text ends the page."),
    ):
        c._code.append(f"/{tag} <</MCID {mcid}>> BDC")
        c.drawString(72, y, text)
        c._code.append("EMC")
        mcid += 1
        y -= 14
    c.showPage()
    c.save()


def _grey_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 80), (128, 128, 128)).save(buf, format="PNG")
    return buf.getvalue()


def _build_garbage(path: str) -> None:
    """Page 1: a text layer of glyphs without a usable character mapping.
    Page 2: a full-page image with a tiny stamp. Page 3: ordinary text."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("Vera", "Vera.ttf"))
    c = _canvas(path, "Garbage Fixture")
    c.setFont("Vera", 10)
    y = 720.0
    for _ in range(6):
        c.drawString(72, y, "".join(chr(0xE000 + (i % 7)) for i in range(40)))
        y -= 14
    c.showPage()
    c.drawImage(ImageReader(io.BytesIO(_grey_png())), 0, 0, PAGE_W, PAGE_H)
    c.setFont("Helvetica", 6)
    c.drawString(500, 20, "SCAN 0042")
    c.showPage()
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "This page has an ordinary, readable text layer.")
    c.showPage()
    c.save()


def _build_long_table(path: str) -> None:
    """A ruled table that runs over two pages and repeats its header."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13)
    rows = [["Item", "Batch", "Weight"]] + [
        [f"Part {i:02d}", f"B-{100 + i}", f"{i * 1.5:.1f} kg"] for i in range(1, 61)
    ]
    table = Table(rows, colWidths=[160, 120, 120], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    doc = SimpleDocTemplate(path, pagesize=letter, title="Long Table", invariant=1)
    doc.build(
        [
            Paragraph("The parts list starts here and continues overleaf.", body),
            table,
            Paragraph("Weights were measured after the final inspection.", body),
        ]
    )


BUILDERS = {
    TWO_COL: _build_two_col,
    THREE_COL: _build_three_col,
    SIDEBAR: _build_sidebar,
    FURNITURE: _build_furniture,
    SHORT_DOC: _build_short_doc,
    REFLOW: _build_reflow,
    OUTLINE: _build_outline,
    NUMBERED: _build_numbered,
    TAGGED: _build_tagged,
    GARBAGE: _build_garbage,
    LONG_TABLE: _build_long_table,
}


def _fixture(name: str) -> str:
    path = os.path.join(TEST_FILES_DIR, name)
    if not os.path.exists(path):
        if name not in BUILDERS:
            pytest.skip(f"{name} is built by another test module")
        pytest.importorskip("reportlab")
        BUILDERS[name](path)
    return path


def _convert(name: str, **kwargs):
    return MarkitForge().convert(_fixture(name), **kwargs)


def _in_order(text: str, *needles: str) -> None:
    positions = [text.index(n) for n in needles]
    assert positions == sorted(positions), list(zip(needles, positions))


# -- 1. reading order -------------------------------------------------------


class TestReadingOrder:
    def test_two_columns_with_title_table_and_footnote(self):
        md = _convert(TWO_COL).markdown
        _in_order(
            md,
            "# Coastal Survey Report",
            "Abstract opens",
            "Leftupper starts",
            "Leftsecond continues",
            "Rightupper starts",
            "Rightsecond continues",
            "| Site",
            "Leftlower follows",
            "Rightlower ends",
            "Footnote one explains",
        )

    def test_columns_are_not_interleaved(self):
        md = _convert(TWO_COL).markdown
        left = md[md.index("Leftupper starts") : md.index("Rightupper starts")]
        assert "Rightupper" not in left and "Rightsecond" not in left
        assert left.count("The survey covered many sites") == 3

    def test_table_keeps_its_place_and_content(self):
        md = _convert(TWO_COL).markdown
        assert md.count("Harbour") == 1
        table = [line for line in md.splitlines() if line.startswith("|")]
        assert len(table) == 5 and "Valley" in table[-1]

    def test_three_columns(self):
        md = _convert(THREE_COL).markdown
        _in_order(
            md,
            "Newsletter Of The Three Valleys",
            "Colone opens",
            "Colone closes",
            "Coltwo opens",
            "Coltwo closes",
            "Colthree opens",
            "Colthree closes",
        )

    def test_sidebar_after_main_text(self):
        md = _convert(SIDEBAR).markdown
        _in_order(md, "Mainfirst is", "Mainsecond carries", "Sidebar facts")

    def test_simple_order_reads_across_columns(self):
        md = _convert(TWO_COL, pdf_reading_order="simple").markdown
        assert md.index("Rightupper starts") < md.index("Leftsecond continues")

    def test_unknown_reading_order(self):
        with pytest.raises(Exception, match="Unknown pdf_reading_order 'zigzag'"):
            _convert(TWO_COL, pdf_reading_order="zigzag")

    def test_pdfminer_backend_is_untouched(self):
        result = _convert(FURNITURE, pdf_backend="pdfminer")
        assert "Acme Annual Report" in result.markdown
        assert "removed_headers_footers" not in result.metadata
        assert "#" not in result.markdown

    def test_xy_cut_units(self):
        def run(x0, x1, top, text="words"):
            r = Run(x0, x1, top, top + 10, 10.0)
            r.parts.append(text * 6)
            r.n = len(text) * 6
            return r

        # Two columns of eight lines with a 30pt gutter.
        left = [run(50, 250, 100 + 12 * i) for i in range(8)]
        right = [run(280, 480, 100 + 12 * i) for i in range(8)]
        blocks = xy_cut(left + right, char_width=5.0)
        assert [len(b) for b in blocks] == [8, 8]
        assert all(r.x0 == 50 for r in blocks[0])

        # Justified text: word gaps do not line up, so no column appears.
        words = [
            run(50 + 43 * j + (i % 3) * 7, 50 + 43 * j + (i % 3) * 7 + 30, 100 + 12 * i)
            for i in range(8)
            for j in range(9)
        ]
        assert len(xy_cut(words, char_width=5.0)) == 1

        # Label/value pairs are read row by row, not column by column.
        labels = [run(50, 90, 100 + 12 * i, "k") for i in range(8)]
        values = [run(200, 240, 100 + 12 * i, "v") for i in range(8)]
        assert len(xy_cut(labels + values, char_width=5.0)) == 1

        # A full-width obstacle splits the columns into two groups.
        table = Block(top=200, x0=50, markdown="| t |", x1=480, bottom=240)
        upper = [run(x, x + 200, 100 + 12 * i) for i in range(6) for x in (50, 280)]
        lower = [run(x, x + 200, 250 + 12 * i) for i in range(6) for x in (50, 280)]
        blocks = xy_cut(upper + [table] + lower, char_width=5.0)
        kinds = ["T" if isinstance(b[0], Block) else b[0].x0 for b in blocks]
        assert kinds == [50, 280, "T", 50, 280]

    def test_layout_page_keeps_blocks_in_flow(self):
        chars = []
        for i in range(8):
            for x in (50, 300):
                for k, ch in enumerate("columnwordsgohere"):
                    chars.append(
                        {
                            "text": ch,
                            "x0": x + 6 * k,
                            "x1": x + 6 * k + 5.5,
                            "top": 100 + 12 * i,
                            "bottom": 110 + 12 * i,
                            "size": 10.0,
                            "fontname": "Helvetica",
                        }
                    )
        figure = Block(top=130, x0=300, markdown="![fig](fig.png)", x1=400, bottom=132)
        flow = layout_page(chars, [figure])
        lines = [x for x in flow if isinstance(x, TextLine)]
        assert len(lines) == 16
        assert len({line.block for line in lines}) == 2
        position = flow.index(figure)
        assert all(isinstance(x, TextLine) and x.x0 == 50 for x in flow[:8])
        assert 8 < position < len(flow) - 1


# -- 2. headings --------------------------------------------------------------


class TestHeadings:
    def test_outline_headings_in_body_font(self):
        md = _convert(OUTLINE).markdown
        lines = md.splitlines()
        assert "# Overview" in lines
        assert "## Scope of Work" in lines
        assert "# Closing Notes" in lines
        assert sum(1 for line in lines if line.startswith("#")) == 3
        _in_order(md, "# Overview", "The overview explains", "## Scope of Work")

    def test_outline_can_be_ignored(self):
        md = _convert(OUTLINE, pdf_outline_headings=False).markdown
        assert "#" not in md
        assert "Scope of Work" in md

    def test_outline_pages(self):
        result = _convert(OUTLINE)
        assert "# Closing Notes" in result.pages[1].markdown
        assert "# Overview" in result.pages[0].markdown

    def test_scored_headings_without_outline(self):
        md = _convert(NUMBERED).markdown
        lines = md.splitlines()
        assert "# Field Manual" in lines
        assert "## 1 Equipment" in lines
        assert "### 1.1 Radios" in lines
        headings = [line for line in lines if line.startswith("#")]
        assert len(headings) == 3
        assert "This slightly larger sentence is still body text" in md
        assert not any("BARCODE" in h for h in headings)

    def test_tagged_headings(self):
        md = _convert(TAGGED).markdown
        lines = md.splitlines()
        assert "# Tagged Title" in lines
        assert "## Tagged Section" in lines
        assert sum(1 for line in lines if line.startswith("#")) == 2

    def test_size_clusters(self):
        clusters = SizeClusters(
            Counter(
                {10.0: 900, 10.2: 20, 8.0: 40, 12.0: 30, 13.9: 4, 14.0: 12, 20.0: 9}
            )
        )
        assert clusters.body == 10.0
        assert [clusters.rank(s) for s in (8, 10, 10.2, 12, 13.9, 14, 20)] == [
            -1,
            0,
            0,
            1,
            2,
            2,
            3,
        ]

    def test_numbering_patterns(self):
        for text in ("2 Methods", "2.1 Setup", "A. Proofs", "Chapter 3", "IV. Results"):
            assert has_numbering(text), text
        for text in ("Methods", "2023 was a good year", "Avery wrote this"):
            assert not has_numbering(text), text

    def test_score_heading(self):
        clusters = SizeClusters(Counter({10.0: 1000, 14.0: 30}))

        def line(text, size=10.0, bold=False, **kw):
            return TextLine(text, 72, 100, 300, 100 + size, size, bold, **kw)

        body = line("Body text follows the heading and runs on for a while here")
        common = dict(typical_gap=3.0, next_line=body)
        assert (
            score_heading(line("Results", 14.0), clusters, gap_above=2.0, **common) >= 3
        )
        assert (
            score_heading(
                line("2.1 Setup", bold=True), clusters, gap_above=12.0, **common
            )
            >= 3
        )
        # Bold alone, in the middle of a paragraph, is not enough.
        assert (
            score_heading(
                line("Important", bold=True), clusters, gap_above=3.0, **common
            )
            < 3
        )
        # Same size, not bold: never a heading, whatever the spacing.
        assert score_heading(line("2.1 Setup"), clusters, gap_above=30.0, **common) == 0
        # Safety rules: sentences, codes, page numbers, tables, code.
        for text in ("This is a sentence.", "*TXN98765202411231432*", "Page 3 of 9"):
            assert (
                score_heading(line(text, 14.0), clusters, gap_above=None, **common) == 0
            )
        assert (
            score_heading(
                line("Total", 14.0, tabular=True), clusters, gap_above=None, **common
            )
            == 0
        )

    def test_heading_tag_level(self):
        assert [
            heading_tag_level(t) for t in ("H1", "H3", "H", "Title", "P", None)
        ] == [
            1,
            3,
            1,
            1,
            0,
            0,
        ]

    def test_outline_matching_units(self):
        def line(text, top):
            return TextLine(text, 72, top, 300, top + 10, 10.0, False)

        flow = [
            line("1 Introduction", 100),
            line("Body text of the introduction.", 114),
            line("A Very Long Section Title That", 140),
            line("Wraps Over Two Lines", 152),
            line("More body text.", 166),
        ]
        outline = [
            OutlineEntry(1, "Introduction", 1),
            OutlineEntry(2, "A Very Long Section Title That Wraps Over Two Lines", 1),
        ]
        assert assign_headings({1: flow}, Counter({10.0: 100}), outline) == "outline"
        assert [(x.text, x.heading_level) for x in flow if x.heading_level] == [
            ("1 Introduction", 1),
            ("A Very Long Section Title That Wraps Over Two Lines", 2),
        ]
        assert len(flow) == 4

        # An outline that matches nothing falls back to the heuristic.
        flow = [line("Plain text only.", 100)]
        outline = [OutlineEntry(1, "Missing Chapter", 1)]
        assert assign_headings({1: flow}, Counter({10.0: 100}), outline) == "none"

    def test_cyclic_outline_terminates(self):
        from pdfminer.pdftypes import PDFObjRef

        class FakeDoc:
            def __init__(self):
                self.objects = {
                    1: {"First": PDFObjRef(self, 2)},
                    2: {"Title": b"Loop", "Next": PDFObjRef(self, 2)},
                }
                self.catalog = {"Outlines": PDFObjRef(self, 1)}

            def getobj(self, objid):
                return self.objects[objid]

        assert read_outline(FakeDoc(), {}) == [(1, "Loop", None)]


# -- 3. running headers and footers -------------------------------------------


class TestFurniture:
    def test_headers_and_footers_removed(self):
        result = _convert(FURNITURE)
        md = result.markdown
        assert "Acme Annual Report" not in md
        assert "10 November 2026" not in md
        assert "Page 3 of 6" not in md and "of 6" not in md
        for n in range(1, 7):
            assert f"Chapter{n} body text is unique to page {n}." in md
        # The two header texts share a row and are reported together.
        assert result.metadata["removed_headers_footers"] == [
            "Acme Annual Report 10 November 2026",
            "Page 1 of 6",
        ]

    def test_pages_are_stripped_too(self):
        result = _convert(FURNITURE, page_markers=True)
        assert len(result.pages) == 6
        assert all("Acme" not in p.markdown for p in result.pages)
        assert "Acme" not in result.markdown

    def test_can_be_turned_off(self):
        result = _convert(FURNITURE, pdf_strip_headers_footers=False)
        assert result.markdown.count("Acme Annual Report") == 6
        assert "Page 3 of 6" in result.markdown
        assert "removed_headers_footers" not in result.metadata

    def test_short_documents_are_never_stripped(self):
        result = _convert(SHORT_DOC)
        assert result.markdown.count("Acme Annual Report") == 2
        assert "Page 2 of 2" in result.markdown
        assert "removed_headers_footers" not in result.metadata

    def test_removed_with_line_by_line_output(self):
        result = _convert(FURNITURE, pdf_reading_order="simple", pdf_reflow=False)
        assert "Acme Annual Report" not in result.markdown
        assert "Chapter4 body text" in result.markdown
        assert "Page 1 of 6" in result.metadata["removed_headers_footers"]

    def test_furniture_key(self):
        assert furniture_key("Page 3 of 12") == furniture_key("page  11 of 12")
        assert furniture_key("xiv") == furniture_key("7")
        assert furniture_key("Annual Report") != furniture_key("Annual Review")

    @staticmethod
    def _pages(count, header="Report Title", body=True, top=30.0):
        pages = []
        for n in range(1, count + 1):
            flow = [TextLine(header, 72, top, 200, top + 8, 8.0, False)]
            if body:
                flow.append(TextLine(f"Body {n}", 72, 300, 200, 310, 10.0, False))
            flow.append(TextLine(str(n), 300, 760, 306, 768, 8.0, False))
            pages.append(PageLines(n, 792.0, flow))
        return pages

    def test_find_and_strip_units(self):
        pages = self._pages(5)
        found = find_furniture(pages)
        assert set(found) == {("top", "report title"), ("bottom", "0")}
        assert strip_furniture(pages, found) == ["Report Title", "1"]
        assert [[x.text for x in p.flow] for p in pages[:2]] == [["Body 1"], ["Body 2"]]

    def test_needs_three_pages_and_a_majority(self):
        assert find_furniture(self._pages(2)) == {}
        pages = self._pages(10)
        names = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()
        for page in pages[4:]:
            page.flow[0].text = f"Section {names[page.index - 1]}"
        # The header repeats on 4 of 10 pages only; page numbers on all.
        assert set(find_furniture(pages)) == {("bottom", "0")}

    def test_body_text_is_not_furniture(self):
        pages = self._pages(5, top=300.0)
        assert ("top", "report title") not in find_furniture(pages)

    def test_alternating_book_headers(self):
        pages = self._pages(8)
        for page in pages:
            page.flow[0].text = "Book Title" if page.index % 2 else "Chapter Title"
        found = find_furniture(pages)
        assert ("top", "book title") in found and ("top", "chapter title") in found

    def test_never_blanks_the_pages(self):
        # Every page holds nothing but the repeated lines: that is content.
        pages = self._pages(5, body=False)
        found = find_furniture(pages)
        assert strip_furniture(pages, found) == []
        assert all(len(p.flow) == 2 for p in pages)

    def test_strip_furniture_text(self):
        found = {("top", "report title"): "Report Title", ("bottom", "0"): "1"}
        text = "Report Title\n\nBody mentions Report Title inside.\n42\nmore\nlines\nhere\n\n7\n"
        # "42" opens the page, so it is not a page number; the "7" ending it is.
        assert strip_furniture_text(text, found) == (
            "\nBody mentions Report Title inside.\n42\nmore\nlines\nhere\n\n"
        )
        assert strip_furniture_text("Just text\n", found) == "Just text\n"


# -- 4. paragraph reflow ------------------------------------------------------


class TestReflow:
    def test_paragraphs_joined_and_separated(self):
        md = _convert(REFLOW).markdown
        paragraphs = md.split("\n\n")
        assert paragraphs[0] == (
            "The review board examined every submission and agreed that the "
            "information was complete. Several reviewers praised the new "
            "state-of-the-art prototype that was presented at the spring "
            "meeting by Jean-Paul Martin, and they noted that the archive of "
            "every project e-mail was well organised."
        )
        assert paragraphs[1] == (
            "A second paragraph follows after a wider gap and it has to stay "
            "separate from the first one."
        )

    def test_list_items_stay_on_their_own_lines(self):
        lines = _convert(REFLOW).markdown.splitlines()
        assert "- First item of the list" in lines
        assert (
            "- Second item is long enough that it wraps onto the following line "
            "and continues there with a hanging indent" in lines
        )
        assert "1. Numbered entry" in lines
        assert "(a) Lettered entry" in lines

    def test_code_is_left_alone(self):
        md = _convert(REFLOW).markdown
        assert "def add(a, b):\n    total = a + b\n    return total" in md

    def test_reflow_off_keeps_line_breaks(self):
        md = _convert(REFLOW, pdf_reflow=False).markdown
        assert "agreed that the infor-\nmation was complete." in md
        assert "information" not in md

    def test_wrapped_prose_in_columns(self):
        md = _convert(TWO_COL).markdown
        para = next(p for p in md.split("\n\n") if p.startswith("Leftupper"))
        assert "\n" not in para
        assert para.endswith("conclusion from them.")

    def test_join_lines(self):
        vocab = build_vocabulary(
            ["a well-known method", "the chat window", "an optimized build"]
        )
        cases = [
            ("the infor-", "mation was", "the information was"),
            ("state-of-the-", "art design", "state-of-the-art design"),
            ("by Jean-", "Paul Martin", "by Jean-Paul Martin"),
            ("COVID-", "19 cases", "COVID-19 cases"),
            ("project e-", "mail was", "project e-mail was"),
            ("a well-", "known method", "a well-known method"),
            ("a self-", "contained unit", "a self-contained unit"),
            ("with chat-", "optimized models", "with chat-optimized models"),
            ("soft­", "ware", "software"),
            ("a range –", "wide", "a range – wide"),
            ("ends here.", "Next sentence", "ends here. Next sentence"),
            ("items 1-", "3 are", "items 1-3 are"),
        ]
        for first, second, expected in cases:
            assert join_lines(first, second, vocab) == expected, (first, second)

    def test_vocabulary_skips_split_words(self):
        vocab = build_vocabulary(["the infor-", "mation was sent", "by e-mail"])
        assert "infor" not in vocab and "mation" not in vocab
        assert {"the", "was", "sent", "e-mail"} <= vocab

    def test_list_markers(self):
        for text in ("- item", "• item", "1. item", "(a) item", "iv) item", "12) item"):
            assert is_list_item(text), text
        for text in ("item", "-5 degrees", "1.5 litres", "a word", "e.g. this"):
            assert not is_list_item(text), text

    @staticmethod
    def _line(text, top, x0=72.0, x1=500.0, **kw):
        kw.setdefault("char_width", 5.0)
        kw.setdefault("first_word_width", 20.0)
        return TextLine(text, x0, top, x1, top + 10, 10.0, False, **kw)

    def test_render_flow_units(self):
        line = self._line
        flow = [
            line("First line of a paragraph that is", 100),
            line("wrapped over two lines.", 112, x1=200),
            line("Second paragraph after a gap", 140),
            line("continues here.", 152, x1=160),
            Block(top=170, x0=72, markdown="| a | b |"),
            line("Indented paragraphs need no gap", 200, x0=86),
            line("between them, the indent of the", 212),
            line("first line is enough.", 224, x1=180),
            line("Here the next one starts and it", 236, x0=86),
            line("ends on this line.", 248, x1=170),
        ]
        assert render_flow(flow).split("\n\n") == [
            "First line of a paragraph that is wrapped over two lines.",
            "Second paragraph after a gap continues here.",
            "| a | b |",
            "Indented paragraphs need no gap between them, the indent of the "
            "first line is enough.",
            "Here the next one starts and it ends on this line.",
        ]
        assert render_flow(flow, reflow=False).split("\n\n")[0] == (
            "First line of a paragraph that is\nwrapped over two lines."
        )

    def test_short_lines_and_tables_are_not_joined(self):
        line = self._line
        address = [
            line("Jane Doe", 100, x1=120),
            line("12 Harbour Road", 112, x1=150),
            line("Springfield and a longer last line", 124, x1=260),
        ]
        assert render_flow(address) == (
            "Jane Doe\n12 Harbour Road\nSpringfield and a longer last line"
        )
        rows = [
            line("Total 12.00", 100, tabular=True),
            line("Tax 1.20", 112, tabular=True),
        ]
        assert render_flow(rows) == "Total 12.00\nTax 1.20"

    def test_paragraph_continues_in_next_column(self):
        line = self._line
        flow = [
            line("A sentence starts in the first column and", 100, block=0),
            line("runs on to the bottom of it where the", 112, block=0),
            line(
                "text simply continues in the second one.", 100, x0=320, x1=560, block=1
            ),
            line("New paragraph. It stays apart", 130, x0=320, x1=560, block=1),
        ]
        assert render_flow(flow).split("\n\n") == [
            "A sentence starts in the first column and runs on to the bottom "
            "of it where the text simply continues in the second one.",
            "New paragraph. It stays apart",
        ]


# -- 5. scan detection --------------------------------------------------------


class TestScanDetection:
    def test_garbled_and_sparse_pages_warn(self):
        result = _convert(GARBAGE)
        assert result.warnings == [
            "Page 1 has an unreadable text layer (garbled characters); it may "
            "be a scan. Try the markitforge-ocr plugin.",
            "Page 2 has almost no text over a full-page image; it may be a "
            "scan. Try the markitforge-ocr plugin.",
        ]
        assert "ordinary, readable text layer" in result.pages[2].markdown

    def test_clean_documents_do_not_warn(self):
        for name in (TWO_COL, REFLOW, FURNITURE, OUTLINE):
            assert _convert(name).warnings == [], name

    def test_is_garbled(self):
        assert _is_garbled(["�"] * 30 + list("ok"))
        assert _is_garbled(["(cid:12)"] * 20 + list("some text"))
        assert _is_garbled([chr(0xE000 + i) for i in range(30)])
        assert not _is_garbled(list("An ordinary sentence with accents: éè 中文."))
        assert not _is_garbled(["�"] * 3)  # too few characters to judge
        assert not _is_garbled(["�"] * 10 + list("mostly readable text here"))

    def test_merged_warnings(self):
        assert _garbled_warnings(list(range(1, 8))) == [
            "Pages 1-7 have unreadable text layers (garbled characters); they "
            "may be scans. Try the markitforge-ocr plugin."
        ]
        assert _sparse_warnings([]) == []


# -- 6. one pass ----------------------------------------------------------------


class TestOnePass:
    def test_pages_are_parsed_once(self, monkeypatch):
        import pdfplumber

        from markitforge.converters import _pdf_converter

        calls = []
        opened = []
        original_open = pdfplumber.open

        def extract_text(*args, **kwargs):
            calls.append("pdfminer")
            raise AssertionError("pdfminer text extraction is not needed")

        def open_pdf(*args, **kwargs):
            opened.append(1)
            return original_open(*args, **kwargs)

        monkeypatch.setattr(
            _pdf_converter.pdfminer.high_level, "extract_text", extract_text
        )
        monkeypatch.setattr(pdfplumber, "open", open_pdf)
        result = _convert(FURNITURE)
        assert calls == [] and len(opened) == 1
        assert len(result.pages) == 6

    def test_line_by_line_output_matches_pdfminer(self):
        import pdfminer.high_level

        path = _fixture(SHORT_DOC)
        result = MarkitForge().convert(
            path, pdf_reading_order="simple", pdf_reflow=False
        )
        raw = pdfminer.high_level.extract_text(path)
        normalised = "\n".join(line.rstrip() for line in raw.split("\n"))
        assert result.markdown == re.sub(r"\n{3,}", "\n\n", normalised)


# -- tables and math in the page pipeline ---------------------------------------


class _FakeLlm:
    """Shaped like openai.OpenAI for client.chat.completions.create()."""

    def __init__(self, reply: str):
        self.calls = []
        outer = self

        class _Completions:
            def create(self, model, messages, **kwargs):
                from types import SimpleNamespace

                outer.calls.append(messages)
                message = SimpleNamespace(content=reply)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


class TestTablesInPipeline:
    def test_wrapped_cells_stay_in_their_rows(self):
        md = _convert(TBL_WRAPPED).markdown
        rows = [line for line in md.splitlines() if line.startswith("|")]
        assert len(rows) == 6  # header, separator and four tasks: one table
        assert any(
            "Inspect conveyor belt tension and realign the drive rollers" in row
            for row in rows
        )
        _in_order(md, "Open maintenance tasks", "| Code", "Hours are estimates")
        assert md.count("realign the drive rollers") == 1

    def test_borderless_table_with_text_around_it(self):
        md = _convert(TBL_BORDERLESS).markdown
        assert "# Shipment Summary" in md.splitlines()
        assert re.search(r"\| Steel brackets\s+\|\s+\| 87\s+\| 2,114.50\s+\|", md)
        _in_order(md, "The figures below", "| Product", "Revenue is reported")

    def test_table_format(self):
        default = _convert(TBL_COLSPAN).markdown
        assert "<table>" not in default
        assert re.search(r"\| Region\s+\| 2024\s+\|", default)
        auto = _convert(TBL_COLSPAN, table_format="auto").markdown
        assert '<th colspan="2">2024</th>' in auto
        assert "a &lt; b &amp; c" in auto
        html = _convert(TBL_BORDERLESS, table_format="html").markdown
        assert "<table>" in html and "<td>Rotterdam</td>" in html
        # Without merged cells "auto" stays Markdown.
        assert "<table>" not in _convert(TBL_BORDERLESS, table_format="auto").markdown

    def test_prose_and_lists_are_not_tables(self):
        result = _convert(TBL_NEGATIVES)
        assert not any(line.startswith("|") for line in result.markdown.splitlines())
        assert "The committee reviewed the proposal" in result.markdown

    def test_real_forms_keep_the_form_layout(self):
        md = (
            MarkitForge()
            .convert(os.path.join(TEST_FILES_DIR, "movie-theater-booking-2024.pdf"))
            .markdown
        )
        assert "| Billing Contact: | accounting@premierent.com" in md
        md = (
            MarkitForge()
            .convert(os.path.join(TEST_FILES_DIR, "REPAIR-2022-INV-001_multipage.pdf"))
            .markdown
        )
        assert "| Insured name" in md and "| GRAND TOTAL" in md

    def test_table_continued_on_next_page_is_merged(self):
        result = _convert(LONG_TABLE)
        md = result.markdown
        assert md.count("| Item") == 1
        rows = [line for line in md.splitlines() if line.startswith("| Part ")]
        assert len(rows) == 60
        assert [r.split("|")[1].strip() for r in rows] == [
            f"Part {i:02d}" for i in range(1, 61)
        ]
        _in_order(md, "The parts list starts", "| Item", "| Part 60", "Weights were")
        # Only the joined text is merged; each page keeps its own rows.
        assert "| Part 01" in result.pages[0].markdown
        assert "| Part 60" in result.pages[1].markdown
        assert "| Part 60" not in result.pages[0].markdown
        assert "Weights were measured" in result.pages[1].markdown

    def test_merge_can_be_turned_off(self):
        result = _convert(LONG_TABLE, pdf_merge_tables=False)
        assert result.markdown.count("| Item") == 2
        assert "| Part 60" in result.pages[1].markdown

    def test_page_markers_keep_pages_apart(self):
        # With page markers the page boundary stays visible, so each page
        # shows its own part of the table, header included.
        md = _convert(LONG_TABLE, page_markers=True).markdown
        assert md.count("| Item") == 2
        assert "<!-- page 2 -->" in md
        _in_order(md, "| Part 01", "<!-- page 2 -->", "| Part 60")


class TestMathInPipeline:
    def test_inline_math_is_opt_in(self):
        plain = _convert(MATH_INLINE).markdown
        assert "$" not in plain
        md = _convert(MATH_INLINE, pdf_inline_math=True).markdown
        assert "$E = mc^{2}$" in md
        assert "$x_{i} + y_{i} = z_{i}$" in md
        assert "Okafor3" in md and "21st of March" in md

    def test_reflow_keeps_math_runs_whole(self):
        md = _convert(MATH_INLINE, pdf_inline_math=True).markdown
        assert md.count("$") % 2 == 0
        for line in md.splitlines():
            assert line.count("$") % 2 == 0, line
        assert (
            "Mass and energy are related by $E = mc^{2}$ in special relativity." in md
        )

    def test_inline_math_with_line_by_line_output(self):
        md = _convert(
            MATH_INLINE,
            pdf_inline_math=True,
            pdf_reading_order="simple",
            pdf_reflow=False,
        ).markdown
        assert "$E = mc^{2}$" in md

    def test_display_formula_without_math_fonts_is_transcribed(self):
        client = _FakeLlm("\\sigma^2 = \\sum_k (x_k - \\mu)^2")
        result = _convert(
            MATH_INLINE, transcribe_formulas=True, llm_client=client, llm_model="m"
        )
        md = result.markdown
        assert len(client.calls) == 1
        assert "$$\n\\sigma^2 = \\sum_k (x_k - \\mu)^2\n$$" in md
        _in_order(md, "21st of March", "$$", "Ordinary closing sentence")

    def test_display_and_inline_together(self):
        client = _FakeLlm("\\sigma^2")
        md = _convert(
            MATH_INLINE,
            transcribe_formulas=True,
            llm_client=client,
            llm_model="m",
            pdf_inline_math=True,
        ).markdown
        assert "$$\n\\sigma^2\n$$" in md
        assert "$\\alpha \\leq \\beta$" in md


if __name__ == "__main__":
    if "--rebuild" in sys.argv:
        for fixture_name, build in BUILDERS.items():
            build(os.path.join(TEST_FILES_DIR, fixture_name))
            print("wrote", fixture_name)
    else:
        raise SystemExit(pytest.main([__file__]))
