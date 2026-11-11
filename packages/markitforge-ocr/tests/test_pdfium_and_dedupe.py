# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PDFium (pypdfium2) replaces PyMuPDF for broken PDFs; the Office OCR hooks
share one per-document cache."""

import io
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from docx import Document
from PIL import Image

from markitforge import StreamInfo
from markitforge_ocr import _pdf_converter_with_ocr
from markitforge_ocr._docx_converter_with_ocr import DocxConverterWithOCR
from markitforge_ocr._ocr_service import OCRResult, _FragmentCache
from markitforge_ocr._pdf_converter_with_ocr import PdfConverterWithOCR
from markitforge_ocr._pptx_converter_with_ocr import PptxConverterWithOCR

PACKAGE = Path(__file__).resolve().parents[1]


def _pdf(pages: int) -> bytes:
    images = [Image.new("RGB", (60, 40), c) for c in ("red", "green", "blue")[:pages]]
    out = io.BytesIO()
    images[0].save(out, format="PDF", save_all=True, append_images=images[1:])
    return out.getvalue()


def test_full_page_fallback_renders_with_pdfium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_open(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("pdfplumber cannot read this file")

    monkeypatch.setattr(_pdf_converter_with_ocr.pdfplumber, "open", broken_open)
    sizes = []

    def extract_text(stream: Any, **_: Any) -> OCRResult:
        with Image.open(stream) as image:
            assert image.format == "PNG"
            sizes.append(image.size)
        return OCRResult(text=f"page text {len(sizes)}")

    service = Mock(extract_text=Mock(side_effect=extract_text))
    markdown = PdfConverterWithOCR()._ocr_full_pages(io.BytesIO(_pdf(2)), service)
    assert markdown.count("## Page") == 2
    assert "page text 1" in markdown and "page text 2" in markdown
    # 300 DPI: a 60x40 px page saved at 72 DPI renders about 4x larger.
    assert sizes[0][0] > 200


def test_truncated_pdf_still_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_open(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("no trailer")

    monkeypatch.setattr(_pdf_converter_with_ocr.pdfplumber, "open", broken_open)
    data = _pdf(1)
    truncated = data[: data.rfind(b"startxref")]
    service = Mock(extract_text=Mock(return_value=OCRResult(text="recovered")))
    markdown = PdfConverterWithOCR()._ocr_full_pages(io.BytesIO(truncated), service)
    assert "recovered" in markdown


def test_unreadable_pdf_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_open(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("broken")

    monkeypatch.setattr(_pdf_converter_with_ocr.pdfplumber, "open", broken_open)
    service = Mock(extract_text=Mock(return_value=OCRResult(text="x")))
    markdown = PdfConverterWithOCR()._ocr_full_pages(
        io.BytesIO(b"not a pdf at all"), service
    )
    assert markdown == "*[Error: Could not process scanned PDF]*"


def test_pyproject_has_no_pymupdf_and_dual_license() -> None:
    text = (PACKAGE / "pyproject.toml").read_text()
    assert "pymupdf" not in text.lower()
    assert "pypdfium2" in text
    assert 'license = "MIT AND Apache-2.0"' in text
    for name in ("LICENSE", "LICENSE-APACHE", "NOTICE"):
        assert (PACKAGE / name).is_file()
    for source in (PACKAGE / "src" / "markitforge_ocr").glob("*.py"):
        assert "fitz" not in source.read_text()


def _png(color: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (30, 20), color).save(out, format="PNG")
    return out.getvalue()


def test_failed_repeated_image_is_asked_and_warned_once() -> None:
    document = Document()
    for _ in range(3):
        document.add_paragraph().add_run().add_picture(io.BytesIO(_png("red")))
    out = io.BytesIO()
    document.save(out)
    service = Mock(
        extract_text=Mock(return_value=OCRResult(text="", error="quota exceeded"))
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        markdown = (
            DocxConverterWithOCR()
            .convert(
                io.BytesIO(out.getvalue()),
                StreamInfo(extension=".docx"),
                ocr_service=service,
            )
            .markdown
        )
    assert service.extract_text.call_count == 1
    assert sum("quota exceeded" in str(w.message) for w in caught) == 1
    assert markdown.count("![](data:image/png;base64...)") == 3


def test_fragment_cache_is_per_document() -> None:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    for _ in range(2):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(_png("blue")), Inches(1), Inches(1))
    out = io.BytesIO()
    presentation.save(out)
    service = Mock(extract_text=Mock(return_value=OCRResult(text="slide text")))
    converter = PptxConverterWithOCR()
    for _ in range(2):
        converter.convert(
            io.BytesIO(out.getvalue()),
            StreamInfo(extension=".pptx"),
            ocr_service=service,
        )
    # Once per conversion, not once per instance.
    assert service.extract_text.call_count == 2

    cache = _FragmentCache()
    ok = Mock(extract_text=Mock(return_value=OCRResult(text="a <b>\nc")))
    fragment = cache.fragment(ok, io.BytesIO(b"img"), StreamInfo(), "PPTX")
    assert fragment == "<p><em>[Image OCR]<br>a &lt;b&gt;<br>c<br>[End OCR]</em></p>"
    assert cache.fragment(ok, io.BytesIO(b"img"), StreamInfo(), "PPTX") is fragment
    assert ok.extract_text.call_count == 1 and len(cache) == 1
