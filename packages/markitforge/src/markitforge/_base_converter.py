# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

from dataclasses import dataclass, field
from typing import Any, BinaryIO, Dict, List, Optional
from ._stream_info import StreamInfo


@dataclass
class DocumentSection:
    """One page, slide or sheet of a converted document. ``index`` is 1-based."""

    index: int
    markdown: str
    title: Optional[str] = None
    kind: str = "page"  # "page", "slide", "sheet", "chapter", ...

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "title": self.title,
            "markdown": self.markdown,
        }


@dataclass
class DocumentAsset:
    """A binary item pulled out of a document, such as an embedded image."""

    name: str
    mime_type: Optional[str] = None
    data: Optional[bytes] = field(default=None, repr=False)
    description: Optional[str] = None
    page: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "size": len(self.data) if self.data is not None else None,
            "description": self.description,
            "page": self.page,
        }


def page_marker(index: int, kind: str = "page") -> str:
    """The HTML comment placed between pages when page markers are on."""
    return f"<!-- {kind} {index} -->"


def join_sections(
    sections: List[DocumentSection], *, markers: bool = False, sep: str = "\n\n"
) -> str:
    """Join sections into one Markdown string, with optional page markers."""
    parts = []
    for sec in sections:
        body = sec.markdown.strip()
        if markers:
            body = (
                f"{page_marker(sec.index, sec.kind)}\n{body}"
                if body
                else page_marker(sec.index, sec.kind)
            )
        if body:
            parts.append(body)
    return sep.join(parts).strip()


def parse_page_range(spec: str, total: int) -> List[int]:
    """Parse "1-3,5,8-" into a sorted list of 1-based indexes within 1..total."""
    spec = spec or ""
    # Specs come from MCP clients and GUI users: bound the work they can ask for.
    if len(spec) > 1000:
        raise ValueError("Page range is too long.")
    chunks = [c.strip() for c in spec.split(",") if c.strip()]
    if len(chunks) > 200:
        raise ValueError("Too many page ranges.")
    picked: set = set()
    for chunk in chunks:
        try:
            if "-" in chunk:
                lo_s, hi_s = chunk.split("-", 1)
                lo = int(lo_s) if lo_s.strip() else 1
                hi = int(hi_s) if hi_s.strip() else total
            else:
                lo = hi = int(chunk)
        except ValueError:
            raise ValueError(f"Bad page range: {chunk!r}") from None
        if lo < 1 or hi < lo:
            raise ValueError(f"Bad page range: {chunk!r}")
        # Clamp to the document so "1-999999999999" costs nothing.
        picked.update(range(lo, min(hi, total) + 1))
    return sorted(picked)


class DocumentConverterResult:
    """The result of converting a document to Markdown.

    Besides the Markdown text, a result can carry structured data:
    - ``metadata``: document properties (author, created, page_count, ...)
    - ``pages``: per-page / per-slide / per-sheet Markdown, when known
    - ``assets``: embedded images and other binaries that were pulled out
    - ``warnings``: problems that did not stop the conversion
    """

    def __init__(
        self,
        markdown: str,
        *,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        pages: Optional[List[DocumentSection]] = None,
        assets: Optional[List[DocumentAsset]] = None,
        warnings: Optional[List[str]] = None,
    ):
        self.markdown = markdown
        self.title = title
        self.metadata: Dict[str, Any] = dict(metadata or {})
        self.pages: List[DocumentSection] = list(pages or [])
        self.assets: List[DocumentAsset] = list(assets or [])
        self.warnings: List[str] = list(warnings or [])

    @property
    def text_content(self) -> str:
        """Soft-deprecated alias for `markdown`. New code should migrate to using `markdown` or __str__."""
        return self.markdown

    @text_content.setter
    def text_content(self, markdown: str):
        """Soft-deprecated alias for `markdown`. New code should migrate to using `markdown` or __str__."""
        self.markdown = markdown

    def select_pages(self, spec: str, *, markers: bool = True) -> str:
        """Markdown for only the pages in ``spec`` (e.g. "1-3,5").

        Raises ValueError when the converter did not record pages.
        """
        if not self.pages:
            raise ValueError("This document has no page information.")
        wanted = set(parse_page_range(spec, max(p.index for p in self.pages)))
        return join_sections(
            [p for p in self.pages if p.index in wanted], markers=markers
        )

    def to_dict(self, *, include_markdown: bool = True) -> Dict[str, Any]:
        """A JSON-friendly view of the result."""
        out: Dict[str, Any] = {
            "title": self.title,
            "metadata": {k: _jsonable(v) for k, v in self.metadata.items()},
            "pages": [p.to_dict() for p in self.pages],
            "assets": [a.to_dict() for a in self.assets],
            "warnings": list(self.warnings),
        }
        if include_markdown:
            out["markdown"] = self.markdown
        else:
            for p in out["pages"]:
                p.pop("markdown", None)
        return out

    def __str__(self) -> str:
        """Return the converted Markdown text."""
        return self.markdown


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


class DocumentConverter:
    """Abstract superclass of all DocumentConverters."""

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> bool:
        """
        Return a quick determination on if the converter should attempt converting the document.
        This is primarily based on `stream_info` (typically, `stream_info.mimetype`, `stream_info.extension`).
        In cases where the data is retrieved via HTTP, the `stream_info.url` might also be referenced to
        make a determination (e.g., special converters for Wikipedia, YouTube etc).
        Finally, it is conceivable that the `stream_info.filename` might be used in cases
        where the filename is well-known (e.g., `Dockerfile`, `Makefile`, etc)

        NOTE: The method signature is designed to match that of the convert() method. This provides some
        assurance that, if accepts() returns True, the convert() method will also be able to handle the document.

        IMPORTANT: In rare cases, (e.g., OutlookMsgConverter) we need to read more from the stream to make a final
        determination. Read operations inevitably advance the position in file_stream. In these cases, the position
        MUST be reset before returning. This is because the convert() method may be called immediately
        after accepts(), and will expect the file_stream to be at the original position.

        E.g.,
        cur_pos = file_stream.tell() # Save the current position
        data = file_stream.read(100) # ... peek at the first 100 bytes, etc.
        file_stream.seek(cur_pos)    # Reset the position to the original position

        Parameters:
        - file_stream: The file-like object to convert. Must support seek(), tell(), and read() methods.
        - stream_info: The StreamInfo object containing metadata about the file (mimetype, extension, charset, etc.)
        - kwargs: Additional keyword arguments for the converter.

        Returns:
        - bool: True if the converter can handle the document, False otherwise.
        """
        raise NotImplementedError(
            f"The subclass, {type(self).__name__}, must implement the accepts() method to determine if they can handle the document."
        )

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        """
        Convert a document to Markdown text.

        Parameters:
        - file_stream: The file-like object to convert. Must support seek(), tell(), and read() methods.
        - stream_info: The StreamInfo object containing metadata about the file (mimetype, extension, charset, etc.)
        - kwargs: Additional keyword arguments for the converter.

        Returns:
        - DocumentConverterResult: The result of the conversion, which includes the title and markdown content.

        Raises:
        - FileConversionException: If the mimetype is recognized, but the conversion fails for some other reason.
        - MissingDependencyException: If the converter requires a dependency that is not installed.
        """
        raise NotImplementedError("Subclasses must implement this method")
