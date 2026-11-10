# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MarkitForge office improvements: sheets, slides, pages, table_format, TSV,
MHTML and LibreOffice-backed formats.

Fixtures are built by tests/test_files/mf_generate_office_fixtures.py.
"""

import io
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pandas as pd
import pytest

from markitforge import (
    MarkitForge,
    MissingDependencyException,
    FileConversionException,
    StreamInfo,
)
from markitforge.converters import (
    CsvConverter,
    DocxConverter,
    HtmlConverter,
    LibreOfficeConverter,
    MhtmlConverter,
    PptxConverter,
    XlsConverter,
    XlsxConverter,
)
from markitforge.converters import _docx_converter, _libreoffice_converter
from markitforge.converters._xlsx_converter import _frame_to_html
from markitforge.converter_utils.docx.page_breaks import split_pages

FILES = Path(__file__).parent / "test_files"
HAS_SOFFICE = _libreoffice_converter.find_soffice() is not None
needs_soffice = pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")


def _convert(converter: Any, name: str, **kwargs: Any) -> Any:
    path = FILES / name
    with open(path, "rb") as fh:
        return converter.convert(
            fh, StreamInfo(extension=path.suffix, filename=path.name), **kwargs
        )


class _FakeLLM:
    """Minimal stand-in for an OpenAI-style client."""

    def __init__(self, caption: str = "A tiny [test] picture\nof nothing") -> None:
        self.calls: List[Any] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._caption = caption

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._caption)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


# ---------------------------------------------------------------- XLSX / XLS


@pytest.mark.parametrize(
    "name,converter",
    [("mf_sheets.xlsx", XlsxConverter()), ("mf_sheets.xls", XlsConverter())],
)
def test_sheets_skip_hidden_and_render_blanks(name: str, converter: Any) -> None:
    result = _convert(converter, name)
    md = result.markdown

    assert "nan" not in md.lower() and "NaT" not in md
    assert "| Lamp | 4 |  |" in md
    assert "| Desk |  | 120 |" in md
    assert "|  | 9 | 15 |" in md
    assert "| Chair | 2 | 4.5 |" in md
    assert "4.0" not in md and "120.0" not in md

    assert "mf-hidden-sheet-text" not in md
    assert "mf-very-hidden-text" not in md
    assert "mf-visible-notes" in md
    assert result.metadata["sheet_names"] == ["Inventory", "Secret", "Deep", "Notes"]
    assert result.metadata["hidden_sheets"] == ["Secret", "Deep"]
    assert [(p.index, p.kind, p.title) for p in result.pages] == [
        (1, "sheet", "Inventory"),
        (2, "sheet", "Notes"),
    ]
    assert result.warnings and "include_hidden_sheets" in result.warnings[0]
    assert "<!-- sheet" not in md


@pytest.mark.parametrize(
    "name,converter",
    [("mf_sheets.xlsx", XlsxConverter()), ("mf_sheets.xls", XlsConverter())],
)
def test_sheets_include_hidden_with_markers(name: str, converter: Any) -> None:
    result = _convert(converter, name, include_hidden_sheets=True, page_markers=True)
    md = result.markdown

    assert "mf-hidden-sheet-text" in md and "mf-very-hidden-text" in md
    assert [p.title for p in result.pages] == ["Inventory", "Secret", "Deep", "Notes"]
    assert md.startswith("<!-- sheet 1 -->\n## Inventory")
    assert "<!-- sheet 4 -->\n## Notes" in md
    assert result.warnings == []


def test_xlsx_core_properties() -> None:
    result = _convert(XlsxConverter(), "mf_sheets.xlsx")
    assert result.metadata["author"] == "MF Tester"
    assert result.metadata["created"].startswith("2024-01-02T03:04:05")
    assert "modified" in result.metadata
    assert result.title == "MF Workbook"


def test_frame_formatting_whole_numbers_and_missing_values() -> None:
    frame = pd.DataFrame(
        {
            "Item": ["Lamp", "Desk", None],
            "Units": [4, None, 9],
            "Price": [None, 120, 15.5],
            "Ratio": [0.1 + 0.2, 1.5, 2.0],
            "Mixed": ["a", 4.0, None],
            "When": [pd.Timestamp("2020-01-01"), pd.NaT, None],
        }
    )
    md = HtmlConverter().convert_string(_frame_to_html(frame)).markdown
    assert "| Lamp | 4 |  | 0.3 | a | 2020-01-01 |" in md
    assert "| Desk |  | 120 | 1.5 | 4 |  |" in md
    assert "|  | 9 | 15.5 | 2 |  |  |" in md
    for bad in ("nan", "NaT", "None", "<NA>", ".0 "):
        assert bad not in md


def test_existing_xlsx_output_unchanged_by_default() -> None:
    result = _convert(XlsxConverter(), "test.xlsx")
    assert result.markdown.startswith("## Sheet1\n| Alpha | Beta | Gamma | Delta |")
    assert "## 09060124-b5e7-4717-9d07-3c046eb\n" in result.markdown


# ---------------------------------------------------------------------- PPTX


def test_pptx_pages_and_metadata() -> None:
    result = _convert(PptxConverter(), "test.pptx")
    assert result.markdown.startswith("<!-- Slide number: 1 -->\n# AutoGen")
    assert "<!-- Slide number: 6 -->" in result.markdown
    assert result.metadata["slide_count"] == 6
    assert result.metadata["author"] == "Adam Fourney"
    assert "created" in result.metadata and "modified" in result.metadata
    assert len(result.pages) == 6
    assert result.pages[2].kind == "slide"
    assert result.pages[2].title == "A table to test parsing:"
    assert "| ColA |" in result.pages[2].markdown
    assert "Slide number" not in result.pages[2].markdown


def test_pptx_page_markers() -> None:
    plain = _convert(PptxConverter(), "test.pptx")
    marked = _convert(PptxConverter(), "test.pptx", page_markers=True)
    assert marked.markdown.startswith("<!-- slide 1 -->\n# AutoGen")
    assert "Slide number" not in marked.markdown
    # Only the marker form changes; slide content is identical.
    assert marked.markdown == plain.markdown.replace("Slide number: ", "slide ")


# ---------------------------------------------------------------------- DOCX


def test_docx_metadata_and_pages() -> None:
    result = _convert(DocxConverter(), "mf_pages.docx")
    assert result.title == "MF Pages"
    assert result.metadata["author"] == "MF Author"
    assert [p.index for p in result.pages] == [1, 2, 3]
    assert all(p.kind == "page" for p in result.pages)
    assert "First page heading" in result.pages[0].markdown
    assert "Second page heading" in result.pages[1].markdown
    assert "Text on the third page." in result.pages[2].markdown
    assert "mfpagebreak" not in result.markdown
    assert "<!-- page" not in result.markdown
    assert "MF Header Text" not in result.markdown


def test_docx_page_markers() -> None:
    result = _convert(DocxConverter(), "mf_pages.docx", page_markers=True)
    md = result.markdown
    assert md.startswith("<!-- page 1 -->\n# First page heading")
    assert "<!-- page 2 -->\n# Second page heading" in md
    assert "<!-- page 3 -->\nText on the third page." in md
    assert "mfpagebreak" not in md
    assert result.select_pages("2", markers=False).startswith("# Second page heading")


@pytest.mark.parametrize(
    "name",
    [
        "test.docx",
        "mf_pages.docx",
        "equations.docx",
        "rlink.docx",
        "test_with_comment.docx",
    ],
)
@pytest.mark.parametrize("table_format", ["markdown", "html"])
def test_docx_default_output_unchanged_by_page_tracking(
    monkeypatch: pytest.MonkeyPatch, name: str, table_format: str
) -> None:
    tracked = _convert(DocxConverter(), name, table_format=table_format)
    original = _docx_converter.pre_process_docx
    monkeypatch.setattr(
        _docx_converter,
        "pre_process_docx",
        lambda stream, page_break_sentinel=None, **kwargs: original(stream, **kwargs),
    )
    untracked = _convert(DocxConverter(), name, table_format=table_format)
    assert tracked.markdown == untracked.markdown


def test_docx_headers_footers() -> None:
    result = _convert(DocxConverter(), "mf_pages.docx", include_headers_footers=True)
    md = result.markdown
    assert "## Page headers\n\nMF Header Text" in md
    assert "## Page footers\n\nMF Footer Text" in md
    assert md.index("Text on the third page.") < md.index("MF Header Text")
    assert result.metadata["headers"] == ["MF Header Text"]
    assert result.metadata["footers"] == ["MF Footer Text"]


def test_docx_merged_cells_table_format() -> None:
    markdown = _convert(DocxConverter(), "mf_pages.docx").markdown
    assert "<table>" not in markdown
    auto = _convert(DocxConverter(), "mf_pages.docx", table_format="auto").markdown
    assert '<td colspan="2">Merged header</td>' in auto
    assert '<td rowspan="2">Tall cell</td>' in auto
    html = _convert(DocxConverter(), "mf_pages.docx", table_format="html").markdown
    assert '<td colspan="2">Merged header</td>' in html


def test_docx_describe_images_uses_caption_as_alt() -> None:
    client = _FakeLLM()
    result = _convert(
        DocxConverter(),
        "mf_pages.docx",
        describe_images=True,
        llm_client=client,
        llm_model="fake-model",
        llm_prompt="Describe it",
    )
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "fake-model"
    assert (
        "![A tiny test picture of nothing](data:image/png;base64...)" in result.markdown
    )
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.mime_type == "image/png"
    assert asset.data and asset.data.startswith(b"\x89PNG")
    assert asset.description == "A tiny test picture of nothing"


def test_docx_describe_images_without_client_collects_assets() -> None:
    result = _convert(DocxConverter(), "mf_pages.docx", describe_images=True)
    assert len(result.assets) == 1
    assert any("llm_client" in w for w in result.warnings)
    assert "![](data:image/png;base64...)" in result.markdown


def test_docx_llm_client_alone_does_not_caption() -> None:
    client = _FakeLLM()
    result = _convert(
        DocxConverter(), "mf_pages.docx", llm_client=client, llm_model="m"
    )
    assert client.calls == []
    assert result.assets == []


def test_split_pages_rules() -> None:
    s = "SENT"
    md = "# SENTHeading\n\nbody textSENT\n\n| aSENT | b |\n| c | d |\n\nafter\n\n- SENTitem"
    assert split_pages(md.replace("SENT", "zzq"), "zzq") == [
        "",
        "# Heading\n\nbody text",
        "| a | b |\n| c | d |",
        "after",
        "- item",
    ]
    assert split_pages("only", s) == ["only"]
    assert split_pages("endSENT", s) == ["end"]


# -------------------------------------------------------------- table_format


def test_html_table_format_sanitizes() -> None:
    html = (
        '<table onclick="x()" class="c"><tr><th style="a" colspan="2">H &amp; <b>B</b></th></tr>'
        '<tr><td rowspan="999999">&lt;script&gt;</td><td data-x="1">*y*</td></tr></table>'
    )
    md = HtmlConverter().convert_string(html, table_format="html").markdown
    assert md.startswith('<table>\n<thead>\n<tr><th colspan="2">H &amp; B</th></tr>')
    assert '<td rowspan="1000">&lt;script&gt;</td><td>*y*</td>' in md
    assert "onclick" not in md and "style" not in md and "class" not in md
    assert "data-x" not in md


def test_html_table_format_auto_and_invalid() -> None:
    simple = "<table><tr><th>a</th></tr><tr><td>b</td></tr></table>"
    assert (
        "<table>"
        not in HtmlConverter().convert_string(simple, table_format="auto").markdown
    )
    assert "| a |" in HtmlConverter().convert_string(simple).markdown
    with pytest.raises(ValueError):
        HtmlConverter().convert_string(simple, table_format="latex")


# ----------------------------------------------------------------------- TSV


def test_tsv_and_delimiter_sniffing() -> None:
    expected = "| name | city | score |\n| --- | --- | --- |\n| Ada | London | 10 |"
    assert _convert(CsvConverter(), "mf_table.tsv").markdown.startswith(expected)
    assert _convert(CsvConverter(), "mf_semicolon.csv").markdown.startswith(expected)
    assert CsvConverter().accepts(io.BytesIO(), StreamInfo(extension=".tsv"))
    assert CsvConverter().accepts(
        io.BytesIO(), StreamInfo(mimetype="text/tab-separated-values")
    )


def test_csv_with_semicolons_in_values_keeps_commas() -> None:
    data = b"a,b\nx;y,z;w\n1;2,3;4\n"
    md = CsvConverter().convert(io.BytesIO(data), StreamInfo(extension=".csv")).markdown
    assert md == "| a | b |\n| --- | --- |\n| x;y | z;w |\n| 1;2 | 3;4 |"


def test_tsv_via_markitforge() -> None:
    result = MarkitForge().convert(str(FILES / "mf_table.tsv"))
    assert "| Linus | Helsinki | 9 |" in result.markdown


# --------------------------------------------------------------------- MHTML


def test_mhtml_basic() -> None:
    result = _convert(MhtmlConverter(), "mf_page.mht")
    md = result.markdown
    assert result.title == "MF Archive Page"
    assert md.startswith("# MF Archive Heading")
    assert "Café text with a soft line break." in md
    assert "![cid image](cid:logo@mf)" in md
    assert "![located image](page_files/pic.png)" in md
    assert result.metadata["subject"] == "MF Archive Subject"
    assert sorted(a.name for a in result.assets) == ["logo@mf", "pic.png"]


def test_mhtml_inlines_images_with_keep_data_uris() -> None:
    md = _convert(MhtmlConverter(), "mf_page.mht", keep_data_uris=True).markdown
    assert "![cid image](data:image/png;base64,iVBOR" in md
    assert "![located image](data:image/png;base64,iVBOR" in md


def test_mhtml_via_markitforge_and_table_format() -> None:
    result = MarkitForge().convert(str(FILES / "mf_page.mht"), table_format="auto")
    assert '<th colspan="2">Span</th>' in result.markdown
    assert MhtmlConverter().accepts(io.BytesIO(), StreamInfo(extension=".mhtml"))
    assert MhtmlConverter().accepts(
        io.BytesIO(), StreamInfo(mimetype="multipart/related")
    )
    assert not MhtmlConverter().accepts(io.BytesIO(), StreamInfo(extension=".eml"))


# --------------------------------------------------------------- LibreOffice


@pytest.mark.parametrize("ext", [".doc", ".rtf", ".odt", ".odp", ".ppt", ".ods"])
def test_libreoffice_accepts_without_soffice(
    monkeypatch: pytest.MonkeyPatch, ext: str
) -> None:
    monkeypatch.setattr(_libreoffice_converter, "find_soffice", lambda: None)
    converter = LibreOfficeConverter()
    assert converter.accepts(io.BytesIO(b"x"), StreamInfo(extension=ext))
    with pytest.raises(MissingDependencyException, match="LibreOffice"):
        converter.convert(io.BytesIO(b"x"), StreamInfo(extension=ext))


def test_libreoffice_rejects_other_formats() -> None:
    converter = LibreOfficeConverter()
    for ext in (".docx", ".xlsx", ".pptx", ".xls", ".pdf", ".txt"):
        assert not converter.accepts(io.BytesIO(), StreamInfo(extension=ext))
    assert converter.accepts(io.BytesIO(), StreamInfo(mimetype="application/msword"))
    assert not converter.accepts(
        io.BytesIO(), StreamInfo(mimetype="application/vnd.ms-excel")
    )


@pytest.mark.skipif(os.name != "posix", reason="uses a shell script stand-in")
def test_libreoffice_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "soffice"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MARKITFORGE_SOFFICE", str(fake))
    with pytest.raises(FileConversionException, match="within 1 seconds"):
        LibreOfficeConverter().convert(
            io.BytesIO(b"x"), StreamInfo(extension=".odt"), libreoffice_timeout=1
        )


@pytest.mark.skipif(os.name != "posix", reason="uses a shell script stand-in")
def test_libreoffice_failure_reports_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = tmp_path / "soffice"
    fake.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MARKITFORGE_SOFFICE", str(fake))
    with pytest.raises(FileConversionException, match="exit code 3.*boom"):
        LibreOfficeConverter().convert(io.BytesIO(b"x"), StreamInfo(extension=".rtf"))


@needs_soffice
@pytest.mark.parametrize("name", ["mf_pages.odt", "mf_pages.rtf", "mf_pages.doc"])
def test_libreoffice_documents(name: str) -> None:
    result = MarkitForge().convert(
        str(FILES / name), page_markers=True, table_format="auto"
    )
    md = result.markdown
    assert result.metadata["converted_via"] == "libreoffice"
    assert result.metadata["source_format"] == name.rsplit(".", 1)[1]
    assert "<!-- page 1 -->\n# First page heading" in md
    assert "<!-- page 2 -->" in md
    assert "Text on the third page." in md
    assert "Merged header" in md and "Tall cell" in md
    assert result.pages and result.pages[0].kind == "page"


@needs_soffice
def test_libreoffice_spreadsheet_keeps_options() -> None:
    result = MarkitForge().convert(str(FILES / "mf_sheets.ods"))
    assert "| Lamp | 4 |  |" in result.markdown
    assert "mf-hidden-sheet-text" not in result.markdown
    assert result.metadata["hidden_sheets"] == ["Secret", "Deep"]

    everything = MarkitForge().convert(
        str(FILES / "mf_sheets.ods"), include_hidden_sheets=True
    )
    assert "mf-hidden-sheet-text" in everything.markdown


@needs_soffice
def test_libreoffice_presentation() -> None:
    result = MarkitForge().convert(str(FILES / "mf_deck.odp"))
    assert "<!-- Slide number: 1 -->\n# MF slide 1 title" in result.markdown
    assert "MF slide 2 body" in result.markdown
    assert [p.title for p in result.pages] == ["MF slide 1 title", "MF slide 2 title"]
    assert result.metadata["slide_count"] == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
