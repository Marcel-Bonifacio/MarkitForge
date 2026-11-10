# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import io
import posixpath
import re
import sys
import zipfile
from contextlib import contextmanager
from typing import BinaryIO, Any, Dict, Iterator, List, Optional, Tuple

from defusedxml import ElementTree as ET

from ._html_converter import HtmlConverter
from ..converter_utils.docx.core_properties import read_core_properties_from_zip
from .._base_converter import (
    DocumentConverter,
    DocumentConverterResult,
    DocumentSection,
    page_marker,
)
from .._exceptions import MissingDependencyException, MISSING_DEPENDENCY_MESSAGE
from .._stream_info import StreamInfo
from ..converter_utils._archive_limits import (
    ArchiveBudget,
    check_archive_stream,
    copy_member,
    get_budget,
    read_member,
)

# Try loading optional (but in this case, required) dependencies
# Save reporting of any exceptions for later
_xlsx_dependency_exc_info = None
try:
    import pandas as pd
    import openpyxl  # noqa: F401
except ImportError:
    _xlsx_dependency_exc_info = sys.exc_info()

_xls_dependency_exc_info = None
try:
    import pandas as pd  # noqa: F811
    import xlrd  # noqa: F401
except ImportError:
    _xls_dependency_exc_info = sys.exc_info()

ACCEPTED_XLSX_MIME_TYPE_PREFIXES = [
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
]
ACCEPTED_XLSX_FILE_EXTENSIONS = [".xlsx"]

ACCEPTED_XLS_MIME_TYPE_PREFIXES = [
    "application/vnd.ms-excel",
    "application/excel",
]
ACCEPTED_XLS_FILE_EXTENSIONS = [".xls"]


# Some producers write the legacy attribute "showZeroes" on <sheetView>, where the
# schema calls it "showZeros". openpyxl rejects the unknown attribute outright, so the
# workbook is repaired by renaming it. The rename is confined to <sheetView> start tags.
_SHEET_VIEW_START_TAG = re.compile(rb"<sheetView(?=[\s/>])[^>]*>")
_SHOW_ZEROES_ATTRIBUTE = re.compile(rb"(?<=[\s])showZeroes(\s*=)")


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_HIDDEN_STATES = ("hidden", "veryHidden")


def _read_workbook_info(
    file_stream: BinaryIO,
) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """Return ([(sheet name, state), ...] in workbook order, core properties).

    State is "visible", "hidden" or "veryHidden" (openpyxl's ``sheet_state``
    values), read straight from the package without loading any sheet. The
    stream position is restored. On any problem the sheet list is empty, so
    callers fall back to reading every sheet.
    """
    position = file_stream.tell()
    sheets: List[Tuple[str, str]] = []
    props: Dict[str, Any] = {}
    try:
        with zipfile.ZipFile(file_stream) as archive:
            props = read_core_properties_from_zip(archive)
            workbook_part = "xl/workbook.xml"
            try:
                rels = ET.fromstring(archive.read("_rels/.rels"))
                for rel in rels:
                    if rel.get("Type", "").endswith("/officeDocument"):
                        workbook_part = posixpath.normpath(
                            rel.get("Target", workbook_part)
                        ).lstrip("/")
                        break
            except Exception:
                pass
            root = ET.fromstring(archive.read(workbook_part))
            container = root.find(_SHEET_NS + "sheets")
            if container is not None:
                for sheet in container.findall(_SHEET_NS + "sheet"):
                    name = sheet.get("name")
                    if name is not None:
                        sheets.append((name, sheet.get("state") or "visible"))
    except Exception:
        sheets = []
    finally:
        file_stream.seek(position)
    return sheets, props


@contextmanager
def _read_xlsx_sheets(
    file_stream: BinaryIO,
    sheet_name: Optional[List[str]] = None,
    budget: Optional[ArchiveBudget] = None,
) -> Iterator[tuple[dict[str, Any], BinaryIO]]:
    if sheet_name is not None and not sheet_name:
        # Every sheet was filtered out; there is nothing to read.
        yield {}, file_stream
        return
    start_pos = file_stream.tell()
    repaired_stream = None
    try:
        try:
            sheets = pd.read_excel(
                file_stream, sheet_name=sheet_name, engine="openpyxl"
            )
        except TypeError as exc:
            if "showZeroes" not in str(exc):
                raise
            repaired_stream = _repair_sheetview_show_zeroes(
                file_stream, start_pos, budget
            )
            sheets = pd.read_excel(
                repaired_stream, sheet_name=sheet_name, engine="openpyxl"
            )
        yield sheets, repaired_stream if repaired_stream is not None else file_stream
    finally:
        if repaired_stream is not None:
            repaired_stream.close()


def _rename_show_zeroes_attribute(data: bytes) -> bytes:
    return _SHEET_VIEW_START_TAG.sub(
        lambda match: _SHOW_ZEROES_ATTRIBUTE.sub(rb"showZeros\1", match.group(0)),
        data,
    )


def _repair_sheetview_show_zeroes(
    file_stream: BinaryIO,
    start_pos: int = 0,
    budget: Optional[ArchiveBudget] = None,
) -> io.BytesIO:
    """Copy the workbook, renaming ``showZeroes`` in worksheet parts.

    Only worksheet parts are read into memory; every other member is
    stream-copied in chunks, within the archive member limits.
    """
    file_stream.seek(start_pos)
    repaired_stream = io.BytesIO()
    if budget is None:
        budget = ArchiveBudget()

    with zipfile.ZipFile(file_stream) as source:
        with zipfile.ZipFile(repaired_stream, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                budget.check_member(item)
                if item.filename.startswith(
                    "xl/worksheets/"
                ) and item.filename.endswith(".xml"):
                    data = read_member(source, item, budget)
                    if b"showZeroes" in data:
                        data = _rename_show_zeroes_attribute(data)
                    target.writestr(item, data)
                else:
                    copy_member(source, item, target, budget)

    repaired_stream.seek(0)
    return repaired_stream


def _select_sheets(
    states: List[Tuple[str, str]], include_hidden: bool
) -> Tuple[Optional[List[str]], List[str]]:
    """Return (sheet names to read, or None for all; hidden sheet names)."""
    hidden = [name for name, state in states if state in _HIDDEN_STATES]
    if not states or include_hidden:
        return None, hidden
    return [name for name, state in states if state not in _HIDDEN_STATES], hidden


def _hidden_sheet_warning(hidden: List[str], include_hidden: bool) -> List[str]:
    if not hidden or include_hidden:
        return []
    return [
        f"Skipped {len(hidden)} hidden sheet(s): {', '.join(hidden)}. "
        "Use include_hidden_sheets=True to include them."
    ]


# Whole numbers at or above this size are left to pandas' float formatting.
_MAX_EXACT_INT = 1e15


def _is_whole(value: Any) -> bool:
    return (
        isinstance(value, float)
        and value == value  # not NaN
        and value.is_integer()
        and abs(value) < _MAX_EXACT_INT
    )


def _format_float(value: float) -> str:
    return str(int(value)) if _is_whole(value) else format(value, ".15g")


def _whole_numbers_as_ints(frame: Any) -> Any:
    """Undo pandas' float upcasting of integer columns that contain blanks.

    A column of integers with an empty cell is read as float64, so 4 would
    print as "4.0". In float columns and in mixed (object) columns, whole
    numbers are printed as integers ("4") and fractions are kept ("4.5").
    Rewritten columns hold strings, with "" for empty cells.
    """
    out = None
    for position in range(len(frame.columns)):
        series = frame.iloc[:, position]
        kind = series.dtype.kind
        if kind == "f":
            if series.isna().all():
                continue
            cells = ["" if v != v else _format_float(float(v)) for v in series]
        elif kind == "O":
            if not any(_is_whole(v) for v in series):
                continue
            cells = [
                (
                    ""
                    if v is None or (isinstance(v, float) and v != v)
                    else str(int(v))
                    if _is_whole(v)
                    else v
                )
                for v in series
            ]
        else:
            continue
        if out is None:
            out = frame.copy()
        out.isetitem(position, pd.Series(cells, index=series.index, dtype=object))
    return frame if out is None else out


def _frame_to_html(frame: Any) -> str:
    # Empty cells must stay empty: na_rep covers NaN, None, NaT and pd.NA.
    return _whole_numbers_as_ints(frame).to_html(index=False, na_rep="")


class XlsxConverter(DocumentConverter):
    """
    Converts XLSX files to Markdown, with each sheet presented as a separate Markdown table.

    The workbook is checked against the archive resource limits before any
    part is read; a hit raises ``ResourceLimitException``.
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

        if extension in ACCEPTED_XLSX_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_XLSX_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        # Check the dependencies
        if _xlsx_dependency_exc_info is not None:
            raise MissingDependencyException(
                MISSING_DEPENDENCY_MESSAGE.format(
                    converter=type(self).__name__,
                    extension=".xlsx",
                    feature="xlsx",
                )
            ) from _xlsx_dependency_exc_info[1].with_traceback(  # type: ignore[union-attr]
                _xlsx_dependency_exc_info[2]
            )

        # Reject archive bombs before anything reads a member.
        budget = get_budget(kwargs)
        check_archive_stream(file_stream, budget)

        include_hidden = bool(kwargs.get("include_hidden_sheets", False))
        markers = bool(kwargs.get("page_markers", False))
        states, props = _read_workbook_info(file_stream)
        wanted, hidden = _select_sheets(states, include_hidden)

        md_content = ""
        sections: List[DocumentSection] = []
        with _read_xlsx_sheets(file_stream, wanted, budget) as (
            sheets,
            workbook_stream,
        ):
            images = None
            if type(self)._image_to_html is not XlsxConverter._image_to_html:
                from ..converter_utils._xlsx_images import _XlsxImages

                images = _XlsxImages(workbook_stream)

            for index, s in enumerate(sheets, start=1):
                chunk = f"## {s}\n"
                html_content = _frame_to_html(sheets[s])
                chunk += (
                    self._html_converter.convert_string(
                        html_content, **kwargs
                    ).markdown.strip()
                    + "\n\n"
                )
                if images is not None:
                    image_content = images.to_html(s, self._image_to_html, kwargs)
                    if image_content:
                        chunk += (
                            self._html_converter.convert_string(
                                image_content, **kwargs
                            ).markdown.strip()
                            + "\n\n"
                        )
                if markers:
                    md_content += page_marker(index, "sheet") + "\n"
                md_content += chunk
                sections.append(
                    DocumentSection(
                        index=index, markdown=chunk.strip(), title=str(s), kind="sheet"
                    )
                )

        metadata: Dict[str, Any] = dict(props)
        metadata["sheet_names"] = (
            [name for name, _ in states] if states else [str(s) for s in sheets]
        )
        metadata["hidden_sheets"] = hidden
        metadata["sheet_count"] = len(metadata["sheet_names"])
        return DocumentConverterResult(
            markdown=md_content.strip(),
            title=props.get("title"),
            metadata=metadata,
            pages=sections,
            warnings=_hidden_sheet_warning(hidden, include_hidden),
        )

    def _image_to_html(
        self,
        image_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> Optional[str]:
        """Override to render an embedded image as an HTML fragment.

        The stream is borrowed, seekable, and positioned at zero; do not close
        or retain it. StreamInfo describes the image, not the workbook. Existing
        conversion options are forwarded through kwargs.

        Return None or blank text to retain the native representation (no image
        output). Otherwise return HTML, escaping any literal text. Images appear
        after their sheet's table, in the existing worksheet image order. Linked
        images are not fetched. Hook failures propagate through the normal
        conversion failure path.
        """
        return None


class XlsConverter(DocumentConverter):
    """
    Converts XLS files to Markdown, with each sheet presented as a separate Markdown table.
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

        if extension in ACCEPTED_XLS_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_XLS_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        # Load the dependencies
        if _xls_dependency_exc_info is not None:
            raise MissingDependencyException(
                MISSING_DEPENDENCY_MESSAGE.format(
                    converter=type(self).__name__,
                    extension=".xls",
                    feature="xls",
                )
            ) from _xls_dependency_exc_info[1].with_traceback(  # type: ignore[union-attr]
                _xls_dependency_exc_info[2]
            )

        include_hidden = bool(kwargs.get("include_hidden_sheets", False))
        markers = bool(kwargs.get("page_markers", False))

        # Open the book once with xlrd so sheet visibility is known, then hand
        # the same Book to pandas. xlrd visibility: 0 visible, 1 hidden,
        # 2 very hidden.
        book = xlrd.open_workbook(file_contents=file_stream.read())
        states: List[Tuple[str, str]] = []
        for index in range(book.nsheets):
            visibility = getattr(book.sheet_by_index(index), "visibility", 0)
            state = {1: "hidden", 2: "veryHidden"}.get(visibility, "visible")
            states.append((book.sheet_names()[index], state))
        wanted, hidden = _select_sheets(states, include_hidden)
        author = (getattr(book, "user_name", "") or "").strip()

        sheets: Dict[str, Any] = (
            {}
            if wanted == []
            else pd.read_excel(book, sheet_name=wanted, engine="xlrd")
        )
        md_content = ""
        sections: List[DocumentSection] = []
        for index, s in enumerate(sheets, start=1):
            chunk = f"## {s}\n"
            html_content = _frame_to_html(sheets[s])
            chunk += (
                self._html_converter.convert_string(
                    html_content, **kwargs
                ).markdown.strip()
                + "\n\n"
            )
            if markers:
                md_content += page_marker(index, "sheet") + "\n"
            md_content += chunk
            sections.append(
                DocumentSection(
                    index=index, markdown=chunk.strip(), title=str(s), kind="sheet"
                )
            )

        metadata: Dict[str, Any] = {
            "sheet_names": [name for name, _ in states],
            "hidden_sheets": hidden,
            "sheet_count": len(states),
        }
        if author:
            metadata["author"] = author
        return DocumentConverterResult(
            markdown=md_content.strip(),
            metadata=metadata,
            pages=sections,
            warnings=_hidden_sheet_warning(hidden, include_hidden),
        )
