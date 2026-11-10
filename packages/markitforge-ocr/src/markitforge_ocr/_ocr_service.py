# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

"""
OCR Service Layer for MarkitForge
Provides LLM Vision-based image text extraction.
"""

import base64
import hashlib
import html
import inspect
from typing import Any, BinaryIO, Optional
from dataclasses import dataclass
from warnings import warn

from markitforge import StreamInfo


@dataclass
class OCRResult:
    """Result from OCR extraction."""

    text: str
    confidence: float | None = None
    backend_used: str | None = None
    error: str | None = None


class LLMVisionOCRService:
    """OCR service using LLM vision models (OpenAI-compatible)."""

    def __init__(
        self,
        client: Any,
        model: str,
        default_prompt: str | None = None,
    ) -> None:
        """
        Initialize LLM Vision OCR service.

        Args:
            client: OpenAI-compatible client
            model: Model name (e.g., 'gpt-4o', 'gemini-2.0-flash')
            default_prompt: Default prompt for OCR extraction
        """
        self.client = client
        self.model = model
        self.default_prompt = default_prompt or (
            "Extract all text from this image. "
            "Return ONLY the extracted text, maintaining the original "
            "layout and order. Do not add any commentary or description."
        )

    def extract_text(
        self,
        image_stream: BinaryIO,
        prompt: str | None = None,
        stream_info: StreamInfo | None = None,
        **kwargs: Any,
    ) -> OCRResult:
        """Extract text using LLM vision."""
        if self.client is None:
            return OCRResult(
                text="",
                backend_used="llm_vision",
                error="LLM client not configured",
            )

        try:
            image_stream.seek(0)

            content_type: str | None = None
            if stream_info:
                content_type = stream_info.mimetype

            if not content_type:
                try:
                    from PIL import Image

                    image_stream.seek(0)
                    img = Image.open(image_stream)
                    fmt = img.format.lower() if img.format else "png"
                    content_type = f"image/{fmt}"
                except Exception:
                    content_type = "image/png"

            image_stream.seek(0)
            base64_image = base64.b64encode(image_stream.read()).decode("utf-8")
            data_uri = f"data:{content_type};base64,{base64_image}"

            actual_prompt = prompt or self.default_prompt
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": actual_prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_uri},
                            },
                        ],
                    }
                ],
            )

            text = response.choices[0].message.content
            return OCRResult(
                text=text.strip() if text else "",
                backend_used="llm_vision",
            )
        except Exception as e:
            return OCRResult(text="", backend_used="llm_vision", error=str(e))
        finally:
            image_stream.seek(0)


def _extract_text_with_metadata(
    ocr_service: LLMVisionOCRService,
    image_stream: BinaryIO,
    stream_info: StreamInfo,
) -> OCRResult:
    """Forward metadata when supported, retaining the legacy stream-only call."""
    extract_text = ocr_service.extract_text
    try:
        inspect.signature(extract_text).bind(image_stream, stream_info=stream_info)
    except (TypeError, ValueError):
        # Unsupported or uninspectable signatures keep the legacy invocation.
        return extract_text(image_stream)
    # Keep service errors outside the signature check; never retry an OCR call.
    return extract_text(image_stream, stream_info=stream_info)


class OCRCache:
    """OCR results of one conversion, keyed by the SHA-256 of the image bytes.

    An image that occurs several times in a document is sent to the service
    once; later occurrences get the first result, including a failed one, so
    a failing image is not retried. Use a new cache for every conversion.
    """

    def __init__(self) -> None:
        self._results: dict[bytes, OCRResult] = {}

    def extract_text(
        self,
        ocr_service: LLMVisionOCRService,
        image_stream: BinaryIO,
        stream_info: StreamInfo | None = None,
    ) -> OCRResult:
        """The service's result for this image, asking it at most once.

        Without ``stream_info`` the service gets the stream alone.
        """
        image_stream.seek(0)
        key = hashlib.sha256(image_stream.read()).digest()
        image_stream.seek(0)
        cached = self._results.get(key)
        if cached is not None:
            return cached
        if stream_info is None:
            result = ocr_service.extract_text(image_stream)
        else:
            result = _extract_text_with_metadata(ocr_service, image_stream, stream_info)
        self._results[key] = result
        return result


class _FragmentCache:
    """OCR HTML fragments of one DOCX/PPTX/XLSX conversion, by image content.

    The ``_image_to_html`` hooks of the Office converters share this: the
    service is asked once per distinct image (SHA-256 of its bytes), and a
    repeat gets the first answer. A failure is warned about once and
    remembered as "keep the native image" (``None``).
    """

    def __init__(self) -> None:
        self._fragments: dict[bytes, Optional[str]] = {}

    def __len__(self) -> int:
        return len(self._fragments)

    def fragment(
        self,
        ocr_service: Any,
        image_stream: BinaryIO,
        stream_info: StreamInfo,
        label: str,
    ) -> Optional[str]:
        image_stream.seek(0)
        key = hashlib.sha256(image_stream.read()).digest()
        image_stream.seek(0)
        if key in self._fragments:
            return self._fragments[key]
        self._fragments[key] = None
        result = _extract_text_with_metadata(ocr_service, image_stream, stream_info)
        if result.error:
            warn(
                f"{label} image OCR failed: {result.error}. Keeping the native image.",
                RuntimeWarning,
                stacklevel=4,
            )
            return None
        text = result.text.strip()
        if not text:
            return None
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        content = html.escape(text).replace("\n", "<br>")
        fragment = f"<p><em>[Image OCR]<br>{content}<br>[End OCR]</em></p>"
        self._fragments[key] = fragment
        return fragment
