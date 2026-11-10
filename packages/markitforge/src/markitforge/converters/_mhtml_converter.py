# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MHTML web archives (.mht/.mhtml).

Covers browser "save as single file" archives as well as Word and OneNote
"single file web page" exports. The archive is a MIME multipart/related
message: the HTML part is converted with the HTML converter, and images it
references (by ``cid:`` or Content-Location) are inlined as data URIs when
``keep_data_uris`` is set. Each image is encoded once, and the inlined data
URIs of a document total at most ``max_inline_bytes`` characters (default
50 MiB); past that, images keep their original reference and a warning is
added.
"""

import base64
import email
import email.policy
import posixpath
from email.message import Message
from typing import Any, BinaryIO, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .._base_converter import DocumentAsset, DocumentConverter, DocumentConverterResult
from .._stream_info import StreamInfo
from ..converter_utils._archive_limits import (
    ArchiveLimits,
    InlineBudget,
    data_uri_length,
)
from ._html_converter import HtmlConverter

ACCEPTED_FILE_EXTENSIONS = [".mht", ".mhtml"]
ACCEPTED_MIME_TYPE_PREFIXES = [
    "multipart/related",
    "application/x-mimearchive",
    "message/x-mimearchive",
]

_Resource = Tuple[str, bytes]  # (content type, bytes)


def _text(part: Message) -> str:
    """Decoded text of a text part, tolerating bad or missing charsets."""
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(part.get_payload() or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _location_keys(location: str) -> List[str]:
    """Lookup keys for a Content-Location: the full URL and its unquoted form."""
    location = location.strip()
    keys = [location, unquote(location)]
    return [k for k in dict.fromkeys(keys) if k]


def _basename(url: str) -> str:
    path = urlparse(url).path or url
    return posixpath.basename(unquote(path).replace("\\", "/")).lower()


class MhtmlConverter(DocumentConverter):
    """Converts .mht/.mhtml web archives to Markdown."""

    def __init__(self) -> None:
        super().__init__()
        self._html_converter = HtmlConverter()

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        extension = (stream_info.extension or "").lower()
        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True
        mimetype = (stream_info.mimetype or "").lower()
        return any(
            mimetype.startswith(prefix) for prefix in ACCEPTED_MIME_TYPE_PREFIXES
        )

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        message = email.message_from_binary_file(
            file_stream, policy=email.policy.compat32
        )

        html_part: Optional[Message] = None
        by_location: Dict[str, _Resource] = {}
        by_basename: Dict[str, _Resource] = {}
        by_cid: Dict[str, _Resource] = {}
        assets: List[DocumentAsset] = []

        start = (message.get_param("start") or "") if message.is_multipart() else ""
        start = str(start).strip("<>")

        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            content_id = (part.get("Content-ID") or "").strip().strip("<>")
            if content_type == "text/html":
                if html_part is None or (start and content_id == start):
                    html_part = part
                continue
            if not content_type.startswith("image/"):
                continue
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes):
                continue
            resource = (content_type, data)
            location = part.get("Content-Location") or ""
            for key in _location_keys(location):
                by_location.setdefault(key, resource)
            if location:
                by_basename.setdefault(_basename(location), resource)
            if content_id:
                by_cid.setdefault(content_id, resource)
            name = _basename(location) if location else (content_id or "")
            assets.append(
                DocumentAsset(
                    name=name or f"image{len(assets) + 1}",
                    mime_type=content_type,
                    data=data,
                )
            )

        if html_part is None:
            # Not multipart (or no HTML inside): treat the body as HTML anyway.
            if message.is_multipart():
                return DocumentConverterResult(
                    markdown="",
                    metadata=self._metadata(message),
                    assets=assets,
                    warnings=["The archive contains no HTML part."],
                )
            html_part = message

        html = _text(html_part)
        base = (html_part.get("Content-Location") or "").strip()

        warnings: List[str] = []
        if kwargs.get("keep_data_uris", False) and (by_location or by_cid):
            budget = InlineBudget(ArchiveLimits.from_kwargs(kwargs).max_inline_bytes)
            html = self._inline_images(
                html, base, by_location, by_basename, by_cid, budget
            )
            warnings.extend(budget.warnings)

        result = self._html_converter.convert_string(html, **kwargs)
        subject = (message.get("Subject") or "").strip()
        return DocumentConverterResult(
            markdown=result.markdown,
            title=result.title or subject or None,
            metadata=self._metadata(message),
            assets=assets,
            warnings=warnings,
        )

    @staticmethod
    def _metadata(message: Message) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for header, key in (
            ("Subject", "subject"),
            ("Snapshot-Content-Location", "source_url"),
            ("Date", "date"),
            ("From", "from"),
        ):
            value = message.get(header)
            if value:
                out[key] = str(value).strip()
        return out

    @staticmethod
    def _inline_images(
        html: str,
        base: str,
        by_location: Dict[str, _Resource],
        by_basename: Dict[str, _Resource],
        by_cid: Dict[str, _Resource],
        budget: InlineBudget,
    ) -> str:
        soup = BeautifulSoup(html, "html.parser")
        changed = False
        # One encoding per image part (keyed by the shared resource tuple);
        # the budget caps the characters inlined across all <img> tags.
        encoded: Dict[int, str] = {}
        for img in soup.find_all("img"):
            src = str(img.get("src") or "").strip()
            if not src or src.lower().startswith("data:"):
                continue
            resource: Optional[_Resource] = None
            if src.lower().startswith("cid:"):
                resource = by_cid.get(unquote(src[4:]).strip("<>"))
            else:
                candidates = [src]
                if base:
                    try:
                        candidates.append(urljoin(base, src))
                    except ValueError:
                        pass
                for candidate in candidates:
                    for key in _location_keys(candidate):
                        resource = by_location.get(key)
                        if resource:
                            break
                    if resource:
                        break
                if resource is None:
                    resource = by_basename.get(_basename(src))
            if resource is None:
                continue
            content_type, data = resource
            uri = encoded.get(id(resource))
            if not budget.take(data_uri_length(content_type, len(data))):
                break  # the rest keep their original references
            if uri is None:
                uri = f"data:{content_type};base64," + base64.b64encode(data).decode(
                    "ascii"
                )
                encoded[id(resource)] = uri
            img["src"] = uri
            changed = True
        return str(soup) if changed else html
