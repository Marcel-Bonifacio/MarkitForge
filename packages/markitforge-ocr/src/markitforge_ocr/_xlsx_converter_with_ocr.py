# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

"""XLSX image OCR using the core spreadsheet conversion pipeline."""

from typing import Any, BinaryIO, Optional

from markitforge import DocumentConverterResult, StreamInfo
from markitforge.converters import XlsxConverter

from ._ocr_service import LLMVisionOCRService, _FragmentCache


class XlsxConverterWithOCR(XlsxConverter):
    """Recognize embedded images while inheriting native XLSX conversion."""

    def __init__(self, ocr_service: Optional[LLMVisionOCRService] = None):
        super().__init__()
        if not hasattr(XlsxConverter, "_image_to_html"):
            raise RuntimeError(
                "XLSX OCR requires markitforge>=0.1.8b3 for the "
                "XlsxConverter._image_to_html hook. "
                "Upgrade with: pip install --upgrade 'markitforge>=0.1.8b3'."
            )
        self.ocr_service = ocr_service

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        kwargs["_xlsx_ocr_cache"] = _FragmentCache()
        return super().convert(file_stream, stream_info, **kwargs)

    def _image_to_html(
        self,
        image_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> Optional[str]:
        ocr_service = kwargs.get("ocr_service") or self.ocr_service
        if ocr_service is None:
            return None

        # One OCR call per distinct image (by its bytes) in this document;
        # a repeated image reuses the first result, a failed one included.
        cache: Optional[_FragmentCache] = kwargs.get("_xlsx_ocr_cache")
        if cache is None:
            cache = _FragmentCache()
        return cache.fragment(ocr_service, image_stream, stream_info, "XLSX")
