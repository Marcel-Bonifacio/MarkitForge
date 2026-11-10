# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import base64
import hashlib
import io
import mimetypes
import os
import re
import sys

from typing import BinaryIO, Any, Dict, List, Optional

from ._html_converter import HtmlConverter
from ._llm_caption import llm_caption
from ..converter_utils.docx.core_properties import read_core_properties
from ..converter_utils.docx.headers_footers import read_headers_footers
from ..converter_utils.docx.page_breaks import (
    PAGE_COUNT_MODES,
    libreoffice_page_count,
    new_sentinel,
    remove_sentinels,
    split_pages,
)
from ..converter_utils.docx.pre_process import pre_process_docx
from .._base_converter import (
    DocumentAsset,
    DocumentConverterResult,
    DocumentSection,
    join_sections,
)
from .._stream_info import StreamInfo
from .._exceptions import MissingDependencyException, MISSING_DEPENDENCY_MESSAGE
from ..converter_utils._archive_limits import (
    ArchiveLimits,
    CaptionCache,
    InlineBudget,
    check_archive_stream,
    data_uri_length,
    get_budget,
)

# Try loading optional (but in this case, required) dependencies
# Save reporting of any exceptions for later
_dependency_exc_info = None
try:
    import mammoth

except ImportError:
    # Preserve the error and stack trace for later
    _dependency_exc_info = sys.exc_info()


ACCEPTED_MIME_TYPE_PREFIXES = [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]

ACCEPTED_FILE_EXTENSIONS = [".docx"]

_UNDERLINE_STYLE_MAP = "u => u"


def _read_embedded_style_map(file_stream: BinaryIO) -> Optional[str]:
    """Read the style map embedded in a .docx, if it has one."""
    position = file_stream.tell()
    file_stream.seek(0)
    try:
        return mammoth.read_embedded_style_map(file_stream)
    finally:
        file_stream.seek(position)


def _image_part_name(image: Any) -> Optional[str]:
    """The package path of a mammoth image, used to recognise repeated images.

    Mammoth does not expose it, but its ``open`` closure captures it as
    ``image_path``. Returns None when that is not available.
    """
    opener = getattr(image, "open", None)
    code = getattr(opener, "__code__", None)
    closure = getattr(opener, "__closure__", None)
    if code is None or not closure:
        return None
    try:
        value = closure[code.co_freevars.index("image_path")].cell_contents
    except (ValueError, IndexError):
        return None
    return value if isinstance(value, str) and value else None


class _Image:
    """What is known about one distinct image part."""

    def __init__(
        self, name: str, content_type: str, src_type: str, data: Optional[bytes]
    ):
        self.name = name
        self.content_type = content_type
        # Media type written into data URIs: mammoth's own (possibly "None")
        # unless captioning, which always used the octet-stream fallback.
        self.src_type = src_type
        self.data = data
        self.data_uri: Optional[str] = None
        self.caption: Optional[str] = None


class _DocxImageHandler:
    """Mammoth ``convert_image`` callback for every DOCX conversion.

    * Without ``keep_data_uris`` the image is never read or encoded: the src
      is ``data:<type>;base64,`` with an empty payload, which the Markdown
      step renders exactly like a full data URI (``data:<type>;base64...``).
    * With ``keep_data_uris`` each distinct image part is encoded once and at
      most ``max_inline_bytes`` characters of data URIs are emitted per
      document; later images get their part name as src, with a warning.
    * With ``describe_images`` each distinct image is captioned once by the
      LLM, at most ``max_llm_calls`` times, and recorded once as an asset.
    """

    def __init__(self, options: Dict[str, Any], *, describe: bool):
        limits = ArchiveLimits.from_kwargs(options)
        self._keep_data_uris = bool(options.get("keep_data_uris", False))
        self._describe = describe
        self._client = options.get("llm_client")
        self._model = options.get("llm_model")
        self._prompt = options.get("llm_prompt")
        self._images: Dict[str, _Image] = {}
        self._inline = InlineBudget(limits.max_inline_bytes)
        self._captions = CaptionCache(
            limits.max_llm_calls,
            similar=bool(options.get("dedupe_similar_images", False)),
        )
        self.assets: List[DocumentAsset] = []
        self._warnings: List[str] = []
        if describe and (self._client is None or self._model is None):
            self._warnings.append(
                "describe_images was requested but no llm_client/llm_model was "
                "given; images were collected without captions."
            )

    @property
    def warnings(self) -> List[str]:
        return self._warnings + self._captions.warnings + self._inline.warnings

    def convert_image(self, image: Any) -> List[Any]:
        from mammoth import html as mammoth_html

        attributes: Dict[str, str] = {}
        if not (self._keep_data_uris or self._describe):
            # Opening (not reading) keeps mammoth's handling of broken
            # references: InvalidFileReferenceError drops the image.
            with image.open():
                pass
            if image.alt_text:
                attributes["alt"] = image.alt_text
            attributes["src"] = "data:{0};base64,".format(image.content_type)
            return [mammoth_html.element("img", attributes)]

        entry = self._entry(image)
        if self._describe:
            self._describe_entry(entry)
        alt = entry.caption or image.alt_text
        if alt:
            attributes["alt"] = alt
        attributes["src"] = self._src(entry)
        return [mammoth_html.element("img", attributes)]

    def _entry(self, image: Any) -> _Image:
        part = _image_part_name(image)
        if part is not None and part in self._images:
            return self._images[part]
        with image.open() as image_stream:
            data = image_stream.read()
        key = part or "sha256:" + hashlib.sha256(data).hexdigest()
        if key in self._images:
            return self._images[key]
        content_type = image.content_type or "application/octet-stream"
        extension = mimetypes.guess_extension(content_type) or ""
        entry = _Image(
            f"image{len(self._images) + 1}{extension}",
            content_type,
            content_type if self._describe else str(image.content_type),
            data,
        )
        self._images[key] = entry
        if self._describe:
            self.assets.append(
                DocumentAsset(
                    name=entry.name,
                    mime_type=content_type,
                    data=data,
                    description=image.alt_text or None,
                )
            )
        return entry

    def _describe_entry(self, entry: _Image) -> None:
        if (
            entry.caption is not None
            or entry.data is None
            or self._client is None
            or self._model is None
        ):
            return
        data = entry.data
        extension = os.path.splitext(entry.name)[1]

        def caption() -> Optional[str]:
            return llm_caption(
                io.BytesIO(data),
                StreamInfo(mimetype=entry.content_type, extension=extension or None),
                client=self._client,
                model=self._model,
                prompt=self._prompt,
            )

        try:
            text = self._captions.caption(data, caption)
        except Exception as exc:
            self._warnings.append(f"Could not caption {entry.name}: {exc}")
            return
        if text:
            # Brackets end the Markdown alt text early, a trailing backslash
            # escapes the closing bracket, and angle brackets could open HTML.
            text = re.sub(r"\s+", " ", re.sub(r"[\[\]\\<>]", " ", text)).strip()
        if text:
            entry.caption = text
            for asset in self.assets:
                if asset.name == entry.name:
                    asset.description = text

    def _src(self, entry: _Image) -> str:
        if not self._keep_data_uris:
            return "data:{0};base64,".format(entry.src_type)
        data = entry.data or b""
        if not self._inline.take(data_uri_length(entry.src_type, len(data))):
            return entry.name
        if entry.data_uri is None:
            entry.data_uri = "data:{0};base64,{1}".format(
                entry.src_type, base64.b64encode(data).decode("ascii")
            )
        return entry.data_uri


def _headers_footers_markdown(headers: List[str], footers: List[str]) -> str:
    blocks: List[str] = []
    if headers:
        blocks.append("## Page headers\n\n" + "\n\n".join(headers))
    if footers:
        blocks.append("## Page footers\n\n" + "\n\n".join(footers))
    return "\n\n".join(blocks)


class DocxConverter(HtmlConverter):
    """
    Converts DOCX files to Markdown. Style information (e.g., headings) and tables are preserved where possible.

    The package is checked against the archive resource limits before any
    part is read (``ResourceLimitException`` on a hit). Images are handled by
    ``_DocxImageHandler``: never base64-encoded unless ``keep_data_uris`` is
    set (then capped at ``max_inline_bytes``), and captioned at most once per
    distinct image within ``max_llm_calls``.
    """

    def __init__(self):
        super().__init__()
        self._html_converter = HtmlConverter()

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        # Check: the dependencies
        if _dependency_exc_info is not None:
            raise MissingDependencyException(
                MISSING_DEPENDENCY_MESSAGE.format(
                    converter=type(self).__name__,
                    extension=".docx",
                    feature="docx",
                )
            ) from _dependency_exc_info[1].with_traceback(  # type: ignore[union-attr]
                _dependency_exc_info[2]
            )

        page_count_mode = kwargs.get("docx_page_count") or None
        if page_count_mode is not None and page_count_mode not in PAGE_COUNT_MODES:
            raise ValueError(
                "docx_page_count must be one of "
                f"{', '.join(PAGE_COUNT_MODES)} (or None); got {page_count_mode!r}"
            )

        # Reject archive bombs before anything reads a member.
        budget = get_budget(kwargs)
        check_archive_stream(file_stream, budget)

        rendered_pages: Optional[int] = None
        page_count_problem = ""
        if page_count_mode == "libreoffice":
            start = file_stream.tell()
            rendered_pages, page_count_problem = libreoffice_page_count(
                file_stream.read(), kwargs
            )
            file_stream.seek(start)

        markers = bool(kwargs.get("page_markers", False))
        sentinel = new_sentinel()
        metadata: Dict[str, Any] = read_core_properties(file_stream)
        warnings: List[str] = []
        headers: List[str] = []
        footers: List[str] = []
        if kwargs.get("include_headers_footers", False):
            headers, footers = read_headers_footers(file_stream)

        pre_process_stream = pre_process_docx(
            file_stream, page_break_sentinel=sentinel, budget=budget
        )

        caller_style_map = kwargs.get("style_map")
        embedded_style_map = _read_embedded_style_map(pre_process_stream)

        style_map = "\n".join(
            part
            for part in (
                caller_style_map,
                embedded_style_map,
                _UNDERLINE_STYLE_MAP,
            )
            if part
        )

        image_adapter = None
        hooked = type(self)._image_to_html is not DocxConverter._image_to_html
        images = _DocxImageHandler(
            kwargs, describe=not hooked and bool(kwargs.get("describe_images", False))
        )
        mammoth_kwargs: dict[str, Any] = {"convert_image": images.convert_image}
        if hooked:
            from ..converter_utils.docx._images import _DocxImages

            image_adapter = _DocxImages(
                self._image_to_html, kwargs, fallback=images.convert_image
            )
            mammoth_kwargs["convert_image"] = image_adapter.convert_image

        html_result = mammoth.convert_to_html(
            pre_process_stream,
            style_map=style_map,
            include_embedded_style_map=False,
            **mammoth_kwargs,
        ).value

        if image_adapter is not None:
            html_result = image_adapter.replace_images(html_result)

        result = self._html_converter.convert_string(html_result, **kwargs)

        sections = [
            DocumentSection(index=i, markdown=text, kind="page")
            for i, text in enumerate(split_pages(result.markdown, sentinel), start=1)
        ]
        if markers:
            markdown = join_sections(sections, markers=True)
        else:
            markdown = remove_sentinels(result.markdown, sentinel)

        extra = _headers_footers_markdown(headers, footers)
        if extra:
            markdown = (markdown + "\n\n" + extra) if markdown else extra
            if headers:
                metadata["headers"] = headers
            if footers:
                metadata["footers"] = footers

        if page_count_mode is not None:
            # "pages" stays as split at the breaks stored in the file; the
            # metadata says how many pages the document really has.
            if rendered_pages is None:
                metadata["page_count"] = len(sections)
                metadata["page_count_source"] = "stored_breaks"
                warnings.append(
                    f"docx_page_count={page_count_mode!r}: {page_count_problem}; "
                    "page_count is the number of sections found from the page "
                    "breaks stored in the file."
                )
            else:
                metadata["page_count"] = rendered_pages
                metadata["page_count_source"] = page_count_mode
                if rendered_pages != len(sections):
                    warnings.append(
                        f"LibreOffice lays this document out on {rendered_pages} "
                        f"page(s), but the page breaks stored in the file give "
                        f"{len(sections)} section(s); 'pages' follows the stored "
                        "breaks and may not match the real pages."
                    )

        assets: List[DocumentAsset] = images.assets
        warnings.extend(images.warnings)

        return DocumentConverterResult(
            markdown=markdown,
            title=result.title if result.title else metadata.get("title"),
            metadata=metadata,
            pages=sections,
            assets=assets,
            warnings=warnings,
        )

    def _image_to_html(
        self,
        image_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> Optional[str]:
        """Override to render an embedded image as an HTML fragment.

        The stream is borrowed, seekable, and positioned at zero; do not close
        or retain it. StreamInfo describes the image, not the document. Existing
        conversion options are forwarded through kwargs.

        Return None or blank text to retain the native image representation.
        Otherwise return HTML, escaping any literal text. Inline HTML stays
        inline; block HTML splits enclosing paragraph/inline wrappers but stays
        inside its table cell or list item. Hook failures propagate through the
        normal conversion failure path.
        """
        return None
