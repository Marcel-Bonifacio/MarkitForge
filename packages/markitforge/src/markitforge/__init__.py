# SPDX-FileCopyrightText: 2024-present Adam Fourney <adamfo@microsoft.com>
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

from .__about__ import __version__
from ._markitforge import (
    MarkitForge,
    PRIORITY_SPECIFIC_FILE_FORMAT,
    PRIORITY_GENERIC_FILE_FORMAT,
)
from ._base_converter import (
    DocumentConverterResult,
    DocumentConverter,
    DocumentSection,
    DocumentAsset,
    join_sections,
    page_marker,
    parse_page_range,
)
from ._stream_info import StreamInfo
from ._progress import ProgressEvent, report_progress, check_cancelled
from ._net import BlockedURLException, check_public_url
from ._exceptions import (
    MarkitForgeException,
    MissingDependencyException,
    FailedConversionAttempt,
    FileConversionException,
    UnsupportedFormatException,
    ResourceLimitException,
    ConversionCancelled,
)

# Backward-compatible names for code written against upstream MarkItDown.
MarkItDown = MarkitForge
MarkItDownException = MarkitForgeException

__all__ = [
    "MarkItDown",
    "MarkItDownException",
    "__version__",
    "MarkitForge",
    "DocumentConverter",
    "DocumentConverterResult",
    "DocumentSection",
    "DocumentAsset",
    "join_sections",
    "page_marker",
    "parse_page_range",
    "MarkitForgeException",
    "MissingDependencyException",
    "FailedConversionAttempt",
    "FileConversionException",
    "UnsupportedFormatException",
    "ResourceLimitException",
    "ConversionCancelled",
    "ProgressEvent",
    "report_progress",
    "check_cancelled",
    "BlockedURLException",
    "check_public_url",
    "StreamInfo",
    "PRIORITY_SPECIFIC_FILE_FORMAT",
    "PRIORITY_GENERIC_FILE_FORMAT",
]
