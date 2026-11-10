# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The Starlette web app behind ``markitforge-gui``.

The server is meant to run on the user's own machine and be used from their
own browser. Hardening for that setting:

* Only loopback ``Host`` headers are accepted by default (blocks DNS rebinding).
* Every state-changing ``POST`` must carry the ``X-MarkitForge-Client`` header
  and, when the browser sends an ``Origin``, it must be this server. A plain
  cross-site form post cannot set a custom header, so other websites cannot
  drive the converter.
* Request bodies are capped (``max_upload_bytes``).
* Rendered HTML is sanitized server-side and the page ships with a strict CSP.
"""

import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unicodedata
import zipfile
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse

import anyio
import requests
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from markitforge import (
    DocumentConverterResult,
    FileConversionException,
    MarkitForge,
    MissingDependencyException,
    ResourceLimitException,
    StreamInfo,
    UnsupportedFormatException,
)
from markitforge import __version__ as markitforge_version

from .__about__ import __version__
from ._render import render_markdown
from ._store import ResultStore, StoredResult

__all__ = [
    "create_app",
    "parse_options",
    "CLIENT_HEADER",
    "DEFAULT_MAX_UPLOAD_MB",
    "LOOPBACK_HOSTS",
]

logger = logging.getLogger(__name__)

DEFAULT_MAX_UPLOAD_MB = 200
CLIENT_HEADER = "X-MarkitForge-Client"
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]", "::1"]
STATIC_DIR = Path(__file__).parent / "static"

# Multipart framing (boundaries, headers, the options field) on top of the file.
_MULTIPART_OVERHEAD = 1024 * 1024
_MAX_URL_LENGTH = 4096

_BOOL_OPTIONS = (
    "page_markers",
    "include_hidden_sheets",
    "include_headers_footers",
    "keep_data_uris",
)
_CHOICE_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "table_format": ("markdown", "html", "auto"),
    "pdf_backend": ("auto", "pdfplumber", "pdfminer", "docling"),
}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


class UploadTooLarge(Exception):
    """The request body is bigger than the configured limit."""


class OptionsError(ValueError):
    """The conversion options sent by the client are not valid."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def parse_options(raw: Any) -> Dict[str, Any]:
    """Validate client-supplied options against an allow-list.

    Unknown keys are ignored; known keys with bad values raise OptionsError.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OptionsError("The options could not be read (invalid JSON).") from exc
    if not isinstance(raw, dict):
        raise OptionsError("The options must be a JSON object.")

    options: Dict[str, Any] = {}
    for key in _BOOL_OPTIONS:
        if key in raw and raw[key] is not None:
            if not isinstance(raw[key], bool):
                raise OptionsError(f"Option '{key}' must be true or false.")
            options[key] = raw[key]
    for key, choices in _CHOICE_OPTIONS.items():
        if key in raw and raw[key] is not None:
            if raw[key] not in choices:
                raise OptionsError(
                    f"Option '{key}' must be one of: {', '.join(choices)}."
                )
            options[key] = raw[key]
    return options


def _safe_stem(name: str) -> str:
    """A filesystem- and header-safe base name (no extension)."""
    base = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if "." in base.strip("."):
        base = base.rsplit(".", 1)[0]
    base = unicodedata.normalize("NFKC", base)
    base = re.sub(r"[^\w\-. ()]+", "_", base)
    base = re.sub(r"_+", "_", base).strip(" ._")
    return base[:120].rstrip(" ._") or "document"


def _safe_extension(name: str) -> str:
    """The original extension, if it looks like a normal one (e.g. '.docx')."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    _, ext = os.path.splitext(base)
    if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
        return ext.lower()
    return ""


def _md_name_for_url(url: str, title: Optional[str]) -> str:
    if title and title.strip():
        return _safe_stem(title.strip() + ".x") + ".md"
    parsed = urlparse(url)
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    stem = f"{parsed.hostname or 'page'}-{last}" if last else (parsed.hostname or "")
    return _safe_stem(stem + ".x") + ".md"


def content_disposition(filename: str) -> str:
    """``attachment`` header with an ASCII fallback and an RFC 5987 UTF-8 name."""
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    )
    ascii_name = re.sub(r"[^A-Za-z0-9\-_. ()]+", "_", ascii_name).strip(" ._")
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = "document" + (ascii_name if ascii_name.startswith(".") else "")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _error(
    status: int,
    message: str,
    *,
    hint: Optional[str] = None,
    detail: Optional[str] = None,
) -> JSONResponse:
    payload: Dict[str, Any] = {"error": message}
    if hint:
        payload["hint"] = hint
    if detail:
        payload["detail"] = detail[:1000]
    return JSONResponse(payload, status_code=status)


def _missing_dependency_in(exc: BaseException) -> Optional[BaseException]:
    if isinstance(exc, MissingDependencyException):
        return exc
    if isinstance(exc, FileConversionException) and exc.attempts:
        for attempt in exc.attempts:
            if attempt.exc_info and isinstance(
                attempt.exc_info[1], MissingDependencyException
            ):
                return attempt.exc_info[1]
    return None


def error_for_exception(exc: BaseException) -> JSONResponse:
    """Turn a conversion failure into a friendly JSON error."""
    missing = _missing_dependency_in(exc)
    if missing is not None:
        return _error(
            422,
            "An extra component is needed to read this file.",
            hint=str(missing).strip(),
        )
    if isinstance(exc, UnsupportedFormatException):
        return _error(
            415,
            "This file type is not supported yet.",
            hint="Try a PDF, Word, PowerPoint, Excel, HTML, CSV, JSON, XML, "
            "EPUB, ZIP, image or audio file.",
        )
    if isinstance(exc, ResourceLimitException):
        return _error(
            413,
            "This file is too large or too complex to convert safely.",
            hint=str(exc),
        )
    if isinstance(exc, FileConversionException):
        detail = None
        if exc.attempts:
            first = exc.attempts[0]
            if first.exc_info and first.exc_info[1] is not None:
                detail = f"{type(first.exc_info[1]).__name__}: {first.exc_info[1]}"
        return _error(
            422,
            "We couldn't convert this file. It may be damaged, password-protected, "
            "or not what its name suggests.",
            detail=detail,
        )
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        suffix = f" (the site answered with error {status})" if status else ""
        return _error(502, f"The web page could not be downloaded{suffix}.")
    if isinstance(exc, requests.exceptions.RequestException):
        return _error(
            502,
            "Could not reach that address. Check the link and your internet "
            "connection.",
        )
    if isinstance(exc, OptionsError):
        return _error(400, str(exc))
    logger.exception("Unexpected conversion failure", exc_info=exc)
    return _error(
        500,
        "Something went wrong while converting this file.",
        detail=f"{type(exc).__name__}: {exc}",
    )


def _result_payload(entry: StoredResult) -> Dict[str, Any]:
    result = entry.result
    data = result.to_dict(include_markdown=False)
    return {
        "id": entry.id,
        "filename": entry.source_name,
        "md_filename": entry.md_filename,
        "title": result.title,
        "markdown": result.markdown,
        "html": render_markdown(result.markdown),
        "metadata": data["metadata"],
        "pages": [
            {"index": p["index"], "kind": p["kind"], "title": p["title"]}
            for p in data["pages"]
        ],
        "warnings": data["warnings"],
        "assets": [
            {
                "name": a["name"],
                "mime_type": a["mime_type"],
                "size": a["size"],
                "description": a["description"],
            }
            for a in data["assets"]
        ],
    }


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------


class BodyLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with a 413."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > self.max_bytes:
                    response = _too_large_response(self.max_bytes)
                    await response(scope, receive, send)
                    return
                break

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise UploadTooLarge()
            return message

        await self.app(scope, limited_receive, send)


def _too_large_response(max_bytes: int) -> JSONResponse:
    limit_mb = max(1, round((max_bytes - _MULTIPART_OVERHEAD) / (1024 * 1024)))
    return _error(
        413,
        f"This file is too large. The limit is {limit_mb} MB.",
        hint="Start MarkitForge with --max-upload-mb to raise the limit.",
    )


class SecurityHeadersMiddleware:
    """Add conservative security headers to every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"content-security-policy", CSP.encode()),
                ]
                for key, value in extra:
                    if key not in present:
                        headers.append((key, value))
                if scope["path"].startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------


def create_app(
    *,
    max_upload_mb: float = DEFAULT_MAX_UPLOAD_MB,
    allowed_hosts: Optional[Sequence[str]] = None,
    store_capacity: int = 50,
    converter_factory: Optional[Callable[[], MarkitForge]] = None,
    max_workers: Optional[int] = None,
) -> Starlette:
    """Build the GUI web app.

    Args:
        max_upload_mb: largest accepted upload, in megabytes.
        allowed_hosts: accepted ``Host`` header values (default: loopback only;
            ``["*"]`` disables the check).
        store_capacity: how many results to keep in memory (oldest dropped).
        converter_factory: builds a MarkitForge instance (one per worker thread).
        max_workers: how many conversions may run at the same time.
    """
    max_upload_bytes = int(max_upload_mb * 1024 * 1024)
    body_limit = max_upload_bytes + _MULTIPART_OVERHEAD
    store = ResultStore(store_capacity)
    hosts = list(allowed_hosts) if allowed_hosts else list(LOOPBACK_HOSTS)
    if "*" in hosts:
        raise ValueError("allowed_hosts may not contain '*' (DNS rebinding risk).")
    allowed_origin_hosts = {h.strip("[]").lower() for h in hosts}
    factory = converter_factory or MarkitForge
    local = threading.local()
    workers = max_workers or max(2, min(4, os.cpu_count() or 2))
    limiter_box: Dict[str, anyio.CapacityLimiter] = {}

    def converter() -> MarkitForge:
        conv = getattr(local, "converter", None)
        if conv is None:
            conv = factory()
            local.converter = conv
        return conv

    async def in_thread(func: Callable[[], Any]) -> Any:
        limiter = limiter_box.get("limiter")
        if limiter is None:
            limiter = limiter_box["limiter"] = anyio.CapacityLimiter(workers)
        return await anyio.to_thread.run_sync(func, limiter=limiter)

    def check_client(request: Request) -> Optional[JSONResponse]:
        if request.headers.get(CLIENT_HEADER) != "1":
            return _error(403, "This request did not come from the MarkitForge page.")
        origin = request.headers.get("origin")
        if origin is not None:
            host = request.headers.get("host", "")
            parsed = urlparse(origin)
            # Same origin AND a host on our allow-list: a DNS-rebinding page
            # controls both Host and Origin, but not the allow-list.
            if (
                origin == "null"
                or parsed.netloc != host
                or (parsed.hostname or "").lower() not in allowed_origin_hosts
            ):
                return _error(403, "Requests from other websites are not allowed.")
        return None

    async def read_json(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OptionsError("The request could not be read (invalid JSON).") from exc
        if not isinstance(body, dict):
            raise OptionsError("The request must be a JSON object.")
        return body

    async def finish(
        source_name: str, md_filename: str, result: DocumentConverterResult
    ) -> JSONResponse:
        # Remember asset sizes, then free the (possibly large) binary payloads:
        # the GUI lists assets but never serves their bytes.
        sizes = [len(a.data) if a.data is not None else None for a in result.assets]
        for asset in result.assets:
            asset.data = None
        entry = store.add(source_name, md_filename, result)
        # Rendering a big document takes seconds: keep it off the event loop.
        payload = await in_thread(partial(_result_payload, entry))
        for out, size in zip(payload["assets"], sizes):
            out["size"] = size
        return JSONResponse(payload)

    # ---- routes -----------------------------------------------------------

    async def index(request: Request) -> Response:
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"cache-control": "no-cache"},
        )

    async def info(request: Request) -> Response:
        return JSONResponse(
            {
                "version": __version__,
                "markitforge_version": markitforge_version,
                "max_upload_mb": max_upload_mb,
                "max_upload_bytes": max_upload_bytes,
            }
        )

    async def convert_upload(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied

        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            return _error(400, "Please send the file as a multipart form upload.")

        async with request.form(max_files=1, max_fields=8) as form:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                return _error(400, "No file was received. Please choose a file.")
            raw_options = form.get("options")
            if raw_options is not None and not isinstance(raw_options, str):
                return _error(400, "The options must be sent as text.")
            options = parse_options(raw_options)

            original = upload.filename or "document"
            md_filename = _safe_stem(original) + ".md"
            suffix = _safe_extension(original)
            fd, tmp_path = tempfile.mkstemp(prefix="markitforge-gui-", suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as fh:
                    await in_thread(
                        partial(shutil.copyfileobj, upload.file, fh, 1024 * 1024)
                    )
                await upload.close()
                stream_info = StreamInfo(
                    filename=_safe_stem(original) + suffix,
                    extension=suffix or None,
                )

                def run() -> DocumentConverterResult:
                    return converter().convert_local(
                        tmp_path, stream_info=stream_info, **options
                    )

                try:
                    result = await in_thread(run)
                except Exception as exc:  # noqa: BLE001 - mapped to friendly JSON
                    return error_for_exception(exc)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("Could not delete temporary file %s", tmp_path)

        return await finish(original, md_filename, result)

    async def convert_url(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        body = await read_json(request)
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            return _error(400, "Please enter a web address.")
        url = url.strip()
        if "://" not in url and re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return _error(
                400, "Please enter a full web address starting with https:// or http://"
            )
        if len(url) > _MAX_URL_LENGTH:
            return _error(400, "That web address is too long.")
        options = parse_options(body.get("options"))

        def run() -> DocumentConverterResult:
            # Same size cap for web pages as for uploads.
            return converter().convert_uri(
                url, max_download_bytes=max_upload_bytes, **options
            )

        try:
            result = await in_thread(run)
        except Exception as exc:  # noqa: BLE001 - mapped to friendly JSON
            return error_for_exception(exc)
        return await finish(url, _md_name_for_url(url, result.title), result)

    async def render(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        body = await read_json(request)
        item_id = body.get("id")
        if item_id is not None:
            if not isinstance(item_id, str):
                return _error(400, "Unknown result id.")
            entry = store.get(item_id)
            if entry is None:
                return _error(
                    404, "This result is no longer available. Please convert it again."
                )
            pages = body.get("pages")
            if pages is None or (isinstance(pages, str) and not pages.strip()):
                markdown = entry.result.markdown
            elif not isinstance(pages, str):
                return _error(400, "The page range must be text, e.g. 1-3,5")
            else:
                try:
                    markdown = entry.result.select_pages(pages)
                except ValueError as exc:
                    if not entry.result.pages:
                        return _error(
                            400, "This document has no page information to select from."
                        )
                    return _error(
                        400,
                        "That page range was not understood. Use numbers like 1-3,5 or 8-",
                        detail=str(exc),
                    )
        else:
            raw_markdown = body.get("markdown")
            if not isinstance(raw_markdown, str):
                return _error(400, "Nothing to show: no Markdown was sent.")
            markdown = raw_markdown
        html = await in_thread(partial(render_markdown, markdown))
        return JSONResponse({"markdown": markdown, "html": html})

    async def download(request: Request) -> Response:
        entry = store.get(request.path_params["item_id"])
        if entry is None:
            return _error(
                404, "This result is no longer available. Please convert it again."
            )
        markdown = entry.result.markdown
        filename = entry.md_filename
        pages = request.query_params.get("pages", "").strip()
        if pages:
            try:
                markdown = entry.result.select_pages(pages)
            except ValueError:
                return _error(400, "That page range was not understood.")
            filename = filename[: -len(".md")] + f" (pages {_safe_stem(pages)}).md"
        return Response(
            markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"content-disposition": content_disposition(filename)},
        )

    async def download_zip(request: Request) -> Response:
        ids_param = request.query_params.get("ids", "").strip()
        if ids_param:
            entries: List[StoredResult] = []
            for item_id in ids_param.split(","):
                entry = store.get(item_id.strip())
                if entry is not None:
                    entries.append(entry)
        else:
            entries = store.all()
        if not entries:
            return _error(404, "There is nothing to download yet.")

        def build() -> bytes:
            buf = io.BytesIO()
            used: Dict[str, int] = {}
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for entry in entries:
                    name = entry.md_filename
                    count = used.get(name.lower(), 0)
                    used[name.lower()] = count + 1
                    if count:
                        name = f"{name[: -len('.md')]} ({count + 1}).md"
                        used[name.lower()] = 1
                    zf.writestr(name, entry.result.markdown)
            return buf.getvalue()

        data = await in_thread(build)
        return Response(
            data,
            media_type="application/zip",
            headers={
                "content-disposition": content_disposition("markitforge-results.zip")
            },
        )

    # ---- exception handlers -----------------------------------------------

    async def handle_too_large(request: Request, exc: Exception) -> Response:
        return _too_large_response(body_limit)

    async def handle_options(request: Request, exc: Exception) -> Response:
        return _error(400, str(exc))

    async def handle_http(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        if request.url.path.startswith("/api/"):
            message = {
                404: "Not found.",
                405: "This action is not allowed here.",
            }.get(exc.status_code, str(exc.detail))
            return _error(exc.status_code, message)
        return Response(str(exc.detail), status_code=exc.status_code)

    middleware = [
        Middleware(SecurityHeadersMiddleware),
        Middleware(TrustedHostMiddleware, allowed_hosts=hosts),
        Middleware(BodyLimitMiddleware, max_bytes=body_limit),
    ]
    handlers: Dict[Any, Callable[[Request, Exception], Awaitable[Response]]] = {
        UploadTooLarge: handle_too_large,
        OptionsError: handle_options,
        HTTPException: handle_http,
    }
    app = Starlette(
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/api/info", info, methods=["GET"]),
            Route("/api/convert", convert_upload, methods=["POST"]),
            Route("/api/convert-url", convert_url, methods=["POST"]),
            Route("/api/render", render, methods=["POST"]),
            Route("/api/download/{item_id}", download, methods=["GET"]),
            Route("/api/zip", download_zip, methods=["GET"]),
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        middleware=middleware,
        exception_handlers=handlers,
    )
    app.state.store = store
    app.state.max_upload_bytes = max_upload_bytes
    return app
