# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MarkitForge PDF features: tables, headings, pages, metadata, warnings,
backends, image descriptions and formula transcription.

Fixtures (tests/test_files/mf_*.pdf) are built with reportlab. They are
regenerated when missing; run this file directly to rebuild them all:

    python tests/test_pdf_markitforge.py --rebuild
"""

import io
import logging
import os
import re
import sys
import types
from types import SimpleNamespace

import pytest

from markitforge import MarkitForge, MissingDependencyException, StreamInfo
from markitforge.converters import _pdf_converter
from markitforge.converters._pdf_converter import PdfConverter, _scan_warnings
from markitforge.converter_utils.pdf import _layout
from markitforge.converter_utils.pdf._layout import (
    annotate_headings,
    is_heading_candidate,
    is_math_font,
)
from markitforge.converter_utils.pdf._metadata import parse_pdf_date
from markitforge.converter_utils.pdf._tables import (
    _looks_like_text_table,
    find_page_tables,
    normalize_rows,
    render_markdown_table,
)
from markitforge.converter_utils.pdf._visual import clean_latex

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), "test_files")

PROSE_TABLE = "mf_prose_table.pdf"
THREE_PAGES = "mf_three_pages.pdf"
IMAGE_DOC = "mf_image.pdf"
FORMULA_DOC = "mf_formula.pdf"

INTRO = (
    "This review summarises how the quarter went for the sales team. "
    "Demand stayed steady through the spring and the new pricing model "
    "was rolled out without major problems. Customer feedback was mostly "
    "positive, although delivery times remain a concern in some areas. "
    "The finance team checked every figure in this report before release. "
) * 2
MIDDLE = (
    "Each region reported its own totals at the end of the quarter. The "
    "figures below compare unit sales with revenue for every region that "
    "took part in the pilot programme this year. "
) * 2
CLOSING = (
    "Overall the quarter met its targets. The team will revisit the "
    "delivery process before the next review and share an update with "
    "everyone involved in the programme. "
) * 2
TABLE_ROWS = [
    ["Region", "Units", "Revenue"],
    ["North", "120", "$4,800"],
    ["South", "95", "$3,610"],
    ["West", "143", "$5,720"],
]


# -- fixture builders -------------------------------------------------------


def _build_prose_table(path: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=20, leading=24)
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, leading=18)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13)
    table = Table(TABLE_ROWS, colWidths=[120, 80, 100])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
            ]
        )
    )
    doc = SimpleDocTemplate(
        path,
        pagesize=letter,
        title="MarkitForge Fixture Report",
        author="Forge Tester",
        subject="PDF converter tests",
        invariant=1,
    )
    doc.build(
        [
            Paragraph("Quarterly Sales Review", h1),
            Spacer(1, 10),
            Paragraph(INTRO, body),
            Spacer(1, 12),
            Paragraph("Regional Results", h2),
            Spacer(1, 6),
            Paragraph(MIDDLE, body),
            Spacer(1, 12),
            table,
            Spacer(1, 12),
            Paragraph(CLOSING, body),
        ]
    )


def _build_three_pages(path: str) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setTitle("Three Page Fixture")
    for name in ("Alpha", "Beta", "Gamma"):
        c.setFont("Helvetica-Bold", 20)
        c.drawString(72, 720, f"Chapter {name}")
        c.setFont("Helvetica", 10)
        y = 690
        for i in range(4):
            c.drawString(
                72, y, f"{name} content line {i + 1} has some ordinary body text."
            )
            y -= 14
        c.showPage()
    c.save()


def _red_square_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (220, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


def _build_image_doc(path: str) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "Text before the picture.")
    c.drawImage(ImageReader(io.BytesIO(_red_square_png())), 72, 560, 120, 120)
    c.drawString(72, 530, "Text after the picture.")
    c.showPage()
    c.save()


def _build_formula_doc(path: str) -> None:
    # Real math fonts (CMMI, Cambria Math, ...) cannot be embedded without
    # font files, so the formula uses Courier and the tests widen the
    # math-font pattern to match it.
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter, invariant=1)
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "The energy relation is stated below.")
    c.setFont("Courier", 12)
    c.drawString(150, 690, "E = mc2")
    c.setFont("Helvetica", 10)
    c.drawString(72, 660, "Here c is the speed of light.")
    c.showPage()
    c.save()


BUILDERS = {
    PROSE_TABLE: _build_prose_table,
    THREE_PAGES: _build_three_pages,
    IMAGE_DOC: _build_image_doc,
    FORMULA_DOC: _build_formula_doc,
}


def _fixture(name: str) -> str:
    path = os.path.join(TEST_FILES_DIR, name)
    if not os.path.exists(path):
        pytest.importorskip("reportlab")
        BUILDERS[name](path)
    return path


# -- fake LLM client --------------------------------------------------------


class _FakeCompletions:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def create(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages})
        message = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeLlmClient:
    """Shaped like openai.OpenAI for client.chat.completions.create()."""

    def __init__(self, reply: str):
        self.chat = SimpleNamespace(completions=_FakeCompletions(reply))

    @property
    def calls(self):
        return self.chat.completions.calls


def _convert(name: str, **kwargs):
    return MarkitForge().convert(_fixture(name), **kwargs)


# -- tables and headings ------------------------------------------------------


class TestTablesAndHeadings:
    def test_ruled_table_rendered_as_markdown(self):
        md = _convert(PROSE_TABLE).markdown
        assert re.search(r"\| Region\s+\| Units\s+\| Revenue\s+\|", md)
        assert re.search(r"\| North\s+\| 120\s+\| \$4,800\s+\|", md)
        assert re.search(r"\| West\s+\| 143\s+\| \$5,720\s+\|", md)

    def test_table_words_not_duplicated_in_prose(self):
        md = _convert(PROSE_TABLE).markdown
        for word in ("North", "South", "West", "$4,800", "143"):
            assert md.count(word) == 1, word

    def test_table_in_reading_order(self):
        md = _convert(PROSE_TABLE).markdown
        heading = md.index("# Quarterly Sales Review")
        intro = md.index("This review summarises")
        middle = md.index("Each region reported")
        table = md.index("| Region")
        closing = md.index("Overall the quarter met its targets")
        assert heading < intro < middle < table < closing

    def test_headings_from_font_size(self):
        md = _convert(PROSE_TABLE).markdown
        assert "# Quarterly Sales Review" in md.splitlines()
        assert "## Regional Results" in md.splitlines()
        # body text never becomes a heading
        assert not any(
            line.startswith("#") and "review summarises" in line
            for line in md.splitlines()
        )

    def test_headings_on_plain_pdfminer_path(self):
        md = _convert(THREE_PAGES).markdown
        for name in ("Alpha", "Beta", "Gamma"):
            assert f"# Chapter {name}" in md.splitlines()
            assert f"{name} content line 1 has some ordinary body text." in md

    def test_academic_pdf_gets_no_tables(self):
        md = MarkitForge().convert(os.path.join(TEST_FILES_DIR, "test.pdf")).markdown
        assert "|" not in md

    def test_pdfminer_backend_is_plain_text(self):
        result = _convert(PROSE_TABLE, pdf_backend="pdfminer")
        assert "Quarterly Sales Review" in result.markdown
        assert "# Quarterly" not in result.markdown
        assert "| Region" not in result.markdown
        assert len(result.pages) == 1
        assert result.metadata["page_count"] == 1


# -- pages, metadata, warnings ----------------------------------------------


class TestPagesAndMetadata:
    def test_pages_always_filled(self):
        result = _convert(THREE_PAGES)
        assert [(p.index, p.kind) for p in result.pages] == [
            (1, "page"),
            (2, "page"),
            (3, "page"),
        ]
        assert "Alpha content" in result.pages[0].markdown
        assert "Beta content" in result.pages[1].markdown
        assert "Gamma content" in result.pages[2].markdown
        assert "Alpha" not in result.pages[1].markdown

    def test_page_markers(self):
        md = _convert(THREE_PAGES, page_markers=True).markdown
        positions = [md.index(f"<!-- page {i} -->") for i in (1, 2, 3)]
        assert positions == sorted(positions)
        assert md.startswith("<!-- page 1 -->")
        assert positions[0] < md.index("Alpha") < positions[1]
        assert positions[1] < md.index("Beta") < positions[2]
        assert positions[2] < md.index("Gamma")

    def test_no_page_markers_by_default(self):
        assert "<!-- page" not in _convert(THREE_PAGES).markdown

    def test_select_pages(self):
        result = _convert(THREE_PAGES)
        picked = result.select_pages("2-3")
        assert "Alpha" not in picked
        assert "Beta content" in picked and "Gamma content" in picked
        assert "<!-- page 2 -->" in picked

    def test_page_markers_with_structured_pages(self):
        result = _convert(PROSE_TABLE, page_markers=True)
        assert result.markdown.startswith("<!-- page 1 -->\n# Quarterly Sales Review")

    def test_metadata_and_title(self):
        result = _convert(PROSE_TABLE)
        meta = result.metadata
        assert result.title == "MarkitForge Fixture Report"
        assert meta["title"] == "MarkitForge Fixture Report"
        assert meta["author"] == "Forge Tester"
        assert meta["subject"] == "PDF converter tests"
        assert meta["page_count"] == 1
        assert "ReportLab" in meta["producer"]
        assert re.match(r"\d{4}-\d{2}-\d{2}T", meta["created"])

    def test_metadata_on_existing_fixture(self):
        path = os.path.join(TEST_FILES_DIR, "REPAIR-2022-INV-001_multipage.pdf")
        result = MarkitForge().convert(path)
        assert result.metadata["author"] == "Miti Joshi"
        assert result.metadata["page_count"] == 3
        assert result.metadata["created"] == "2025-11-25T12:24:09-05:00"
        assert len(result.pages) == 3
        assert result.title is None  # empty Title in the file

    def test_placeholder_metadata_is_dropped(self):
        path = os.path.join(TEST_FILES_DIR, "SPARSE-2024-INV-1234_borderless_table.pdf")
        meta = MarkitForge().convert(path).metadata
        assert "title" not in meta  # "(anonymous)"
        assert "author" not in meta

    def test_scan_warning(self):
        path = os.path.join(
            TEST_FILES_DIR, "MEDRPT-2024-PAT-3847_medical_report_scan.pdf"
        )
        result = MarkitForge().convert(path)
        assert (
            "Page 2 has no text layer; it may be a scan. "
            "Try the markitforge-ocr plugin." in result.warnings
        )
        assert len(result.pages) == 3

    def test_many_scanned_pages_share_one_warning(self):
        warnings = _scan_warnings(list(range(1, 11)) + [14])
        assert warnings == [
            "Pages 1-10, 14 have no text layer; they may be scans. "
            "Try the markitforge-ocr plugin."
        ]

    def test_text_pdf_has_no_warnings(self):
        assert _convert(PROSE_TABLE).warnings == []


# -- backends and fallback ----------------------------------------------------


def _raw_convert(name: str, **kwargs):
    with open(_fixture(name), "rb") as fh:
        return PdfConverter().convert(fh, StreamInfo(extension=".pdf"), **kwargs)


class TestBackends:
    def test_auto_falls_back_with_warning_and_log(self, monkeypatch, caplog):
        def boom(page):
            raise RuntimeError("synthetic layout failure")

        monkeypatch.setattr(_pdf_converter, "_extract_form_content_from_words", boom)
        with caplog.at_level(logging.WARNING, logger=_pdf_converter.__name__):
            result = _raw_convert(THREE_PAGES)
        assert "Alpha content" in result.markdown
        assert any("fell back" in w and "pdfminer" in w for w in result.warnings)
        assert any("synthetic layout failure" in r.getMessage() for r in caplog.records)
        assert len(result.pages) == 3

    def test_pdfplumber_backend_raises(self, monkeypatch):
        def boom(page):
            raise RuntimeError("synthetic layout failure")

        monkeypatch.setattr(_pdf_converter, "_extract_form_content_from_words", boom)
        with pytest.raises(RuntimeError, match="synthetic"):
            _raw_convert(THREE_PAGES, pdf_backend="pdfplumber")

    def test_unknown_backend(self):
        with pytest.raises(ValueError, match="pdf_backend"):
            _raw_convert(THREE_PAGES, pdf_backend="magic")

    def test_docling_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "docling", None)
        monkeypatch.setitem(sys.modules, "docling.document_converter", None)
        with pytest.raises(MissingDependencyException, match="pip install docling"):
            _raw_convert(THREE_PAGES, pdf_backend="docling")

    def test_docling_adapter(self, monkeypatch):
        seen = {}

        class FakeDoc:
            pages = {1: object(), 2: object()}

            def export_to_markdown(self, page_no=None):
                if page_no is None:
                    return "# Docling\n\nFirst\n\nSecond"
                return {1: "# Docling\n\nFirst", 2: "Second"}[page_no]

        class FakeDocumentConverter:
            def convert(self, source):
                seen["source"] = str(source)
                with open(source, "rb") as fh:
                    seen["head"] = fh.read(5)
                return SimpleNamespace(document=FakeDoc())

        package = types.ModuleType("docling")
        module = types.ModuleType("docling.document_converter")
        module.DocumentConverter = FakeDocumentConverter
        package.document_converter = module
        monkeypatch.setitem(sys.modules, "docling", package)
        monkeypatch.setitem(sys.modules, "docling.document_converter", module)

        result = _raw_convert(THREE_PAGES, pdf_backend="docling")
        assert result.markdown == "# Docling\n\nFirst\n\nSecond"
        assert [p.markdown for p in result.pages] == ["# Docling\n\nFirst", "Second"]
        assert seen["head"] == b"%PDF-"
        assert not os.path.exists(seen["source"])  # temp file removed
        assert result.metadata["page_count"] == 3
        assert result.title == "Three Page Fixture"

        marked = _raw_convert(THREE_PAGES, pdf_backend="docling", page_markers=True)
        assert marked.markdown.startswith("<!-- page 1 -->\n# Docling")


# -- LLM image descriptions and formulas --------------------------------------


class TestDescribeImages:
    def test_images_described_inline(self):
        client = FakeLlmClient("A red square")
        result = _convert(
            IMAGE_DOC, describe_images=True, llm_client=client, llm_model="fake-model"
        )
        md = result.markdown
        assert "![A red square](page1_img1.png)" in md
        assert (
            md.index("Text before the picture.")
            < md.index("![A red square]")
            < md.index("Text after the picture.")
        )
        assert len(result.assets) == 1
        asset = result.assets[0]
        assert asset.name == "page1_img1.png"
        assert asset.mime_type == "image/png"
        assert asset.page == 1
        assert asset.description == "A red square"
        assert asset.data.startswith(b"\x89PNG")

        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["model"] == "fake-model"
        parts = call["messages"][0]["content"]
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_custom_prompt_is_passed(self):
        client = FakeLlmClient("x")
        _convert(
            IMAGE_DOC,
            describe_images=True,
            llm_client=client,
            llm_model="m",
            llm_prompt="Say what colour this is.",
        )
        assert client.calls[0]["messages"][0]["content"][0]["text"] == (
            "Say what colour this is."
        )

    def test_default_output_has_no_image_placeholders(self):
        result = _convert(IMAGE_DOC)
        assert "![" not in result.markdown
        assert result.assets == []
        assert result.warnings == []

    def test_client_without_flag_is_not_called(self):
        client = FakeLlmClient("A red square")
        result = _convert(IMAGE_DOC, llm_client=client, llm_model="m")
        assert client.calls == []
        assert "![" not in result.markdown

    def test_flag_without_client_warns(self):
        result = _convert(IMAGE_DOC, describe_images=True)
        assert "![" not in result.markdown
        assert any("describe_images needs llm_client" in w for w in result.warnings)

    def test_caption_failure_is_a_warning(self):
        class Broken:
            def create(self, **kwargs):
                raise ConnectionError("LLM offline")

        client = SimpleNamespace(chat=SimpleNamespace(completions=Broken()))
        result = _convert(
            IMAGE_DOC, describe_images=True, llm_client=client, llm_model="m"
        )
        assert "Text after the picture." in result.markdown
        assert "![](page1_img1.png)" in result.markdown
        assert any("LLM offline" in w for w in result.warnings)


@pytest.fixture
def courier_is_math(monkeypatch):
    monkeypatch.setattr(_layout, "_MATH_FONT_RE", re.compile("Courier"))


class TestFormulas:
    def test_math_font_names(self):
        for name in (
            "ABCDEF+CMMI10",
            "CMSY7",
            "CMEX10",
            "CambriaMath",
            "Cambria Math",
            "STIXTwoMath-Regular",
            "MathJax_Main-Regular",
        ):
            assert is_math_font(name), name
        for name in ("Helvetica", "ABCDEF+CMR10", "CMTT10", "NimbusRomNo9L-Regu"):
            assert not is_math_font(name), name

    def test_formula_transcribed(self, courier_is_math):
        client = FakeLlmClient("```latex\n$$E = mc^2$$\n```")
        result = _convert(
            FORMULA_DOC, transcribe_formulas=True, llm_client=client, llm_model="m"
        )
        md = result.markdown
        assert "$$\nE = mc^2\n$$" in md
        assert "E = mc2" not in md
        assert (
            md.index("The energy relation")
            < md.index("$$")
            < md.index("Here c is the speed of light.")
        )
        assert "LaTeX" in client.calls[0]["messages"][0]["content"][0]["text"]
        assert result.warnings == []

    def test_formula_without_client_warns_once(self, courier_is_math):
        result = _convert(FORMULA_DOC, transcribe_formulas=True)
        assert "E = mc2" in result.markdown
        garbled = [w for w in result.warnings if "formulas may be garbled" in w]
        assert len(garbled) == 1
        assert "transcribe_formulas needs llm_client" in garbled[0]

    def test_formula_flag_off_leaves_text(self, courier_is_math):
        client = FakeLlmClient("E = mc^2")
        result = _convert(FORMULA_DOC, llm_client=client, llm_model="m")
        assert "E = mc2" in result.markdown
        assert client.calls == []
        assert any("formulas may be garbled" in w for w in result.warnings)

    def test_clean_latex(self):
        assert clean_latex("```latex\n\\frac{a}{b}\n```") == "\\frac{a}{b}"
        assert clean_latex("$$ x^2 $$") == "x^2"
        assert clean_latex("\\[ y \\]") == "y"
        assert clean_latex("$z$") == "z"


# -- helpers --------------------------------------------------------------------


class TestHelpers:
    def test_annotate_headings_joins_split_lines(self):
        text = "1\n\nIntroduction\n\nBody text here.\n"
        out = annotate_headings(text, [("1 Introduction", 2)])
        assert out == "## 1 Introduction\n\nBody text here.\n"

    def test_annotate_headings_unchanged_without_match(self):
        text = "Some\ntext\x0c"
        assert annotate_headings(text, [("Missing", 1)]) is text

    def test_heading_candidate_rules(self):
        assert is_heading_candidate("Results", 16, 10)
        assert not is_heading_candidate("Results", 11, 10)  # not larger enough
        assert not is_heading_candidate("This is a sentence.", 16, 10)
        assert not is_heading_candidate("12", 16, 10)
        assert not is_heading_candidate("Page 3 of 9", 16, 10)
        assert not is_heading_candidate("word " * 20, 16, 10)

    def test_parse_pdf_date(self):
        assert parse_pdf_date("D:20231005000756Z") == "2023-10-05T00:07:56+00:00"
        assert parse_pdf_date("D:20251125122409-05'00'") == "2025-11-25T12:24:09-05:00"
        assert parse_pdf_date("D:2024") == "2024-01-01T00:00:00"
        assert parse_pdf_date("yesterday") is None

    @pytest.mark.parametrize(
        "name, page_no, expected",
        [
            # justified italic disclaimer: text strategy must not fire
            ("REPAIR-2022-INV-001_multipage.pdf", 3, []),
            ("test.pdf", 1, []),
            ("RECEIPT-2024-TXN-98765_retail_purchase.pdf", 1, []),
            ("masterformat_partial_numbering.pdf", 1, []),
            # ruled 11 x 6 grid
            ("SPARSE-2024-INV-1234_borderless_table.pdf", 2, [(11, 6)]),
        ],
    )
    def test_find_page_tables(self, name, page_no, expected):
        import pdfplumber

        with pdfplumber.open(os.path.join(TEST_FILES_DIR, name)) as pdf:
            tables = find_page_tables(pdf.pages[page_no - 1])
        assert [(len(t.rows), len(t.rows[0])) for t in tables] == expected

    def test_text_table_guard(self):
        long_cells = [["word " * 8] * 3] * 4
        assert not _looks_like_text_table(normalize_rows(long_cells))
        sparse = [["a", "", ""], ["", "b", ""], ["", "", "c"], ["d", "", ""]]
        assert not _looks_like_text_table(normalize_rows(sparse))
        table = [
            ["SKU", "Qty", "Price"],
            ["A-1", "2", "$3"],
            ["B-2", "5", "$9"],
        ]
        assert _looks_like_text_table(normalize_rows(table))

    def test_render_markdown_table_escapes_pipes(self):
        md = render_markdown_table(normalize_rows([["a|b", None], ["1", "2\n3"]]))
        assert md.splitlines() == [
            "| a\\|b |     |",
            "| ---- | --- |",
            "| 1    | 2 3 |",
        ]


if __name__ == "__main__":
    if "--rebuild" in sys.argv:
        for fixture_name, build in BUILDERS.items():
            build(os.path.join(TEST_FILES_DIR, fixture_name))
            print("wrote", fixture_name)
    else:
        raise SystemExit(pytest.main([__file__]))
