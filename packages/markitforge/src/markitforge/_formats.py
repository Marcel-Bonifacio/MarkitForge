# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The list of supported formats, with a live check of optional dependencies.

Used by `markitforge --list-formats`, the MCP `list_supported_formats` tool,
the GUI, and `scripts/gen_formats_doc.py` (which writes docs/FORMATS.md).
"""

import importlib.util
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class FormatInfo:
    name: str
    extensions: List[str]
    extra: Optional[str]  # pip extra, e.g. markitforge[pdf]
    modules: List[str] = field(default_factory=list)  # import names to check
    tools: List[str] = field(default_factory=list)  # external programs to check
    notes: str = ""


FORMATS: List[FormatInfo] = [
    FormatInfo(
        "PDF",
        [".pdf"],
        "pdf",
        ["pdfminer", "pdfplumber"],
        notes="Tables, headings, page markers. --pdf-backend docling for hard layouts.",
    ),
    FormatInfo(
        "Word",
        [".docx"],
        "docx",
        ["mammoth", "lxml"],
        notes="Math to LaTeX, headers/footers, page markers.",
    ),
    FormatInfo(
        "PowerPoint",
        [".pptx"],
        "pptx",
        ["pptx"],
        notes="Per-slide sections, notes, charts, tables.",
    ),
    FormatInfo(
        "Excel",
        [".xlsx"],
        "xlsx",
        ["pandas", "openpyxl"],
        notes="Per-sheet sections; hidden sheets skipped by default.",
    ),
    FormatInfo("Excel 97-2003", [".xls"], "xls", ["pandas", "xlrd"]),
    FormatInfo(
        "Legacy & OpenDocument",
        [
            ".doc",
            ".rtf",
            ".odt",
            ".ott",
            ".fodt",
            ".wpd",
            ".ppt",
            ".pps",
            ".odp",
            ".ods",
            ".xlsb",
        ],
        "docx",
        ["mammoth"],
        ["soffice"],
        "Converted through LibreOffice (must be installed), sandboxed.",
    ),
    FormatInfo(
        "Apple iWork",
        [".pages", ".numbers", ".key"],
        "docx",
        ["mammoth"],
        ["soffice"],
        "Through LibreOffice; falls back to the file's built-in preview.",
    ),
    FormatInfo("Outlook message", [".msg"], "outlook", ["olefile"]),
    FormatInfo(
        "Email",
        [".eml"],
        None,
        notes="Headers, body and attachment list; convert_attachments=True converts them too.",
    ),
    FormatInfo("EPUB", [".epub"], None),
    FormatInfo(
        "HTML",
        [".html", ".htm"],
        None,
        notes="Wikipedia, Bing SERP and RSS pages get special handling.",
    ),
    FormatInfo(
        "MHTML web archive",
        [".mht", ".mhtml"],
        None,
        notes="Also OneNote / Word 'single file web page' exports.",
    ),
    FormatInfo("CSV / TSV", [".csv", ".tsv"], None),
    FormatInfo("JSON / XML / text", [".json", ".jsonl", ".xml", ".txt", ".md"], None),
    FormatInfo("Jupyter notebook", [".ipynb"], None),
    FormatInfo("RSS / Atom", [".rss", ".atom"], None),
    FormatInfo("ZIP archive", [".zip"], None, notes="Each file inside is converted."),
    FormatInfo(
        "Images",
        [".jpg", ".jpeg", ".png"],
        None,
        tools=["exiftool"],
        notes="EXIF metadata; descriptions with an LLM client.",
    ),
    FormatInfo(
        "Audio",
        [".wav", ".mp3", ".m4a", ".mp4"],
        "audio-transcription",
        ["pydub", "speech_recognition"],
        ["exiftool"],
        "Metadata plus speech transcript.",
    ),
    FormatInfo(
        "YouTube URL",
        ["https://youtube.com/..."],
        "youtube-transcription",
        ["youtube_transcript_api"],
    ),
    FormatInfo(
        "Azure Document Intelligence",
        ["(cloud)"],
        "az-doc-intel",
        ["azure.ai.documentintelligence"],
        notes="Use -d / --use-docintel.",
    ),
]


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _has_tool(name: str) -> bool:
    if name == "soffice":
        return bool(
            os.environ.get("MARKITFORGE_SOFFICE")
            or shutil.which("soffice")
            or shutil.which("libreoffice")
            or os.path.exists("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        )
    return shutil.which(name) is not None


def format_status() -> List[Dict]:
    """Each format with `available` (required parts present) and `missing` parts."""
    rows = []
    for f in FORMATS:
        missing = [m for m in f.modules if not _has_module(m)]
        missing_tools = [t for t in f.tools if not _has_tool(t)]
        # exiftool only adds metadata; it is not required
        required_tools = [t for t in missing_tools if t != "exiftool"]
        row = asdict(f)
        row["available"] = not missing and not required_tools
        row["missing"] = missing + missing_tools
        row["install"] = (
            f"pip install 'markitforge[{f.extra}]'" if (missing and f.extra) else ""
        )
        rows.append(row)
    return rows


def format_table(as_json: bool = False) -> str:
    rows = format_status()
    if as_json:
        return json.dumps(rows, indent=2)
    lines = ["Supported formats (✓ ready, ✗ needs install):", ""]
    for r in rows:
        mark = "✓" if r["available"] else "✗"
        lines.append(f"  {mark} {r['name']:<28} {' '.join(r['extensions'])}")
        if r["missing"]:
            hint = r["install"] or ""
            if "soffice" in r["missing"]:
                hint = (hint + "; " if hint else "") + "install LibreOffice"
            lines.append(f"      missing: {', '.join(r['missing'])}  {hint}".rstrip())
    return "\n".join(lines)


def format_markdown() -> str:
    """docs/FORMATS.md content (no live status, so it is stable in git)."""
    out = [
        "# Supported formats",
        "",
        "Generated from `markitforge/_formats.py` by `scripts/gen_formats_doc.py`. "
        "Run `markitforge --list-formats` to see what works on your machine.",
        "",
        "| Format | Extensions | Install | Needs | Notes |",
        "|---|---|---|---|---|",
    ]
    for f in FORMATS:
        extra = f"`markitforge[{f.extra}]`" if f.extra else "core"
        needs = ", ".join(f.tools) if f.tools else ""
        out.append(
            f"| {f.name} | {' '.join(f'`{e}`' for e in f.extensions)} | {extra} | {needs} | {f.notes} |"
        )
    return "\n".join(out) + "\n"
