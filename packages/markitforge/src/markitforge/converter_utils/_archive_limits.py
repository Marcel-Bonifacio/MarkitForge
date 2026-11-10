# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Resource limits for ZIP-based inputs (ZIP, DOCX, XLSX, PPTX, EPUB).

Every limit is an optional keyword argument of ``MarkitForge.convert()``;
``None`` (or leaving it out) keeps the default:

* ``max_archive_bytes`` -- total uncompressed bytes per document, shared by
  every archive nested inside it (default 500 MiB).
* ``max_archive_member_bytes`` -- uncompressed bytes of a single member
  (default 200 MiB).
* ``max_archive_entries`` -- members per document, shared by every nested
  archive (default 2000).
* ``max_archive_depth`` -- how many ZIP archives may be nested inside each
  other, counting the outermost one (default 3).
* ``max_compression_ratio`` -- largest uncompressed/compressed ratio for a
  member, enforced only for members larger than 1 MiB (default 200).
* ``max_inline_bytes`` -- characters of ``data:`` URIs a document may inline
  when ``keep_data_uris`` is set (default 50 MiB).
* ``max_llm_calls`` -- LLM image-caption calls per document, after identical
  images have been deduplicated (default 50).

Sizes declared in the ZIP central directory are checked before a member is
read, and bytes are counted again while streaming, so a member that inflates
past its declared size or past the cap is cut off. A limit hit raises
:class:`~markitforge._exceptions.ResourceLimitException`.
"""

import hashlib
import zipfile
from dataclasses import dataclass, fields
from typing import IO, Any, Callable, Dict, List, Mapping, NoReturn, Optional

from .._exceptions import FileConversionException, ResourceLimitException

MIB = 1024 * 1024

#: Members at or below this size are exempt from the compression-ratio check.
RATIO_CHECK_MIN_BYTES = MIB

#: Keyword argument that carries the shared :class:`ArchiveBudget` into
#: nested conversions.
BUDGET_KWARG = "_archive_budget"

_CHUNK_SIZE = MIB


def _number(value: float) -> str:
    """A limit for messages: whole numbers without exponent or decimals."""
    return str(int(value)) if float(value).is_integer() else str(value)


@dataclass(frozen=True)
class ArchiveLimits:
    """The configured limits; see the module docstring."""

    max_archive_bytes: int = 500 * MIB
    max_archive_member_bytes: int = 200 * MIB
    max_archive_entries: int = 2000
    max_archive_depth: int = 3
    max_compression_ratio: float = 200
    max_inline_bytes: int = 50 * MIB
    max_llm_calls: int = 50

    @classmethod
    def from_kwargs(cls, kwargs: Mapping[str, Any]) -> "ArchiveLimits":
        """Limits from conversion kwargs; missing or ``None`` values use defaults."""
        values: Dict[str, Any] = {}
        for field in fields(cls):
            value = kwargs.get(field.name)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise ValueError(
                    f"{field.name} must be a non-negative number, got {value!r}"
                )
            values[field.name] = value
        return cls(**values)


class ArchiveBudget:
    """Bytes and entries consumed so far by one top-level conversion.

    One budget is shared by an archive and everything nested inside it (it
    travels through the conversion kwargs as ``_archive_budget``). Once a
    document-wide limit (total bytes or entries) is hit the budget is
    *exhausted*: every later charge fails with the same message, so nested
    converters stop as well.
    """

    def __init__(self, limits: Optional[ArchiveLimits] = None) -> None:
        self.limits = limits or ArchiveLimits()
        self.bytes_used = 0
        self.entries_used = 0
        #: Number of ZIP containers currently being unpacked (ZipConverter).
        self.depth = 0
        #: Nested archives refused for exceeding max_archive_depth.
        self.depth_refusals = 0
        #: Message of the document-wide limit that was hit, if any.
        self.exhausted: Optional[str] = None

    def _exhaust(self, message: str) -> NoReturn:
        if self.exhausted is None:
            self.exhausted = message
        raise ResourceLimitException(message)

    def ensure_open(self) -> None:
        """Raise if a document-wide limit was already hit."""
        if self.exhausted is not None:
            raise ResourceLimitException(self.exhausted)

    def charge_entries(self, count: int) -> None:
        self.ensure_open()
        self.entries_used += count
        if self.entries_used > self.limits.max_archive_entries:
            self._exhaust(
                f"Archive limit reached: more than {self.limits.max_archive_entries} "
                "entries in total (max_archive_entries)."
            )

    def charge_bytes(self, count: int) -> None:
        self.ensure_open()
        self.bytes_used += count
        if self.bytes_used > self.limits.max_archive_bytes:
            self._exhaust(
                f"Archive limit reached: more than {self.limits.max_archive_bytes} "
                "uncompressed bytes in total (max_archive_bytes)."
            )

    def check_member(self, info: zipfile.ZipInfo) -> None:
        """Check a member's declared size and compression ratio (no charge)."""
        limits = self.limits
        size = info.file_size
        if size > limits.max_archive_member_bytes:
            raise ResourceLimitException(
                f"Archive member {info.filename!r} is {size} bytes uncompressed, "
                f"more than {limits.max_archive_member_bytes} "
                "(max_archive_member_bytes)."
            )
        if size > RATIO_CHECK_MIN_BYTES and size > limits.max_compression_ratio * max(
            info.compress_size, 1
        ):
            raise ResourceLimitException(
                f"Archive member {info.filename!r} expands from "
                f"{info.compress_size} to {size} bytes, a compression ratio above "
                f"{_number(limits.max_compression_ratio)} (max_compression_ratio)."
            )


def get_budget(kwargs: Mapping[str, Any]) -> ArchiveBudget:
    """The shared budget from ``kwargs``, or a new one built from its limits."""
    budget = kwargs.get(BUDGET_KWARG)
    if isinstance(budget, ArchiveBudget):
        return budget
    return ArchiveBudget(ArchiveLimits.from_kwargs(kwargs))


def check_archive(archive: zipfile.ZipFile, budget: ArchiveBudget) -> None:
    """Validate a whole archive from its central directory, before any read.

    Every member must respect the per-member size and ratio limits, and the
    archive's entries and total declared size are charged to ``budget``.
    """
    infos = archive.infolist()
    budget.charge_entries(len(infos))
    total = 0
    for info in infos:
        budget.check_member(info)
        total += info.file_size
    budget.charge_bytes(total)


def check_archive_stream(file_stream: IO[bytes], budget: ArchiveBudget) -> None:
    """:func:`check_archive` for a seekable stream; its position is restored.

    A stream that is not a readable ZIP is left for the converter to reject.
    """
    position = file_stream.tell()
    try:
        try:
            archive = zipfile.ZipFile(file_stream)
        except (zipfile.BadZipFile, OSError, ValueError, EOFError):
            return
        with archive:
            check_archive(archive, budget)
    finally:
        file_stream.seek(position)


def _member_cap(info: zipfile.ZipInfo, budget: ArchiveBudget) -> int:
    return min(info.file_size, budget.limits.max_archive_member_bytes)


def _overflow(info: zipfile.ZipInfo, cap: int) -> NoReturn:
    raise ResourceLimitException(
        f"Archive member {info.filename!r} inflates past {cap} bytes "
        "(its declared size or max_archive_member_bytes)."
    )


def read_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: ArchiveBudget
) -> bytes:
    """Read a member in chunks, stopping at its declared size or the member cap.

    Call :meth:`ArchiveBudget.check_member` (or :func:`check_archive`) first.
    """
    cap = _member_cap(info, budget)
    chunks: List[bytes] = []
    total = 0
    with archive.open(info) as source:
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                _overflow(info, cap)
            chunks.append(chunk)
    return b"".join(chunks)


def copy_member(
    source: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: zipfile.ZipFile,
    budget: ArchiveBudget,
    *,
    compress_type: Optional[int] = None,
) -> None:
    """Stream one member from ``source`` into ``target`` without loading it whole.

    The copy keeps the member's name, timestamp and attributes; it is stored
    with ``compress_type`` (default: the member's own compression).
    """
    cap = _member_cap(info, budget)
    try:
        copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    except ValueError:  # a timestamp ZIP cannot store (e.g. before 1980)
        copy = zipfile.ZipInfo(info.filename)
    copy.compress_type = info.compress_type if compress_type is None else compress_type
    copy.external_attr = info.external_attr
    copy.create_system = info.create_system
    copy.comment = info.comment
    copy.file_size = info.file_size  # lets zipfile pick zip64 when needed
    total = 0
    with source.open(info) as reader, target.open(copy, "w") as writer:
        while True:
            chunk = reader.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                _overflow(info, cap)
            writer.write(chunk)


def limit_failure(exc: BaseException) -> Optional[BaseException]:
    """The resource-limit or recursion error behind a failed nested conversion.

    ``MarkitForge`` wraps converter errors in ``FileConversionException``; this
    digs the underlying ``ResourceLimitException``/``RecursionError`` back out.
    """
    if isinstance(exc, (ResourceLimitException, RecursionError)):
        return exc
    if isinstance(exc, FileConversionException):
        for attempt in exc.attempts or []:
            if attempt.exc_info is not None:
                found = limit_failure(attempt.exc_info[1])
                if found is not None:
                    return found
    return None


def data_uri_length(content_type: str, size: int) -> int:
    """Length of ``data:<content_type>;base64,<payload>`` for ``size`` bytes."""
    return len(f"data:{content_type};base64,") + 4 * -(-size // 3)


class InlineBudget:
    """Characters of ``data:`` URIs one document may still inline.

    Every emitted URI counts, including repeats of the same image, because
    each repeat lands in the output again. The first refusal adds a warning
    and every later request is refused too.
    """

    def __init__(self, max_chars: float) -> None:
        self.max_chars = max_chars
        self.used = 0
        self.stopped = False
        self.warnings: List[str] = []

    def take(self, size: int) -> bool:
        if not self.stopped and self.used + size <= self.max_chars:
            self.used += size
            return True
        if not self.stopped:
            self.stopped = True
            self.warnings.append(
                f"Stopped inlining images as data URIs after {self.used} characters "
                f"(max_inline_bytes={_number(self.max_chars)}); the remaining images were "
                "not inlined."
            )
        return False


class CaptionCache:
    """Per-document LLM captions: one call per distinct image, at most ``max_calls``.

    Images are identified by the SHA-256 of their bytes. A caption that
    failed is remembered as ``None`` so the same image is not retried. Once
    the call budget is spent, uncaptioned images get ``None`` and a single
    warning is added.
    """

    def __init__(self, max_calls: float) -> None:
        self.calls_left = max_calls
        self.calls = 0
        self.warnings: List[str] = []
        self._captions: Dict[str, Optional[str]] = {}

    def caption(
        self, data: bytes, produce: Callable[[], Optional[str]]
    ) -> Optional[str]:
        key = hashlib.sha256(data).hexdigest()
        if key in self._captions:
            return self._captions[key]
        if self.calls_left <= 0:
            if not self.warnings:
                self.warnings.append(
                    f"Reached max_llm_calls={self.calls}; the remaining images were "
                    "not captioned."
                )
            return None
        self.calls_left -= 1
        self.calls += 1
        self._captions[key] = None
        result = produce()
        self._captions[key] = result
        return result
