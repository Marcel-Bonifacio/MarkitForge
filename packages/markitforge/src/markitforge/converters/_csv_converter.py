# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import csv
import io
import re
from collections import Counter
from typing import BinaryIO, Any, List
from charset_normalizer import from_bytes
from .._base_converter import DocumentConverter, DocumentConverterResult
from .._stream_info import StreamInfo

ACCEPTED_MIME_TYPE_PREFIXES = [
    "text/csv",
    "application/csv",
    "text/tab-separated-values",
    "text/tsv",
]
ACCEPTED_FILE_EXTENSIONS = [".csv", ".tsv"]

_TSV_EXTENSIONS = (".tsv",)
_TSV_MIME_PREFIXES = ("text/tab-separated-values", "text/tsv")
# Pipes are deliberately not sniffed: a single-column CSV whose values contain
# pipes must stay a single column.
_CANDIDATE_DELIMITERS = (",", "\t", ";")
_SNIFF_LINES = 50


# Matches a pipe together with the (possibly empty) run of backslashes in front
# of it, so that run can be doubled before the pipe is escaped.
# The lookbehind avoids retrying from each position inside a backslash run.
_PIPE_ESCAPE_RE = re.compile(r"(?<!\\)(\\*)\|")


def _escape_table_cell(value: str) -> str:
    r"""Escape a CSV value so it is safe inside a Markdown table cell.

    A pipe is a column separator, so it must be escaped.
    Line breaks would end the row early, so they collapse to a single space.
    """
    value = _PIPE_ESCAPE_RE.sub(lambda m: m.group(1) * 2 + r"\|", value)
    return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _trim_outer_blank_rows(rows: list[list[str]]) -> None:
    """Remove empty rows from the beginning and end, and immediately after the header. This operation is performed in-place."""
    start = 0
    while start < len(rows) and not rows[start]:
        start += 1

    if start == len(rows):
        rows.clear()
        return

    header_index = start
    start += 1
    while start < len(rows) and not rows[start]:
        start += 1

    end = len(rows)
    while end > start and not rows[end - 1]:
        end -= 1

    # Remove each blank run at once, rather than shifting the list per row.
    del rows[end:]
    del rows[header_index + 1 : start]
    del rows[:header_index]


def _column_profile(sample: str, delimiter: str) -> tuple[int, float]:
    """Return (modal column count, share of rows with that count) for a sample."""
    try:
        rows = [
            r
            for r in csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter)
            if r
        ]
    except csv.Error:
        return 1, 0.0
    if not rows:
        return 1, 0.0
    counts = Counter(len(r) for r in rows)
    width, freq = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return width, freq / len(rows)


def _sniff_delimiter(content: str, default: str) -> str:
    """Pick the delimiter for ``content``.

    The default delimiter (comma for CSV, tab for TSV) is kept whenever it
    splits the data into more than one column, so ordinary files convert
    exactly as before. Only when it yields a single column do we look for a
    candidate (comma, tab, semicolon) that splits nearly every row into the same
    number of columns.
    """
    lines: List[str] = []
    for line in content.splitlines():
        if line.strip():
            lines.append(line)
        if len(lines) >= _SNIFF_LINES:
            break
    sample = "\n".join(lines)
    if not sample:
        return default

    width, _ = _column_profile(sample, default)
    if width > 1:
        return default

    best = default
    best_score = (0.0, 1)
    for delimiter in _CANDIDATE_DELIMITERS:
        if delimiter == default or delimiter not in sample:
            continue
        width, share = _column_profile(sample, delimiter)
        if width > 1 and share >= 0.9 and (share, width) > best_score:
            best, best_score = delimiter, (share, width)
    return best


class CsvConverter(DocumentConverter):
    """
    Converts CSV and TSV files to Markdown tables. The delimiter is sniffed
    when the default one (comma, or tab for .tsv) does not split the data.
    """

    def __init__(self):
        super().__init__()

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
        # Read the file content
        if stream_info.charset:
            content = file_stream.read().decode(stream_info.charset)
        else:
            data = file_stream.read()
            detected = from_bytes(data).best()
            content = (
                str(detected)
                if detected is not None
                else data.decode("utf-8", errors="ignore")
            )

        # Excel and other tools prepend a UTF-8 BOM to CSV exports; strip it so
        # it does not end up inside the first header cell.
        content = content.lstrip("\ufeff")

        # Parse CSV content
        extension = (stream_info.extension or "").lower()
        mimetype = (stream_info.mimetype or "").lower()
        default = (
            "\t"
            if extension in _TSV_EXTENSIONS or mimetype.startswith(_TSV_MIME_PREFIXES)
            else ","
        )
        delimiter = _sniff_delimiter(content, default)
        reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter)
        rows = list(reader)
        _trim_outer_blank_rows(rows)

        if not rows:
            return DocumentConverterResult(markdown="")

        # Pad all rows, including the header, to preserve the widest row.
        num_columns = max(len(row) for row in rows)
        for row in rows:
            row.extend([""] * (num_columns - len(row)))

        # Create markdown table
        markdown_table = []

        # Add header row
        header = [_escape_table_cell(cell) for cell in rows[0]]
        markdown_table.append("| " + " | ".join(header) + " |")

        # Add separator row
        markdown_table.append("| " + " | ".join(["---"] * num_columns) + " |")

        # Add data rows
        for row in rows[1:]:
            markdown_table.append(
                "| " + " | ".join(_escape_table_cell(cell) for cell in row) + " |"
            )

        result = "\n".join(markdown_table)

        return DocumentConverterResult(markdown=result)
