# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""E-mail messages (.eml, ``message/rfc822``).

The message is parsed with the standard library (``email.policy.default``,
so RFC 2047/2231 encoded headers and file names are decoded). The output is
the subject as title, a small header table (From, To, Cc, Date, Subject),
the body -- the HTML alternative converted with the HTML converter when
there is one, the plain-text one otherwise -- and a list of the attachments
(name, type, size).

``convert_attachments=True`` also converts every attachment through the
parent MarkitForge, the way the ZIP converter converts its members. The
archive limits apply (see ``converter_utils._archive_limits``): a message
counts as one container level towards ``max_archive_depth`` and shares the
document-wide ``_archive_budget``, every attachment is an entry, and a part
larger than ``max_archive_member_bytes`` is neither decoded nor converted.
Limit hits and failed attachments become warnings.
"""

import io
import os
import re
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from typing import TYPE_CHECKING, Any, BinaryIO, Dict, Iterator, List, Optional

from .._base_converter import DocumentConverter, DocumentConverterResult
from .._exceptions import (
    FileConversionException,
    ResourceLimitException,
    UnsupportedFormatException,
)
from .._progress import check_cancelled, report_progress
from .._stream_info import StreamInfo
from ..converter_utils._archive_limits import BUDGET_KWARG, get_budget, limit_failure
from ._html_converter import HtmlConverter

if TYPE_CHECKING:
    from .._markitforge import MarkitForge

ACCEPTED_FILE_EXTENSIONS = [".eml"]
ACCEPTED_MIME_TYPE_PREFIXES = ["message/rfc822"]

#: Header table rows: (header, label).
_HEADERS = (
    ("From", "From"),
    ("To", "To"),
    ("Cc", "Cc"),
    ("Date", "Date"),
    ("Subject", "Subject"),
)
#: MIME parts walked per message; more are ignored with a warning.
MAX_PARTS = 1000
#: Multipart nesting followed inside one message.
MAX_PART_DEPTH = 32
_MAX_HEADER_CHARS = 2000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def _clean(value: str, limit: int = _MAX_HEADER_CHARS) -> str:
    """One line of text: controls dropped, whitespace collapsed, length capped."""
    value = re.sub(r"\s+", " ", _CONTROL.sub("", value)).strip()
    if len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def _header(message: Message, name: str) -> str:
    """A decoded header value; malformed headers fall back to their raw text."""
    try:
        values = message.get_all(name) or []
        return _clean(", ".join(str(v) for v in values))
    except Exception:
        pass
    try:
        raw = [v for k, v in message.raw_items() if k.lower() == name.lower()]  # type: ignore[attr-defined]
    except Exception:
        raw = []
    return _clean(", ".join(str(v) for v in raw))


def _filename(part: Message) -> str:
    try:
        name = part.get_filename() or ""
    except Exception:
        name = ""
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    return _clean(_UNSAFE_NAME.sub("_", name), 255)


def _content_type(part: Message) -> str:
    try:
        return part.get_content_type()
    except Exception:
        return "application/octet-stream"


def _payload_size(part: Message) -> int:
    """Approximate decoded size of a leaf part, without decoding it."""
    payload = part.get_payload()
    if not isinstance(payload, str):
        return 0
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if encoding == "base64":
        return len(payload) * 3 // 4
    return len(payload)


def _decode_text(part: Message, data: bytes) -> str:
    """Text of a part, tolerating wrong, unknown or missing charsets."""
    try:
        charset = part.get_content_charset() or ""
    except Exception:
        charset = ""
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return data.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    decoded = data.decode("utf-8", errors="replace")
    if decoded.count("\ufffd") <= max(1, len(decoded) // 100):
        return decoded  # UTF-8 with a stray bad byte or two
    try:
        import charset_normalizer

        best = charset_normalizer.from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return data.decode("utf-8", errors="replace")


_AUTOLINK = re.compile(r"<([^<>\s@]+@[^<>\s]+|https?://[^<>\s]+)>")


def _md_text(value: str, *, cell: bool = True) -> str:
    """Header text for Markdown: no raw HTML (an ``<a@b.example>`` or
    ``<https://...>`` autolink stays) and, in a table ``cell``, no pipe that
    could split it."""
    pieces: List[str] = []
    last = 0
    for match in _AUTOLINK.finditer(value):
        pieces.append(value[last : match.start()].replace("<", "&lt;"))
        pieces.append(match.group(0))
        last = match.end()
    pieces.append(value[last:].replace("<", "&lt;"))
    text = "".join(pieces)
    if cell:
        text = text.replace("\\", "\\\\").replace("|", "\\|")
    return text


def _table(header: List[str], rows: List[List[str]]) -> str:
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines += ["| " + " | ".join(_md_text(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


class _Leaf:
    """A non-multipart part (or an attached message) of the e-mail."""

    def __init__(self, part: Message, *, alternative: bool, attached_message: bool):
        self.part = part
        self.alternative = alternative
        self.attached_message = attached_message
        self.content_type = _content_type(part)
        self.name = _filename(part)
        try:
            disposition = (part.get_content_disposition() or "").lower()
        except Exception:
            disposition = ""
        self.disposition = disposition

    @property
    def is_body_candidate(self) -> bool:
        return (
            not self.attached_message
            and self.disposition != "attachment"
            and not self.name
            and self.content_type in ("text/plain", "text/html")
        )


class EmlConverter(DocumentConverter):
    """Converts .eml e-mail messages to Markdown; see the module docstring."""

    def __init__(self, *, markitforge: Optional["MarkitForge"] = None) -> None:
        super().__init__()
        self._markitforge = markitforge
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

    # ------------------------------------------------------------ helpers

    def _leaves(self, message: Message, warnings: List[str]) -> List[_Leaf]:
        leaves: List[_Leaf] = []
        count = 0
        truncated = False

        def walk(part: Message, depth: int, alternative: bool) -> Iterator[_Leaf]:
            nonlocal count, truncated
            count += 1
            if count > MAX_PARTS:
                truncated = True
                return
            content_type = _content_type(part)
            if content_type == "message/rfc822" or (
                part is not message and content_type.startswith("message/")
            ):
                yield _Leaf(part, alternative=False, attached_message=True)
                return
            if part.is_multipart():
                if depth >= MAX_PART_DEPTH:
                    truncated = True
                    return
                children = part.get_payload()
                if not isinstance(children, list):
                    return
                inner_alternative = content_type == "multipart/alternative"
                for child in children:
                    if isinstance(child, Message):
                        yield from walk(child, depth + 1, inner_alternative)
                return
            yield _Leaf(part, alternative=alternative, attached_message=False)

        leaves.extend(walk(message, 0, False))
        if truncated:
            warnings.append(
                f"The message has more than {MAX_PARTS} MIME parts or nests them "
                f"more than {MAX_PART_DEPTH} deep; the rest were ignored."
            )
        return leaves

    def _decoded(
        self, leaf: _Leaf, max_bytes: int, warnings: List[str]
    ) -> Optional[bytes]:
        """The decoded payload, or None when it is too large or unreadable."""
        label = leaf.name or leaf.content_type
        if _payload_size(leaf.part) > max_bytes:
            warnings.append(
                f"Skipped `{label}`: the part is larger than {max_bytes} bytes "
                "(max_archive_member_bytes)."
            )
            return None
        try:
            if leaf.attached_message:
                inner = leaf.part.get_payload()
                if isinstance(inner, list) and inner:
                    inner = inner[0]
                if isinstance(inner, Message):
                    data = inner.as_bytes()
                elif isinstance(inner, str):
                    data = inner.encode("utf-8", errors="replace")
                else:
                    return None
            else:
                payload = leaf.part.get_payload(decode=True)
                if not isinstance(payload, bytes):
                    return None
                data = payload
        except Exception as exc:
            warnings.append(f"Could not decode `{label}`: {exc}")
            return None
        if len(data) > max_bytes:
            warnings.append(
                f"Skipped `{label}`: the part is larger than {max_bytes} bytes "
                "(max_archive_member_bytes)."
            )
            return None
        return data

    def _body(
        self,
        leaves: List[_Leaf],
        max_bytes: int,
        warnings: List[str],
        kwargs: Dict[str, Any],
    ) -> tuple:
        """(Markdown of the body, the leaves used as body)."""
        candidates = [leaf for leaf in leaves if leaf.is_body_candidate]
        html_leaf = next((c for c in candidates if c.content_type == "text/html"), None)
        text_leaf = next(
            (c for c in candidates if c.content_type == "text/plain"), None
        )
        chosen = html_leaf or text_leaf
        if chosen is None:
            return "", []
        used = [chosen]
        # The other half of a multipart/alternative pair is the same text.
        other = text_leaf if chosen is html_leaf else html_leaf
        if other is not None and other.alternative and chosen.alternative:
            used.append(other)
        data = self._decoded(chosen, max_bytes, warnings)
        if data is None:
            return "", used
        text = _decode_text(chosen.part, data)
        if chosen is html_leaf:
            html_kwargs = {
                k: v
                for k, v in kwargs.items()
                if k not in ("convert_attachments", BUDGET_KWARG)
            }
            markdown = self._html_converter.convert_string(text, **html_kwargs).markdown
        else:
            markdown = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        return markdown.strip(), used

    @staticmethod
    def _header_table(headers: Dict[str, str]) -> str:
        rows = [[label, headers[name]] for name, label in _HEADERS if headers.get(name)]
        return _table(["Header", "Value"], rows) if rows else ""

    @staticmethod
    def _attachment_table(attachments: List[Dict[str, Any]]) -> str:
        return _table(
            ["Name", "Type", "Size"],
            [[a["name"], a["type"], f"{a['size']} bytes"] for a in attachments],
        )

    # ------------------------------------------------------------ convert

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        budget = get_budget(kwargs)
        limits = budget.limits
        warnings: List[str] = []

        # The whole message is bounded by the document-wide byte budget.
        cap = int(limits.max_archive_bytes)
        raw = file_stream.read(cap + 1)
        if len(raw) > cap:
            raise ResourceLimitException(
                f"The message is larger than {cap} bytes (max_archive_bytes)."
            )
        message: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
        del raw

        headers = {name: _header(message, name) for name, _ in _HEADERS}
        message_id = _header(message, "Message-ID")
        leaves = self._leaves(message, warnings)
        max_part = int(limits.max_archive_member_bytes)
        body, used = self._body(leaves, max_part, warnings, kwargs)

        attachments: List[Dict[str, Any]] = []
        attachment_leaves: List[_Leaf] = []
        for leaf in leaves:
            if any(leaf is u for u in used):
                continue
            if leaf.is_body_candidate and leaf.alternative:
                continue  # an unused alternative of the body
            name = leaf.name
            if not name and leaf.attached_message:
                inner = leaf.part.get_payload()
                if isinstance(inner, list) and inner and isinstance(inner[0], Message):
                    subject = _header(inner[0], "Subject")
                    name = _clean(_UNSAFE_NAME.sub("_", subject), 200)
                name = (name or "message") + ".eml"
            if not name:
                name = f"part{len(attachments) + 1}"
            size = _payload_size(leaf.part)
            try:
                if leaf.attached_message:
                    size = len(leaf.part.as_bytes())
                elif size <= max_part:
                    decoded = leaf.part.get_payload(decode=True)
                    if isinstance(decoded, bytes):
                        size = len(decoded)
            except Exception:
                pass
            attachments.append(
                {"name": name, "type": leaf.content_type, "size": int(size)}
            )
            attachment_leaves.append(leaf)

        parts: List[str] = []
        subject = headers.get("Subject") or ""
        if subject:
            parts.append("# " + _md_text(subject, cell=False))
        table = self._header_table(headers)
        if table:
            parts.append(table)
        if body:
            parts.append(body)
        if attachments:
            parts.append("## Attachments\n\n" + self._attachment_table(attachments))

        if kwargs.get("convert_attachments", False) and attachments:
            parts.extend(
                self._convert_attachments(
                    attachments, attachment_leaves, budget, warnings, kwargs
                )
            )

        metadata: Dict[str, Any] = {}
        for key, name in (
            ("from", "From"),
            ("to", "To"),
            ("cc", "Cc"),
            ("date", "Date"),
            ("subject", "Subject"),
        ):
            if headers.get(name):
                metadata[key] = headers[name]
        if message_id:
            metadata["message_id"] = message_id
        metadata["attachments"] = [a["name"] for a in attachments]

        return DocumentConverterResult(
            markdown="\n\n".join(p for p in parts if p).strip(),
            title=subject or None,
            metadata=metadata,
            warnings=warnings,
        )

    def _convert_attachments(
        self,
        attachments: List[Dict[str, Any]],
        leaves: List[_Leaf],
        budget: Any,
        warnings: List[str],
        kwargs: Dict[str, Any],
    ) -> List[str]:
        if self._markitforge is None:
            warnings.append(
                "convert_attachments needs an EmlConverter created by "
                "MarkitForge; the attachments were only listed."
            )
            return []
        limits = budget.limits
        if budget.exhausted is not None:
            warnings.append(f"Attachments not converted: {budget.exhausted}")
            return []
        if budget.depth >= limits.max_archive_depth:
            warnings.append(
                "Attachments not converted: containers nested more than "
                f"{limits.max_archive_depth} deep (max_archive_depth)."
            )
            return []

        member_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in ("file_extension", "url", "progress")
        }
        member_kwargs[BUDGET_KWARG] = budget
        max_part = int(limits.max_archive_member_bytes)
        out: List[str] = []
        budget.depth += 1
        try:
            for done, (info, leaf) in enumerate(zip(attachments, leaves), start=1):
                check_cancelled(kwargs)
                name = info["name"]
                if budget.exhausted is not None:
                    break
                try:
                    budget.charge_entries(1)
                except ResourceLimitException as exc:
                    warnings.append(f"Skipped `{name}`: {exc}")
                    break
                data = self._decoded(leaf, max_part, warnings)
                if data is None:
                    report_progress(kwargs, "entry", done, len(attachments))
                    continue
                try:
                    budget.charge_bytes(len(data))
                except ResourceLimitException as exc:
                    warnings.append(f"Skipped `{name}`: {exc}")
                    break

                result: Optional[DocumentConverterResult] = None
                extension = os.path.splitext(name)[1] or None
                try:
                    with budget.prepaid(len(data)):
                        result = self._markitforge.convert_stream(
                            stream=io.BytesIO(data),
                            stream_info=StreamInfo(
                                extension=extension,
                                filename=name,
                                mimetype=info["type"],
                            ),
                            **member_kwargs,
                        )
                except UnsupportedFormatException:
                    pass
                except ResourceLimitException as exc:
                    warnings.append(f"Skipped `{name}`: {exc}")
                except FileConversionException as exc:
                    failure = limit_failure(exc)
                    if failure is not None:
                        reason = (
                            str(failure)
                            if isinstance(failure, ResourceLimitException)
                            else "nesting too deep to convert (RecursionError)."
                        )
                        warnings.append(f"Skipped `{name}`: {reason}")
                    else:
                        warnings.append(f"Could not convert `{name}`.")
                del data
                if result is not None:
                    out.append(f"## Attachment: {name}\n\n{result.markdown}".strip())
                    warnings.extend(f"{name}: {w}" for w in result.warnings)
                report_progress(kwargs, "entry", done, len(attachments))
        finally:
            budget.depth -= 1
        return out
