# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import zipfile
import io
import os

from typing import BinaryIO, Any, List, Optional, TYPE_CHECKING

from .._base_converter import DocumentConverter, DocumentConverterResult
from .._stream_info import StreamInfo
from .._exceptions import (
    FileConversionException,
    ResourceLimitException,
    UnsupportedFormatException,
)
from ..converter_utils._archive_limits import (
    BUDGET_KWARG,
    get_budget,
    limit_failure,
    read_member,
)

# Break otherwise circular import for type hinting
if TYPE_CHECKING:
    from .._markitforge import MarkitForge

ACCEPTED_MIME_TYPE_PREFIXES = [
    "application/zip",
]

ACCEPTED_FILE_EXTENSIONS = [".zip"]


class ZipConverter(DocumentConverter):
    """Converts ZIP files to markdown by extracting and converting all contained files.

    The converter extracts the ZIP contents to a temporary directory, processes each file
    using appropriate converters based on file extensions, and then combines the results
    into a single markdown document. The temporary directory is cleaned up after processing.

    Example output format:
    ```markdown
    Content from the zip file `example.zip`:

    ## File: docs/readme.txt

    This is the content of readme.txt
    Multiple lines are preserved

    ## File: images/example.jpg

    ImageSize: 1920x1080
    DateTimeOriginal: 2024-02-15 14:30:00
    Description: A beautiful landscape photo

    ## File: data/report.xlsx

    ## Sheet1
    | Column1 | Column2 | Column3 |
    |---------|---------|---------|
    | data1   | data2   | data3   |
    | data4   | data5   | data6   |
    ```

    Key features:
    - Maintains original file structure in headings
    - Processes nested files recursively
    - Uses appropriate converters for each file type
    - Preserves formatting of converted content
    - Cleans up temporary files after processing
    - Enforces archive resource limits (sizes, ratio, entries, nesting depth)
      across the whole recursion; limit hits become result warnings
    """

    def __init__(
        self,
        *,
        markitforge: "MarkitForge",
    ):
        super().__init__()
        self._markitforge = markitforge

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
        """Convert every member, within the archive resource limits.

        Limits (see ``converter_utils._archive_limits``) are shared by the
        whole recursion through ``kwargs["_archive_budget"]``. A limit hit
        never raises from here: it becomes a result warning. A member that is
        too large (or too compressed), or a nested archive beyond
        ``max_archive_depth``, is skipped (listed with its header only, and
        reported once with a count); once the document-wide byte or entry
        budget is spent, conversion stops and the members converted so far
        are returned. Warnings of nested members are passed up, prefixed with
        the member name. Every byte extracted at any level counts towards
        ``max_archive_bytes``. A member that is an archive itself (a nested
        ZIP, or a DOCX/XLSX/PPTX/EPUB package) is counted once: what it
        holds is charged only beyond the size already charged for the member.
        """
        file_path = (
            stream_info.url
            or stream_info.local_path
            or stream_info.filename
            or "(unknown)"
        )
        parts: List[str] = [f"Content from the zip file `{file_path}`:"]
        warnings: List[str] = []

        budget = get_budget(kwargs)
        limits = budget.limits
        if budget.exhausted is not None:
            warnings.append(f"Skipped `{file_path}`: {budget.exhausted}")
            return DocumentConverterResult(markdown=parts[0], warnings=warnings)
        if budget.depth >= limits.max_archive_depth:
            # Reported once per document; the outermost call adds a count.
            budget.depth_refusals += 1
            if budget.depth_refusals == 1:
                warnings.append(
                    f"Skipped `{file_path}`: archives nested more than "
                    f"{limits.max_archive_depth} deep (max_archive_depth)."
                )
            return DocumentConverterResult(markdown=parts[0], warnings=warnings)

        # Drop the metadata describing the outer *archive*
        member_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in ("file_extension", "url")
        }
        member_kwargs[BUDGET_KWARG] = budget

        budget.depth += 1
        try:
            with zipfile.ZipFile(file_stream, "r") as zipObj:
                for entry in zipObj.infolist():
                    if budget.exhausted is not None:
                        break
                    name = entry.filename
                    try:
                        budget.charge_entries(1)
                        budget.check_member(entry)
                        budget.charge_bytes(entry.file_size)
                        data = read_member(zipObj, entry, budget)
                    except ResourceLimitException as exc:
                        warnings.append(f"Skipped `{name}`: {exc}")
                        continue

                    result: Optional[DocumentConverterResult] = None
                    try:
                        with budget.prepaid(entry.file_size):
                            result = self._markitforge.convert_stream(
                                stream=io.BytesIO(data),
                                stream_info=StreamInfo(
                                    extension=os.path.splitext(name)[1],
                                    filename=os.path.basename(name),
                                ),
                                **member_kwargs,
                            )
                    except UnsupportedFormatException:
                        pass
                    except ResourceLimitException as exc:
                        # MarkitForge re-raises limit hits as-is; report and go on
                        # (the shared budget stops the loop once it is spent).
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
                    del data

                    if result is not None:
                        parts.append(f"## File: {name}\n\n{result.markdown}")
                        warnings.extend(f"{name}: {w}" for w in result.warnings)
        finally:
            budget.depth -= 1

        if budget.depth == 0 and budget.depth_refusals > 1:
            warnings.append(
                f"Skipped {budget.depth_refusals - 1} more archives nested more "
                f"than {limits.max_archive_depth} deep (max_archive_depth)."
            )
        return DocumentConverterResult(
            markdown="\n\n".join(parts).strip(), warnings=warnings
        )
