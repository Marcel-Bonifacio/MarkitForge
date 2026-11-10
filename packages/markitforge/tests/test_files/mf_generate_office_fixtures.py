# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regenerate the mf_* office fixtures used by tests/test_office_markitforge.py.

Run from packages/markitforge:  python tests/test_files/mf_generate_office_fixtures.py
Needs openpyxl and python-docx; the .odt/.rtf/.ods/.odp/.xls files also need
LibreOffice (soffice) on PATH or in MARKITFORGE_SOFFICE.
"""

import base64
import datetime
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def build_xlsx(path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(["Item", "Units", "Price"])
    ws.append(["Lamp", 4, None])
    ws.append(["Desk", None, 120])
    ws.append([None, 9, 15])
    ws.append(["Chair", 2, 4.5])

    secret = wb.create_sheet("Secret")
    secret.append(["Code"])
    secret.append(["mf-hidden-sheet-text"])
    secret.sheet_state = "hidden"

    very = wb.create_sheet("Deep")
    very.append(["Code"])
    very.append(["mf-very-hidden-text"])
    very.sheet_state = "veryHidden"

    notes = wb.create_sheet("Notes")
    notes.append(["Note"])
    notes.append(["mf-visible-notes"])

    wb.properties.creator = "MF Tester"
    wb.properties.title = "MF Workbook"
    wb.properties.created = datetime.datetime(2024, 1, 2, 3, 4, 5)
    wb.properties.modified = datetime.datetime(2024, 2, 3, 4, 5, 6)
    wb.save(path)


def build_docx(path: Path) -> None:
    import docx
    from docx.enum.text import WD_BREAK

    d = docx.Document()
    d.core_properties.title = "MF Pages"
    d.core_properties.author = "MF Author"
    section = d.sections[0]
    section.header.paragraphs[0].text = "MF Header Text"
    section.footer.paragraphs[0].text = "MF Footer Text"

    d.add_heading("First page heading", 1)
    d.add_paragraph("Text on the first page.")
    d.add_page_break()
    d.add_heading("Second page heading", 1)
    p = d.add_paragraph("Text on the second page.")
    p.add_run().add_break(WD_BREAK.PAGE)
    d.add_paragraph("Text on the third page.")

    table = d.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    header = table.rows[0].cells
    header[0].merge(header[1]).text = "Merged header"
    header[2].text = "Solo"
    table.cell(1, 0).merge(table.cell(2, 0)).text = "Tall cell"
    table.cell(1, 1).text = "b1"
    table.cell(1, 2).text = "c1"
    table.cell(2, 1).text = "b2"
    table.cell(2, 2).text = "c2"

    image_path = path.with_suffix(".png")
    image_path.write_bytes(PNG)
    try:
        d.add_picture(str(image_path))
    finally:
        image_path.unlink()
    d.save(path)


def build_pptx(path: Path) -> None:
    import pptx

    deck = pptx.Presentation()
    deck.core_properties.title = "MF Deck"
    deck.core_properties.author = "MF Presenter"
    for n in (1, 2):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = f"MF slide {n} title"
        slide.placeholders[1].text = f"MF slide {n} body"
    deck.save(path)


def build_text_files() -> None:
    (HERE / "mf_table.tsv").write_text(
        "name\tcity\tscore\nAda\tLondon\t10\nLinus\tHelsinki\t9\n", encoding="utf-8"
    )
    (HERE / "mf_semicolon.csv").write_text(
        "name;city;score\nAda;London;10\nLinus;Helsinki;9\n", encoding="utf-8"
    )


def build_mht(path: Path) -> None:
    png_b64 = base64.b64encode(PNG).decode("ascii")
    html = (
        "<html><head><title>MF Archive Page</title></head><body>"
        "<h1>MF Archive Heading</h1>"
        "<p>Caf=C3=A9 text with a =\r\nsoft line break.</p>"
        '<p><img src=3D"cid:logo@mf" alt=3D"cid image"></p>'
        '<p><img src=3D"page_files/pic.png" alt=3D"located image"></p>'
        "<table><tr><th colspan=3D2>Span</th></tr><tr><td>a</td><td>b</td></tr></table>"
        "</body></html>"
    )
    body = "\r\n".join(
        [
            "From: <Saved by MF>",
            "Subject: MF Archive Subject",
            "MIME-Version: 1.0",
            'Content-Type: multipart/related; boundary="----=_MF_BOUNDARY"; type="text/html"',
            "",
            "------=_MF_BOUNDARY",
            'Content-Type: text/html; charset="utf-8"',
            "Content-Transfer-Encoding: quoted-printable",
            "Content-Location: file:///C:/docs/page.htm",
            "",
            html,
            "",
            "------=_MF_BOUNDARY",
            "Content-Type: image/png",
            "Content-Transfer-Encoding: base64",
            "Content-ID: <logo@mf>",
            "",
            png_b64,
            "",
            "------=_MF_BOUNDARY",
            "Content-Type: image/png",
            "Content-Transfer-Encoding: base64",
            "Content-Location: file:///C:/docs/page_files/pic.png",
            "",
            png_b64,
            "",
            "------=_MF_BOUNDARY--",
            "",
        ]
    )
    path.write_bytes(body.encode("ascii"))


def soffice_convert(src: Path, target: str, dest: Path) -> None:
    soffice = os.environ.get("MARKITFORGE_SOFFICE") or shutil.which("soffice")
    if not soffice:
        print(f"skipping {dest.name}: soffice not found")
        return
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp, "profile").as_uri()
        subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile}",
                "--headless",
                "--convert-to",
                target,
                "--outdir",
                tmp,
                str(src),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        produced = Path(tmp, src.stem + "." + target.split(":")[0])
        shutil.move(str(produced), dest)


def main() -> None:
    build_xlsx(HERE / "mf_sheets.xlsx")
    build_docx(HERE / "mf_pages.docx")
    build_text_files()
    build_mht(HERE / "mf_page.mht")
    soffice_convert(HERE / "mf_pages.docx", "odt", HERE / "mf_pages.odt")
    soffice_convert(HERE / "mf_pages.docx", "rtf", HERE / "mf_pages.rtf")
    soffice_convert(HERE / "mf_sheets.xlsx", "ods", HERE / "mf_sheets.ods")
    soffice_convert(HERE / "mf_sheets.xlsx", "xls:MS Excel 97", HERE / "mf_sheets.xls")
    soffice_convert(HERE / "mf_pages.docx", "doc:MS Word 97", HERE / "mf_pages.doc")
    with tempfile.TemporaryDirectory() as tmp:
        deck = Path(tmp, "mf_deck.pptx")
        build_pptx(deck)
        soffice_convert(deck, "odp", HERE / "mf_deck.odp")


if __name__ == "__main__":
    main()
