# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for fixes made after reviewing the layout algorithms."""

import io
import re
import zipfile

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from markitforge import MarkitForge, StreamInfo
from markitforge.converter_utils._archive_limits import ArchiveBudget
from markitforge.converter_utils.pdf._furniture import _stable_or_paged
from markitforge.converters._pdf_converter import (
    _cell_covered,
    _flag,
)
from markitforge.converters._xlsx_converter import _scan_merge_cells


def _pdf(draw) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    draw(c)
    c.save()
    return buf.getvalue()


def _convert(data: bytes, **kwargs):
    return MarkitForge().convert_stream(
        io.BytesIO(data), stream_info=StreamInfo(extension=".pdf"), **kwargs
    )


# -- running headers and footers ---------------------------------------------


def test_page_numbers_and_constant_lines_are_furniture() -> None:
    assert _stable_or_paged([(i, f"Page {i} of 6") for i in range(1, 7)])
    assert _stable_or_paged([(i, str(i - 1)) for i in range(2, 7)])  # offset
    assert _stable_or_paged([(i, "Acme Handbook") for i in range(1, 7)])
    assert _stable_or_paged([(1, "i"), (2, "ii"), (3, "iii"), (4, "iv")])


def test_varying_totals_are_not_furniture() -> None:
    assert not _stable_or_paged(
        [(1, "Subtotal: 412.50"), (2, "Subtotal: 98.10"), (3, "Subtotal: 77.00")]
    )
    # Increasing, but not in step with the pages: a running balance.
    assert not _stable_or_paged(
        [(1, "Balance 1,200"), (2, "Balance 1,450"), (3, "Balance 1,900")]
    )


def test_subtotal_footers_survive_conversion() -> None:
    totals = ["412.50", "98.10", "77.00", "1310.25"]

    def draw(c) -> None:
        for i, total in enumerate(totals, 1):
            c.setFont("Helvetica", 11)
            c.drawString(72, 700, f"Items listed for section {i} of the order.")
            c.drawString(72, 40, f"Subtotal: {total}")
            c.drawCentredString(300, 20, f"Page {i}")
            c.showPage()

    result = _convert(_pdf(draw))
    for total in totals:
        assert f"Subtotal: {total}" in result.markdown
    assert "Page 2" not in result.markdown


# -- rotated text --------------------------------------------------------------


def test_rotated_text_keeps_its_lines_and_order() -> None:
    def draw(c) -> None:
        c.setFont("Helvetica", 11)
        c.drawString(72, 780, "Upright intro line.")
        c.saveState()
        c.translate(200, 200)
        c.rotate(90)
        for i, text in enumerate(["First sideways", "Second sideways"]):
            c.drawString(0, -i * 16, text)
        c.restoreState()

    md = _convert(_pdf(draw)).markdown
    assert "First sideways" in md and "Second sideways" in md
    assert md.index("First sideways") < md.index("Second sideways")
    assert "First sideways Second sideways" not in md  # separate lines


# -- continued tables keep their pages ------------------------------------------


def test_merged_table_rows_stay_on_their_pages() -> None:
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    rows = [["Code", "City", "Count"]] + [
        [f"C{i:03d}", f"City{i}", str(100 + i)] for i in range(1, 61)
    ]
    buf = io.BytesIO()
    table = Table(rows, repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(buf, pagesize=A4, invariant=1).build([table])
    result = _convert(buf.getvalue())

    header = re.compile(r"^\| Code +\| City +\| Count +\|$", re.M)
    assert len(header.findall(result.markdown)) == 1  # merged in the text
    assert len(result.pages) == 2
    assert "C060" in result.pages[1].markdown  # rows stay on their own page
    assert "C060" not in result.pages[0].markdown
    marked = _convert(buf.getvalue(), page_markers=True).markdown
    assert "<!-- page 2 -->" in marked and len(header.findall(marked)) == 2


# -- form coverage ----------------------------------------------------------------


def test_short_form_cells_need_exact_matches() -> None:
    cells = ["Notes", "100", "Owner name"]
    exact = set(cells)
    assert not _cell_covered("No", exact, cells)
    assert not _cell_covered("1", exact, cells)
    assert _cell_covered("100", exact, cells)
    assert _cell_covered("Owner", exact, cells)  # part of a wrapped cell
    assert not _cell_covered("Own", exact, cells)  # not a whole word


# -- boolean options ----------------------------------------------------------------


def test_string_flags() -> None:
    for value in ("false", "False", "0", "no", "off", ""):
        assert _flag({"x": value}, "x", True) is False
    for value in ("true", "1", "yes", "on"):
        assert _flag({"x": value}, "x", False) is True
    assert _flag({}, "x", True) is True
    assert _convert(
        _pdf(lambda c: c.drawString(72, 700, "x")), pdf_reflow="false"
    ).markdown


# -- merged-cell scan ---------------------------------------------------------------


def test_merge_scan_handles_chunk_boundaries(monkeypatch) -> None:
    import markitforge.converters._xlsx_converter as xc

    refs = [f'<mergeCell ref="A{i}:C{i}"/>' for i in range(1, 400)]
    body = "<sheetData>" + "x" * 5000 + "</sheetData><mergeCells>" + "".join(refs)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("sheet.xml", body + "</mergeCells>")
    monkeypatch.setattr(xc, "_MERGE_SCAN_CHUNK", 97)  # force many boundaries
    with zipfile.ZipFile(data) as z:
        ranges = _scan_merge_cells(z, z.getinfo("sheet.xml"), ArchiveBudget())
    assert ranges == [(i - 1, 0, i - 1, 2) for i in range(1, 400)]
