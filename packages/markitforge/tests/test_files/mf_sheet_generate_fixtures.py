# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regenerate the mf_sheet_* fixtures used by tests/test_sheet_algos.py.

Run from packages/markitforge:  python tests/test_files/mf_sheet_generate_fixtures.py
Needs openpyxl; mf_sheet_layouts.xls also needs LibreOffice (soffice) on PATH
or in MARKITFORGE_SOFFICE.
"""

import datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence

from mf_generate_office_fixtures import soffice_convert

HERE = Path(__file__).resolve().parent

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
HALVES: List[List[Any]] = [
    ["Region", "H1", None, "H2", None],
    ["North", 1, 2, 3, 4],
    ["South", 5, 6, 7, 8],
    [None, 9, 10, 11, 12],
]


def put(ws: Any, top: int, left: int, rows: Sequence[Sequence[Optional[Any]]]) -> None:
    """Write ``rows`` with their first cell at 1-based (top, left)."""
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            if value is not None:
                ws.cell(top + i, left + j, value)


def build_layouts(path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plain"
    put(ws, 1, 1, PARTS)

    ws = wb.create_sheet("Side")
    put(ws, 1, 1, PARTS)
    put(ws, 1, 6, CITIES)

    ws = wb.create_sheet("Stacked")
    put(ws, 1, 1, PARTS)
    put(ws, 7, 1, CITIES)

    ws = wb.create_sheet("Titled")
    put(ws, 2, 2, [["Stock report"]])
    put(ws, 4, 2, PARTS)

    ws = wb.create_sheet("Bare")
    put(ws, 3, 3, [[1, 2, 3], [4, 5.5, 6]])

    ws = wb.create_sheet("Merged")
    put(ws, 2, 2, HALVES)
    ws.merge_cells("C2:D2")
    ws.merge_cells("E2:F2")
    ws.merge_cells("B4:B5")

    wb.save(path)


def main() -> None:
    xlsx = HERE / "mf_sheet_layouts.xlsx"
    build_layouts(xlsx)
    soffice_convert(xlsx, "xls:MS Excel 97", HERE / "mf_sheet_layouts.xls")
    print("fixtures written to", HERE)


if __name__ == "__main__":
    main()
