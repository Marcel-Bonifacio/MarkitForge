# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Legacy and OpenDocument office formats, converted through LibreOffice.

LibreOffice (``soffice --headless --convert-to``) turns the input into
.docx/.pptx/.xlsx, which is then handed to the matching MarkitForge converter,
so all the usual options (page_markers, table_format, include_hidden_sheets,
...) apply.

Hardening against hostile input:

- every extension maps to an explicit import filter (``--infilter``), so
  LibreOffice's content sniffing cannot switch a ".doc" or ".rtf" to its
  HTML import, which fetches linked stylesheets, images and frames;
- soffice runs without network access where the OS makes that cheap:
  ``sandbox-exec`` on macOS and an unprivileged network namespace
  (``unshare -rn``) on Linux. Set MARKITFORGE_LIBREOFFICE_SANDBOX=0 to turn
  this off.

Apple iWork files (.pages, .numbers, .key) go through LibreOffice's
libetonyek import filters ("Apple Pages", "Apple Numbers", "Apple Keynote").
They are ZIP packages, so only streams that start like a ZIP are accepted
(a ".key" PEM file is left to the text converter), and the package is
checked against the archive limits first. When LibreOffice is missing,
fails, or produces nothing, the preview the package carries
(``QuickLook/Preview.pdf`` from iWork '09, else ``preview.jpg``) is
converted through the parent MarkitForge instead, with a warning saying the
text comes from a preview.
"""

import functools
import io
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from .._base_converter import DocumentConverter, DocumentConverterResult
from .._exceptions import (
    ConversionCancelled,
    FileConversionException,
    MissingDependencyException,
    ResourceLimitException,
)
from .._stream_info import StreamInfo
from ..converter_utils._archive_limits import (
    check_archive_stream,
    get_budget,
    read_member,
)
from ._docx_converter import DocxConverter
from ._pptx_converter import PptxConverter
from ._xlsx_converter import XlsxConverter

logger = logging.getLogger(__name__)

# extension -> target family. Only formats LibreOffice imports reliably.
_TEXT_EXTENSIONS = (
    ".doc",
    ".dot",
    ".rtf",
    ".odt",
    ".ott",
    ".fodt",
    ".wpd",
    ".pages",
)
_PRESENTATION_EXTENSIONS = (".ppt", ".pps", ".pot", ".odp", ".otp", ".fodp", ".key")
_SPREADSHEET_EXTENSIONS = (".ods", ".ots", ".fods", ".xlsb", ".numbers")
#: Apple iWork packages (ZIP files).
_IWORK_EXTENSIONS = (".pages", ".numbers", ".key")

_MIME_TO_EXTENSION = {
    "application/msword": ".doc",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "application/x-rtf": ".rtf",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.text-template": ".ott",
    "application/vnd.wordperfect": ".wpd",
    "application/wordperfect": ".wpd",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/vnd.oasis.opendocument.presentation-template": ".otp",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.spreadsheet-template": ".ots",
    "application/vnd.ms-excel.sheet.binary.macroenabled.12": ".xlsb",
    "application/vnd.apple.pages": ".pages",
    "application/x-iwork-pages-sffpages": ".pages",
    "application/vnd.apple.numbers": ".numbers",
    "application/x-iwork-numbers-sffnumbers": ".numbers",
    "application/vnd.apple.keynote": ".key",
    "application/x-iwork-keynote-sffkey": ".key",
}

ACCEPTED_FILE_EXTENSIONS = (
    _TEXT_EXTENSIONS + _PRESENTATION_EXTENSIONS + _SPREADSHEET_EXTENSIONS
)

# extension -> LibreOffice import filter. Filter names checked against
# LibreOffice 26.8 (registry/*.xcd) by converting a sample of each format
# (the iWork ones with the templates Apple's iWork apps ship).
_INPUT_FILTERS: Dict[str, str] = {
    ".doc": "MS Word 97",
    ".dot": "MS Word 97 Vorlage",
    ".rtf": "Rich Text Format",
    ".odt": "writer8",
    ".ott": "writer8_template",
    ".fodt": "OpenDocument Text Flat XML",
    ".wpd": "WordPerfect",
    ".ppt": "MS PowerPoint 97",
    ".pps": "MS PowerPoint 97 AutoPlay",
    ".pot": "MS PowerPoint 97 Vorlage",
    ".odp": "impress8",
    ".otp": "impress8_template",
    ".fodp": "OpenDocument Presentation Flat XML",
    ".ods": "calc8",
    ".ots": "calc8_template",
    ".fods": "OpenDocument Spreadsheet Flat XML",
    ".xlsb": "Calc MS Excel 2007 Binary",
    ".pages": "Apple Pages",
    ".numbers": "Apple Numbers",
    ".key": "Apple Keynote",
}

#: Previews inside iWork packages, best first (compared case-insensitively).
_IWORK_PREVIEWS = (
    ("quicklook/preview.pdf", ".pdf", "application/pdf"),
    ("preview.jpg", ".jpg", "image/jpeg"),
    ("preview-web.jpg", ".jpg", "image/jpeg"),
    ("quicklook/thumbnail.jpg", ".jpg", "image/jpeg"),
)
_ZIP_MAGIC = b"PK\x03\x04"
_RTF_FILTER = "Rich Text Format"

_TARGETS: Dict[str, Tuple[str, str]] = {
    # family -> (--convert-to argument, output extension)
    "text": ("docx:MS Word 2007 XML", ".docx"),
    "presentation": ("pptx:Impress MS PowerPoint 2007 XML", ".pptx"),
    "spreadsheet": ("xlsx:Calc MS Excel 2007 XML", ".xlsx"),
}

DEFAULT_TIMEOUT = 120.0

INSTALL_HINT = (
    "LibreOfficeConverter needs LibreOffice to read {extension} files, but the "
    "'soffice' program was not found. Install LibreOffice "
    "(https://www.libreoffice.org/download/ ; macOS: 'brew install --cask "
    "libreoffice'; Debian/Ubuntu: 'apt install libreoffice-core "
    "libreoffice-writer libreoffice-calc libreoffice-impress') or point the "
    "MARKITFORGE_SOFFICE environment variable at the soffice executable."
)


def find_soffice() -> Optional[str]:
    """Locate the LibreOffice ``soffice`` executable, or return None."""
    override = os.environ.get("MARKITFORGE_SOFFICE")
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        found = shutil.which(override)
        if found:
            return found
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    candidates: List[str] = []
    if sys.platform == "darwin":
        candidates += [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            os.path.expanduser("~/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        ]
    elif sys.platform.startswith("win"):
        for root in (
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        ):
            candidates.append(
                os.path.join(root, "LibreOffice", "program", "soffice.exe")
            )
    else:
        candidates += [
            "/usr/bin/soffice",
            "/usr/lib/libreoffice/program/soffice",
            "/opt/libreoffice/program/soffice",
            "/snap/bin/libreoffice",
        ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _family(extension: str) -> Optional[str]:
    if extension in _TEXT_EXTENSIONS:
        return "text"
    if extension in _PRESENTATION_EXTENSIONS:
        return "presentation"
    if extension in _SPREADSHEET_EXTENSIONS:
        return "spreadsheet"
    return None


def _resolve_extension(stream_info: StreamInfo) -> Optional[str]:
    extension = (stream_info.extension or "").lower()
    if _family(extension):
        return extension
    mimetype = (stream_info.mimetype or "").lower().split(";")[0].strip()
    return _MIME_TO_EXTENSION.get(mimetype)


def _timeout(kwargs: Dict[str, Any]) -> float:
    value: Any = kwargs.get("libreoffice_timeout")
    if value is None:
        value = os.environ.get("MARKITFORGE_LIBREOFFICE_TIMEOUT")
    try:
        seconds = float(value) if value is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT
    return seconds if seconds > 0 else DEFAULT_TIMEOUT


def _input_filter(extension: str, head: bytes) -> str:
    """The import filter for a file. Word files saved as RTF are common, so a
    .doc/.dot that starts like RTF is read with the RTF filter; nothing else
    is sniffed."""
    if extension in (".doc", ".dot") and head.lstrip()[:5] == b"{\\rtf":
        return _RTF_FILTER
    return _INPUT_FILTERS[extension]


# ------------------------------------------------------- network isolation

_SANDBOX_ENV = "MARKITFORGE_LIBREOFFICE_SANDBOX"
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# soffice binds a local (unix-domain) pipe, so "(deny network*)" stops it
# from starting; deny IP and all outbound connections (which covers DNS via
# mDNSResponder) instead.
_MACOS_PROFILE = (
    "(version 1)(allow default)"
    "(deny network-outbound)"
    "(deny network-inbound (local ip))"
    "(deny network-bind (local ip))"
)
# soffice binary -> whether it works under the sandbox (absent: not known).
_SANDBOX_WORKS: Dict[str, bool] = {}

_PROBE_FODT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    b' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    b' office:version="1.2" office:mimetype="application/vnd.oasis.opendocument.text">'
    b"<office:body><office:text><text:p>probe</text:p></office:text></office:body>"
    b"</office:document>\n"
)


@functools.lru_cache(maxsize=None)
def _unshare_works() -> bool:
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    try:
        return (
            subprocess.run(
                [unshare, "-rn", "true"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _sandbox_prefix() -> List[str]:
    """Command prefix that runs soffice without network access, or []."""
    if os.environ.get(_SANDBOX_ENV, "").strip() == "0":
        return []
    if sys.platform == "darwin":
        if os.path.isfile(_SANDBOX_EXEC) and os.access(_SANDBOX_EXEC, os.X_OK):
            return [_SANDBOX_EXEC, "-p", _MACOS_PROFILE]
        return []
    if sys.platform.startswith("linux") and _unshare_works():
        unshare = shutil.which("unshare")
        if unshare:
            return [unshare, "-rn"]
    return []


def _kill(process: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, OSError):
        pass


def _soffice_args(
    profile: Path, infilter: str, convert_to: str, out_dir: Path, source: Path
) -> List[str]:
    return [
        # A private profile lets parallel conversions run side by side.
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--invisible",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        # Never let content sniffing pick the import filter.
        f"--infilter={infilter}",
        "--convert-to",
        convert_to,
        "--outdir",
        str(out_dir),
        str(source),
    ]


def _run(
    command: List[str], cwd: str, timeout: float, extension: str
) -> Tuple[int, bytes, bytes]:
    # A new session puts soffice (which the sandbox wrappers exec in place)
    # and anything it spawns in one process group, killed as a whole.
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(process)
        process.communicate()
        raise FileConversionException(
            f"LibreOffice did not finish converting the {extension} file "
            f"within {timeout:g} seconds (set libreoffice_timeout to allow more)."
        )
    return process.returncode, stdout, stderr


def _probe(soffice: str, prefix: List[str]) -> bool:
    """Whether soffice converts a trivial document when run under prefix."""
    with tempfile.TemporaryDirectory(prefix="markitforge-lo-probe-") as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "probe.fodt"
        source.write_bytes(_PROBE_FODT)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        args = _soffice_args(
            tmp_path / "profile",
            _INPUT_FILTERS[".fodt"],
            _TARGETS["text"][0],
            out_dir,
            source,
        )
        try:
            _run(prefix + [soffice] + args, tmp, 120.0, ".fodt")
        except (OSError, FileConversionException):
            return False
        return (out_dir / "probe.docx").is_file()


def _sandbox_breaks(soffice: str, sandbox: List[str]) -> bool:
    """After a failed sandboxed conversion: is the sandbox to blame?

    Only when a trivial document also fails under the sandbox but converts
    without it; a hostile file that merely fails in the sandbox never gets
    a second, unsandboxed run.
    """
    if _SANDBOX_WORKS.get(soffice) or _probe(soffice, sandbox):
        _SANDBOX_WORKS[soffice] = True
        return False
    if not _probe(soffice, []):
        return False  # soffice itself is broken; report the original error
    _SANDBOX_WORKS[soffice] = False
    return True


class LibreOfficeConverter(DocumentConverter):
    """Converts .doc/.rtf/.odt/.odp/.ppt/.ods/.xlsb (and related) files by
    having LibreOffice translate them to OOXML first."""

    def __init__(self) -> None:
        super().__init__()
        self._docx = DocxConverter()
        self._pptx = PptxConverter()
        self._xlsx = XlsxConverter()

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:
        # Accept even when soffice is missing, so the user gets an install hint
        # instead of a silent fallback to a less suitable converter.
        extension = _resolve_extension(stream_info)
        if extension is None:
            return False
        if extension in _IWORK_EXTENSIONS:
            # iWork documents are ZIP packages; ".key" is also a common name
            # for PEM keys, which must reach the text converter instead.
            position = file_stream.tell()
            try:
                head = file_stream.read(4)
            finally:
                file_stream.seek(position)
            return head == _ZIP_MAGIC
        return True

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:
        extension = _resolve_extension(stream_info)
        family = _family(extension or "")
        if extension is None or family is None:
            raise FileConversionException(
                f"LibreOfficeConverter cannot handle {stream_info.extension!r}."
            )

        if extension in _IWORK_EXTENSIONS:
            return self._convert_iwork(file_stream, stream_info, extension, kwargs)
        return self._convert_with_soffice(file_stream, stream_info, extension, kwargs)

    def _convert_with_soffice(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        extension: str,
        kwargs: Dict[str, Any],
    ) -> DocumentConverterResult:
        family = _family(extension)
        assert family is not None
        soffice = find_soffice()
        if not soffice:
            raise MissingDependencyException(INSTALL_HINT.format(extension=extension))

        convert_to, out_extension = _TARGETS[family]
        timeout = _timeout(kwargs)

        with tempfile.TemporaryDirectory(prefix="markitforge-lo-") as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / f"input{extension}"
            with open(source, "wb") as fh:
                shutil.copyfileobj(file_stream, fh)
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            profile = tmp_path / "profile"

            with open(source, "rb") as fh:
                head = fh.read(64)
            output = out_dir / f"input{out_extension}"
            args = _soffice_args(
                profile, _input_filter(extension, head), convert_to, out_dir, source
            )

            sandbox = [] if _SANDBOX_WORKS.get(soffice) is False else _sandbox_prefix()
            returncode, stdout, stderr = _run(
                sandbox + [soffice] + args, tmp, timeout, extension
            )
            if sandbox and output.is_file():
                _SANDBOX_WORKS[soffice] = True
            elif sandbox and _sandbox_breaks(soffice, sandbox):
                logger.debug(
                    "soffice does not work under %s; running it without the "
                    "network sandbox.",
                    sandbox[0],
                )
                returncode, stdout, stderr = _run(
                    [soffice] + args, tmp, timeout, extension
                )

            if not output.is_file():
                detail = (stderr or stdout or b"").decode("utf-8", "replace").strip()
                raise FileConversionException(
                    f"LibreOffice could not convert the {extension} file "
                    f"(exit code {returncode})"
                    + (f": {detail[-500:]}" if detail else ".")
                )

            delegate: DocumentConverter = {
                "text": self._docx,
                "presentation": self._pptx,
                "spreadsheet": self._xlsx,
            }[family]
            with open(output, "rb") as converted:
                result = delegate.convert(
                    converted,
                    StreamInfo(
                        extension=out_extension,
                        filename=(stream_info.filename or "document") + out_extension,
                    ),
                    **kwargs,
                )

        result.metadata.setdefault("source_format", extension.lstrip("."))
        result.metadata["converted_via"] = "libreoffice"
        return result

    # ------------------------------------------------------------- iWork

    def _convert_iwork(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        extension: str,
        kwargs: Dict[str, Any],
    ) -> DocumentConverterResult:
        # A package is read twice (LibreOffice, then maybe its preview); check
        # it against the archive limits once, before either.
        budget = get_budget(kwargs)
        start = file_stream.tell()
        check_archive_stream(file_stream, budget)
        data = file_stream.read()
        file_stream.seek(start)

        reason: str
        failure: Optional[BaseException] = None
        try:
            result = self._convert_with_soffice(
                io.BytesIO(data), stream_info, extension, kwargs
            )
            if result.markdown.strip():
                return result
            reason = "LibreOffice found no content in it"
        except (ResourceLimitException, ConversionCancelled):
            raise
        except MissingDependencyException as exc:
            failure = exc
            reason = "LibreOffice is not installed"
        except Exception as exc:
            failure = exc
            reason = f"LibreOffice could not import it ({exc})"

        preview = _iwork_preview(data, budget)
        if preview is None:
            if failure is not None:
                raise failure
            return result
        name, preview_ext, preview_mime, preview_data = preview
        preview_result = _convert_with_parents(
            preview_data,
            StreamInfo(
                extension=preview_ext,
                mimetype=preview_mime,
                filename=os.path.basename(name),
            ),
            kwargs,
        )
        preview_result.warnings.insert(
            0,
            f"Converted the preview image of this {extension} document "
            f"({name}), not the document itself: {reason}. The preview may "
            "show only the first page or slide and holds no editable text "
            "structure.",
        )
        preview_result.metadata["source_format"] = extension.lstrip(".")
        preview_result.metadata["converted_via"] = "preview"
        preview_result.metadata["preview_part"] = name
        return preview_result


def _iwork_preview(data: bytes, budget: Any) -> Optional[Tuple[str, str, str, bytes]]:
    """(part name, extension, media type, bytes) of the package's best preview."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        return None
    with archive:
        infos = {info.filename.lower(): info for info in archive.infolist()}
        for wanted, extension, mime in _IWORK_PREVIEWS:
            info = infos.get(wanted)
            if info is None or info.is_dir():
                continue
            return info.filename, extension, mime, read_member(archive, info, budget)
    return None


def _convert_with_parents(
    data: bytes, stream_info: StreamInfo, kwargs: Dict[str, Any]
) -> DocumentConverterResult:
    """Convert ``data`` with the parent MarkitForge's converters, best first.

    Falls back to the built-in PDF/image converters when this converter is
    used on its own. Limit hits and cancellation propagate.
    """
    registrations = kwargs.get("_parent_converters") or []
    converters: List[DocumentConverter] = [
        r.converter for r in sorted(registrations, key=lambda r: r.priority)
    ]
    if not converters:
        from ._image_converter import ImageConverter
        from ._pdf_converter import PdfConverter

        converters = [PdfConverter(), ImageConverter()]
    options = {k: v for k, v in kwargs.items() if k not in ("file_extension", "url")}
    errors: List[str] = []
    for converter in converters:
        if isinstance(converter, LibreOfficeConverter):
            continue
        stream = io.BytesIO(data)
        try:
            if not converter.accepts(stream, stream_info, **options):
                continue
        except NotImplementedError:
            continue
        stream.seek(0)
        try:
            return converter.convert(stream, stream_info, **options)
        except (ResourceLimitException, ConversionCancelled):
            raise
        except Exception as exc:
            errors.append(f"{type(converter).__name__}: {exc}")
    raise FileConversionException(
        "Could not convert the document preview"
        + (": " + "; ".join(errors) if errors else ".")
    )
