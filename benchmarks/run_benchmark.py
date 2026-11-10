# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MarkitForge quality benchmark.

Builds a fixed set of tricky documents (deterministically, from code), converts
them, and scores the Markdown against known ground truth:

  text      share of expected words found in the output
  headings  share of expected headings emitted as Markdown headings (# ...)
  tables    share of expected table cells found inside Markdown/HTML table rows
  structure 1 when the number of tables is exactly right (split/merged tables fail)
  order     share of paragraph pairs that come out in reading order
  flow      share of phrases that sit on one line (wrapped lines joined, hyphens undone)
  clean     share of junk strings absent ("NaN", "Unnamed:", running headers/footers)

Usage:
  python benchmarks/run_benchmark.py                   # print the score table
  python benchmarks/run_benchmark.py --json            # machine-readable
  python benchmarks/run_benchmark.py --check           # fail if below baseline
  python benchmarks/run_benchmark.py --update-baseline # accept current scores

Needs: markitforge[all], reportlab, python-docx, openpyxl.
"""

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"
TOLERANCE = 0.02


@dataclass
class Case:
    name: str
    build: Callable[[Path], Path]
    words: List[str] = field(default_factory=list)
    headings: List[str] = field(default_factory=list)
    cells: List[str] = field(default_factory=list)
    order: List[str] = field(default_factory=list)
    absent: List[str] = field(default_factory=list)  # must not appear (furniture)
    single_line: List[str] = field(default_factory=list)  # must sit on one line
    table_count: Optional[int] = None  # exact number of tables expected
    options: Dict = field(default_factory=dict)


# ----------------------------------------------------------------- fixtures --

REPORT_PARAS = [
    "Quarterly revenue grew steadily across every region this year.",
    "Operating costs fell because the new warehouse opened in March.",
    "The board approved a second dividend payment for shareholders.",
]
TABLE = [
    ["Region", "Q1", "Q2", "Q3"],
    ["North", "1200", "1350", "1490"],
    ["South", "980", "1010", "1105"],
    ["West", "760", "845", "910"],
]


def build_pdf_report(d: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    path = d / "report.pdf"
    ss = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, title="Annual Report", author="Bench Co"
    )
    t = Table(TABLE)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    story = [
        Paragraph("Annual Report", ss["Title"]),
        Paragraph("Financial Summary", ss["Heading1"]),
        Paragraph(REPORT_PARAS[0], ss["BodyText"]),
        Paragraph(REPORT_PARAS[1], ss["BodyText"]),
        Spacer(1, 12),
        t,
        PageBreak(),
        Paragraph("Outlook", ss["Heading1"]),
        Paragraph(REPORT_PARAS[2], ss["BodyText"]),
    ]
    doc.build(story)
    return path


def build_pdf_two_column(d: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        PageTemplate,
        Paragraph,
    )

    path = d / "two_column.pdf"
    ss = getSampleStyleSheet()
    w, h = A4
    frames = [
        Frame(40, 40, (w - 100) / 2, h - 80, id="left"),
        Frame(60 + (w - 100) / 2, 40, (w - 100) / 2, h - 80, id="right"),
    ]
    doc = BaseDocTemplate(str(path), pagesize=A4)
    doc.addPageTemplates([PageTemplate(frames=frames)])
    filler = " ".join(["Context words keep this column long enough."] * 30)
    story = [
        Paragraph("Column Study", ss["Heading1"]),
        Paragraph("Alpha paragraph opens the left column. " + filler, ss["BodyText"]),
        Paragraph("Omega paragraph closes the article. " + filler, ss["BodyText"]),
    ]
    doc.build(story)
    return path


def build_docx_merged(d: Path) -> Path:
    import docx

    path = d / "merged.docx"
    doc = docx.Document()
    doc.core_properties.author = "Bench Co"
    doc.add_heading("Project Plan", level=1)
    doc.add_paragraph(REPORT_PARAS[0])
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Phase"
    table.cell(0, 2).text = "Owner"
    for r, row in enumerate([["Design", "Draft", "Ana"], ["Build", "Code", "Ben"]], 1):
        for c, val in enumerate(row):
            table.cell(r, c).text = val
    doc.add_heading("Risks", level=2)
    doc.add_paragraph(REPORT_PARAS[1])
    doc.save(path)
    return path


def build_xlsx_messy(d: Path) -> Path:
    import openpyxl

    path = d / "messy.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Item", "Units", "Price"])
    ws.append(["Lamp", 4, None])
    ws.append(["Desk", None, 120])
    ws.append([None, 9, 15])
    hidden = wb.create_sheet("Secret")
    hidden.append(["internal", "do-not-ship"])
    hidden.sheet_state = "hidden"
    wb.save(path)
    return path


MIXED = {
    "abstract": "Abstract text spans the full page width above both columns.",
    "left1": "First left paragraph starts the body of the article.",
    "left2": "Second left paragraph continues the first column downwards.",
    "right1": "First right paragraph begins the second column of text.",
    "right2": "Second right paragraph ends the article on this page.",
}


def build_pdf_mixed_layout(d: Path) -> Path:
    """Full-width title and abstract above a two-column body."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        FrameBreak,
        PageTemplate,
        Paragraph,
    )

    path = d / "mixed_layout.pdf"
    ss = getSampleStyleSheet()
    w, h = A4
    colw = (w - 100) / 2
    frames = [
        Frame(40, h - 200, w - 80, 160, id="top"),
        Frame(40, 40, colw, h - 250, id="left"),
        Frame(60 + colw, 40, colw, h - 250, id="right"),
    ]
    doc = BaseDocTemplate(str(path), pagesize=A4)
    doc.addPageTemplates([PageTemplate(frames=frames)])
    pad = " ".join(["Filler sentence keeps the block tall."] * 14)
    story = [
        Paragraph("Layout Study", ss["Title"]),
        Paragraph(MIXED["abstract"] + " " + pad[:300], ss["BodyText"]),
        FrameBreak(),
        Paragraph("Methods", ss["Heading2"]),
        Paragraph(MIXED["left1"] + " " + pad, ss["BodyText"]),
        Paragraph(MIXED["left2"] + " " + pad, ss["BodyText"]),
        FrameBreak(),
        Paragraph("Results", ss["Heading2"]),
        Paragraph(MIXED["right1"] + " " + pad, ss["BodyText"]),
        Paragraph(MIXED["right2"] + " " + pad, ss["BodyText"]),
    ]
    doc.build(story)
    return path


FURNITURE_PARAS = [
    f"Section {i} body text describes topic number {i} plainly." for i in range(1, 7)
]


def build_pdf_furniture(d: Path) -> Path:
    """Six pages, each with a running header, footer and page number."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    path = d / "furniture.pdf"
    ss = getSampleStyleSheet()

    def decorate(canvas, doc):
        w, h = A4
        canvas.setFont("Helvetica", 9)
        canvas.drawString(72, h - 40, "Acme Internal Handbook")
        canvas.drawRightString(w - 72, h - 40, "Draft 2026")
        canvas.drawCentredString(w / 2, 30, f"Page {doc.page} of 6")

    story = []
    for i, para in enumerate(FURNITURE_PARAS):
        story += [Paragraph(para, ss["BodyText"])]
        if i < len(FURNITURE_PARAS) - 1:
            story.append(PageBreak())
    SimpleDocTemplate(str(path), pagesize=A4).build(
        story, onFirstPage=decorate, onLaterPages=decorate
    )
    return path


REFLOW_LINES = [
    "The documentation explains every configu-",
    "ration option in detail, and each chapter",
    "ends with a short list of worked examples",
    "that readers can copy.",
    "",
    "A second paragraph covers the state-of-the-art",
    "methods used for long-term storage.",
]


def build_pdf_reflow(d: Path) -> Path:
    """Hard-wrapped lines with an end-of-line hyphen, drawn line by line."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path = d / "reflow.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 11)
    y = 760
    for line in REFLOW_LINES:
        if line:
            c.drawString(72, y, line)
            y -= 14
        else:
            y -= 12
    c.save()
    return path


BORDERLESS = [
    ["Product", "Units", "Revenue"],
    ["Anvil", "12", "1,440.00"],
    ["Hammer", "140", "2,100.50"],
    ["Tongs", "7", "91.00"],
    ["Bellows", "3", "780.25"],
]


def build_pdf_borderless(d: Path) -> Path:
    """A table with no ruling lines and right-aligned numbers."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path = d / "borderless.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 11)
    c.drawString(72, 770, "Sales by product for the last quarter are listed below.")
    y = 740
    for row in BORDERLESS:
        c.drawString(72, y, row[0])
        c.drawRightString(300, y, row[1])
        c.drawRightString(430, y, row[2])
        y -= 18
    c.drawString(72, y - 10, "Figures exclude tax and shipping.")
    c.save()
    return path


def build_pdf_long_table(d: Path) -> Path:
    """A ruled table that continues on a second page with a repeated header."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    path = d / "long_table.pdf"
    rows = [["Code", "City", "Count"]] + [
        [f"C{i:03d}", f"City{i}", str(100 + i)] for i in range(1, 61)
    ]
    t = Table(rows, repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(str(path), pagesize=A4).build([t])
    return path


def build_xlsx_offset(d: Path) -> Path:
    """Title row, header on row 3, and a second table further down."""
    import openpyxl

    path = d / "offset.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report"
    ws["A1"] = "Regional sales summary"
    for r, row in enumerate(
        [["Region", "Sales", "Target"], ["North", 120, 100], ["South", 95, 110]], 3
    ):
        for c, v in enumerate(row, 1):
            ws.cell(r, c, v)
    for r, row in enumerate(
        [["Staff", "Role"], ["Ana", "Lead"], ["Ben", "Analyst"]], 9
    ):
        for c, v in enumerate(row, 1):
            ws.cell(r, c, v)
    wb.save(path)
    return path


CASES = [
    Case(
        "pdf_report",
        build_pdf_report,
        words=" ".join(REPORT_PARAS).split(),
        headings=["Annual Report", "Financial Summary", "Outlook"],
        cells=[c for row in TABLE for c in row],
        order=REPORT_PARAS,
    ),
    Case(
        "pdf_two_column",
        build_pdf_two_column,
        words="Alpha paragraph opens the left column Omega paragraph closes the article".split(),
        headings=["Column Study"],
        order=["Alpha paragraph", "Omega paragraph"],
    ),
    Case(
        "docx_merged_cells",
        build_docx_merged,
        words=" ".join(REPORT_PARAS[:2]).split(),
        headings=["Project Plan", "Risks"],
        cells=["Phase", "Owner", "Design", "Draft", "Ana", "Build", "Code", "Ben"],
        order=REPORT_PARAS[:2],
    ),
    Case(
        "pdf_mixed_layout",
        build_pdf_mixed_layout,
        words=" ".join(MIXED.values()).split(),
        headings=["Layout Study", "Methods", "Results"],
        order=list(MIXED.values()),
    ),
    Case(
        "pdf_headers_footers",
        build_pdf_furniture,
        words=" ".join(FURNITURE_PARAS).split(),
        order=FURNITURE_PARAS,
        absent=["Acme Internal Handbook", "Draft 2026", "Page 3 of 6"],
    ),
    Case(
        "pdf_reflow",
        build_pdf_reflow,
        words="documentation explains chapter worked examples storage".split(),
        single_line=[
            "explains every configuration option in detail",
            "worked examples that readers can copy",
            "state-of-the-art methods used for long-term storage",
        ],
    ),
    Case(
        "pdf_borderless_table",
        build_pdf_borderless,
        words="Sales by product Figures exclude tax".split(),
        cells=[c for row in BORDERLESS for c in row],
        table_count=1,
    ),
    Case(
        "pdf_multipage_table",
        build_pdf_long_table,
        cells=["Code", "City", "Count", "C001", "City30", "C060", "160"],
        table_count=1,
    ),
    Case(
        "xlsx_offset_header",
        build_xlsx_offset,
        words=["Regional", "North", "Ana", "Analyst"],
        cells=[
            "Region",
            "Sales",
            "Target",
            "North",
            "120",
            "South",
            "110",
            "Staff",
            "Role",
            "Ben",
        ],
        table_count=2,
    ),
    Case(
        "xlsx_blanks_hidden",
        build_xlsx_messy,
        words=["Lamp", "Desk", "Units", "Price"],
        headings=["Sales"],
        cells=["Item", "Units", "Price", "Lamp", "4", "Desk", "120", "9", "15"],
    ),
]

# ------------------------------------------------------------------ scoring --

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _table_text(md: str) -> str:
    rows = [ln for ln in md.splitlines() if _TABLE_ROW.match(ln)]
    html_cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", md, flags=re.S | re.I)
    return " | ".join(rows + html_cells)


def score(case: Case, md: str) -> Dict[str, float]:
    low = _norm(md)
    words = [w.strip(".,").lower() for w in case.words]
    out: Dict[str, float] = {}
    out["text"] = sum(1 for w in words if w in low) / len(words) if words else 1.0

    if case.headings:
        heads = [
            _norm(re.sub(r"^#+\s*", "", ln))
            for ln in md.splitlines()
            if re.match(r"^#{1,6}\s+\S", ln)
        ]
        out["headings"] = sum(
            1 for h in case.headings if any(_norm(h) == x for x in heads)
        ) / len(case.headings)

    if case.cells:
        tt = _norm(_table_text(md))
        cell_hits = 0
        for c in case.cells:
            if re.search(r"(^|[\s|>])" + re.escape(c.lower()) + r"($|[\s|<])", tt):
                cell_hits += 1
        out["tables"] = cell_hits / len(case.cells)

    if case.order:
        # Share of paragraph pairs that come out in the right relative order
        # (a missing paragraph counts as wrong for every pair it is in).
        pos = [low.find(_norm(p)[:40]) for p in case.order]
        pairs = [(i, j) for i in range(len(pos)) for j in range(i + 1, len(pos))]
        good = sum(1 for i, j in pairs if 0 <= pos[i] < pos[j])
        out["order"] = good / len(pairs) if pairs else 1.0

    if case.single_line:
        lines = [_norm(ln) for ln in md.splitlines()]
        out["flow"] = sum(
            1 for ph in case.single_line if any(_norm(ph) in ln for ln in lines)
        ) / len(case.single_line)

    if case.table_count is not None:
        found = _count_tables(md)
        out["structure"] = 1.0 if found == case.table_count else 0.0

    junk = ["nan", "unnamed:", "do-not-ship"] + [a.lower() for a in case.absent]
    hits = sum(1 for j in junk if re.search(rf"(?<!\w){re.escape(j)}(?!\w)", low))
    out["clean"] = 1.0 - hits / len(junk)
    return out


def _count_tables(md: str) -> int:
    """Markdown tables (runs of pipe rows) plus HTML tables."""
    count, inside = 0, False
    for ln in md.splitlines():
        row = bool(_TABLE_ROW.match(ln))
        if row and not inside:
            count += 1
        inside = row
    return count + len(re.findall(r"<table\b", md, flags=re.I))


# ------------------------------------------------------------------- runner --


def run(
    configs: Dict[str, Dict], engine: str = "markitforge"
) -> Dict[str, Dict[str, Dict[str, float]]]:
    if engine == "markitdown":  # upstream, for before/after comparisons
        from markitdown import MarkItDown as Engine
    else:
        from markitforge import MarkitForge as Engine

    md = Engine()
    results: Dict[str, Dict[str, Dict[str, float]]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for case in CASES:
            path = case.build(d)
            for cfg_name, opts in configs.items():
                if cfg_name != "default" and not path.suffix == ".pdf":
                    continue  # backend configs only differ for PDF
                try:
                    text = md.convert(str(path), **{**case.options, **opts}).markdown
                    s = score(case, text)
                except Exception as exc:  # a crash scores zero
                    s = {"error": 0.0}
                    print(f"  {case.name}/{cfg_name}: {exc}", file=sys.stderr)
                results.setdefault(case.name, {})[cfg_name] = s
    return results


def overall(s: Dict[str, float]) -> float:
    return round(sum(s.values()) / len(s), 3) if s else 0.0


def print_table(results) -> None:
    metrics = ["text", "headings", "tables", "structure", "order", "flow", "clean"]
    print(
        f"{'case':<22}{'config':<12}"
        + "".join(f"{m:>10}" for m in metrics)
        + f"{'overall':>10}"
    )
    for case, by_cfg in results.items():
        for cfg, s in by_cfg.items():
            cells = "".join(
                f"{s[m]:>10.2f}" if m in s else f"{'-':>10}" for m in metrics
            )
            print(f"{case:<22}{cfg:<12}{cells}{overall(s):>10.2f}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument(
        "--engine",
        choices=["markitforge", "markitdown"],
        default="markitforge",
        help="markitdown = score upstream MarkItDown (if installed) for comparison",
    )
    args = ap.parse_args(argv)

    configs = {"default": {}}
    if args.engine == "markitforge":
        configs["pdfminer"] = {"pdf_backend": "pdfminer"}
    results = run(configs, args.engine)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results)

    flat = {
        f"{c}/{cfg}": overall(s) for c, by in results.items() for cfg, s in by.items()
    }
    if args.update_baseline:
        BASELINE.write_text(json.dumps(flat, indent=2, sort_keys=True) + "\n")
        print(f"\nBaseline written to {BASELINE}")
        return 0
    if args.check:
        if not BASELINE.exists():
            print("No baseline yet. Run with --update-baseline.", file=sys.stderr)
            return 1
        base = json.loads(BASELINE.read_text())
        bad = [
            f"{k}: {flat.get(k, 0):.3f} < baseline {v:.3f}"
            for k, v in base.items()
            if flat.get(k, 0.0) + TOLERANCE < v
        ]
        if bad:
            print("\nQuality regressions:\n  " + "\n  ".join(bad), file=sys.stderr)
            return 1
        print("\nNo regressions against baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
