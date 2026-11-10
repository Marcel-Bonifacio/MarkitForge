# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MarkitForge quality benchmark.

Builds a fixed set of tricky documents (deterministically, from code), converts
them, and scores the Markdown against known ground truth:

  text      share of expected words found in the output
  headings  share of expected headings emitted as Markdown headings (# ...)
  tables    share of expected table cells found inside Markdown/HTML table rows
  order     whether key paragraphs appear in reading order (1 or 0)
  clean     1 when no junk such as "NaN" or "Unnamed: 0" appears

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
        pos = [low.find(_norm(p)[:40]) for p in case.order]
        out["order"] = 1.0 if all(p >= 0 for p in pos) and pos == sorted(pos) else 0.0

    junk = ["nan", "unnamed: 0", "do-not-ship"]
    out["clean"] = 0.0 if any(re.search(rf"\b{j}\b", low) for j in junk) else 1.0
    return out


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
    metrics = ["text", "headings", "tables", "order", "clean"]
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
