# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""An image that occurs several times in a document is recognized once.

Each format gets a document holding the same picture three times and a
different one once; the service must be asked twice, while every occurrence
still shows its text.
"""

import io
import random
from typing import Any
from unittest.mock import Mock

from docx import Document
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from markitforge import StreamInfo
from markitforge_ocr._docx_converter_with_ocr import DocxConverterWithOCR
from markitforge_ocr._ocr_service import OCRCache, OCRResult
from markitforge_ocr._pdf_converter_with_ocr import PdfConverterWithOCR
from markitforge_ocr._pptx_converter_with_ocr import PptxConverterWithOCR
from markitforge_ocr._xlsx_converter_with_ocr import XlsxConverterWithOCR


def _picture(seed: int) -> Image.Image:
    rnd = random.Random(seed)
    small = Image.new("RGB", (8, 6))
    small.putdata(
        [
            (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for _ in range(48)
        ]
    )
    return small.resize((80, 60))


def _png(seed: int) -> bytes:
    output = io.BytesIO()
    _picture(seed).save(output, format="PNG")
    return output.getvalue()


_LOGO = _png(1)
_CHART = _png(2)
_IMAGES = (_LOGO, _CHART, _LOGO, _LOGO)


def _service() -> Mock:
    return Mock(
        extract_text=Mock(
            side_effect=lambda stream, **_: OCRResult(text="recognized text")
        )
    )


def _convert(converter: Any, data: bytes, extension: str, service: Mock) -> str:
    return converter.convert(
        io.BytesIO(data), StreamInfo(extension=extension), ocr_service=service
    ).markdown


def test_docx_repeated_image_is_recognized_once() -> None:
    document = Document()
    for data in _IMAGES:
        document.add_paragraph().add_run().add_picture(io.BytesIO(data))
    output = io.BytesIO()
    document.save(output)

    service = _service()
    markdown = _convert(DocxConverterWithOCR(), output.getvalue(), ".docx", service)
    assert service.extract_text.call_count == 2
    assert markdown.count("[Image OCR]") == len(_IMAGES)


def test_pptx_repeated_image_is_recognized_once() -> None:
    presentation = Presentation()
    for data in _IMAGES:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(data), Inches(1), Inches(1))
    output = io.BytesIO()
    presentation.save(output)

    service = _service()
    markdown = _convert(PptxConverterWithOCR(), output.getvalue(), ".pptx", service)
    assert service.extract_text.call_count == 2
    assert markdown.count("[Image OCR]") == len(_IMAGES)


def test_xlsx_repeated_image_is_recognized_once() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Item", "Count"])
    sheet.append(["Bolt", 3])
    for row, data in enumerate(_IMAGES, start=4):
        sheet.add_image(SheetImage(io.BytesIO(data)), f"D{row * 5}")
    output = io.BytesIO()
    workbook.save(output)

    service = _service()
    markdown = _convert(XlsxConverterWithOCR(), output.getvalue(), ".xlsx", service)
    assert service.extract_text.call_count == 2
    assert markdown.count("[Image OCR]") == len(_IMAGES)


def test_pdf_repeated_image_is_recognized_once() -> None:
    # One picture per page; the logo is on three of the four pages.
    pages = [_picture(1), _picture(2), _picture(1), _picture(1)]
    output = io.BytesIO()
    pages[0].save(output, format="PDF", save_all=True, append_images=pages[1:])

    service = _service()
    markdown = _convert(PdfConverterWithOCR(), output.getvalue(), ".pdf", service)
    assert service.extract_text.call_count == 2
    assert markdown.count("[Image OCR]") == len(pages)


def test_ocr_cache_keeps_failures_and_is_keyed_by_content() -> None:
    results = [OCRResult(text="", error="quota exceeded"), OCRResult(text="chart")]
    service = Mock(extract_text=Mock(side_effect=results))
    cache = OCRCache()

    first = cache.extract_text(service, io.BytesIO(_LOGO))
    again = cache.extract_text(service, io.BytesIO(_LOGO))
    other = cache.extract_text(
        service, io.BytesIO(_CHART), StreamInfo(mimetype="image/png")
    )

    assert first is again and first.error == "quota exceeded"
    assert other.text == "chart"
    assert service.extract_text.call_count == 2
    assert service.extract_text.call_args.kwargs["stream_info"].mimetype == "image/png"
