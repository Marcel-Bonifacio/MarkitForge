# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The Starlette web app behind ``markitforge-gui``.

The server is meant to run on the user's own machine and be used from their
own browser. Hardening for that setting:

* Only loopback ``Host`` headers are accepted by default (blocks DNS rebinding).
* Every ``/api`` request (GETs included) must carry the per-launch session
  token: the ``X-MarkitForge-Token`` header, or ``?t=`` on GET links such as
  downloads. The token is put into the page (a ``<meta>`` tag) when it is
  served; other websites cannot read it. Tokens are compared in constant time.
* Every state-changing request (``POST``/``PUT``/``DELETE``) must also carry
  the ``X-MarkitForge-Client`` header and, when the browser sends an
  ``Origin``, it must be this server.
* Request bodies are capped (``max_upload_bytes``), edits too.
* Rendered HTML is sanitized server-side and the page ships with a strict CSP
  (no inline script or style). The original-file view gets its own CSP.
* Web addresses are fetched with ``block_private_networks=True`` (no requests
  to this computer or the local network) unless ``allow_private_urls``.
* An AI API key typed into the page stays in memory only (see ``_llm``).
"""

import base64
import contextlib
import html as html_lib
import io
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import unicodedata
import zipfile
from functools import partial
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import parse_qs, quote, urlparse

import anyio
import requests
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from markitforge import (
    BlockedURLException,
    ConversionCancelled,
    DocumentConverterResult,
    FileConversionException,
    MarkitForge,
    MissingDependencyException,
    ResourceLimitException,
    StreamInfo,
    UnsupportedFormatException,
)
from markitforge import __version__ as markitforge_version
from markitforge._config import Config, load_config

from .__about__ import __version__
from ._history import HistoryStore
from ._jobs import Job, JobManager, TooManyJobs
from ._llm import LLMSettings, LLMSettingsError
from ._original import (
    MAX_ORIGINAL_BYTES,
    PreviewUnavailable,
    decode_text,
    image_type,
    office_to_pdf,
    original_kind,
)
from ._render import render_markdown
from ._store import ResultStore, StoredResult, new_id, safe_asset_files, valid_id

__all__ = [
    "create_app",
    "parse_options",
    "CLIENT_HEADER",
    "TOKEN_HEADER",
    "DEFAULT_MAX_UPLOAD_MB",
    "LOOPBACK_HOSTS",
    "MAX_EDIT_BYTES",
    "MAX_FOLDER_FILES",
    "safe_rel_dir",
    "rewrite_image_links",
    "error_details",
]

logger = logging.getLogger(__name__)

DEFAULT_MAX_UPLOAD_MB = 200
CLIENT_HEADER = "X-MarkitForge-Client"
TOKEN_HEADER = "X-MarkitForge-Token"
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]", "::1"]
STATIC_DIR = Path(__file__).parent / "static"
TOKEN_META = '<meta name="mf-token" content="">'

# Multipart framing (boundaries, headers, the options field) on top of the file.
_MULTIPART_OVERHEAD = 1024 * 1024
_MAX_URL_LENGTH = 4096
MAX_EDIT_BYTES = 32 * 1024 * 1024
MAX_FOLDER_FILES = 500
# Extracted images kept per result (count and bytes); the rest is dropped.
MAX_ASSETS = 500
MAX_ASSET_BYTES = 100 * 1024 * 1024
# Extracted images shown in the preview as data: URIs, at most this much.
MAX_PREVIEW_IMAGE_BYTES = 16 * 1024 * 1024
_PREVIEW_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/bmp",
}

_BOOL_OPTIONS = (
    "page_markers",
    "include_hidden_sheets",
    "include_headers_footers",
    "keep_data_uris",
    "describe_images",
    "transcribe_formulas",
    "ocr_scanned_pages",
    "extract_images",
)
_CHOICE_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "table_format": ("markdown", "html", "auto"),
    "pdf_backend": ("auto", "pdfplumber", "pdfminer", "docling"),
    "ocr_engine": ("auto", "llm", "tesseract"),
}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; frame-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
# The original file, shown in an <iframe> (PDF) or <img> on our own page.
# The built-in PDF viewer embeds the document as a plugin, so object-src
# must allow 'self'; nothing else (script, style, network) is granted.
# No "plugin-types": it is obsolete, and Chromium then fails to show the
# PDF when the frame is added by script (checked in the browser).
ORIGINAL_PDF_CSP = (
    "default-src 'none'; object-src 'self'; frame-ancestors 'self'; "
    "base-uri 'none'; form-action 'none'"
)
ORIGINAL_IMAGE_CSP = (
    "default-src 'none'; img-src 'self'; frame-ancestors 'self'; sandbox"
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


def _clean_name(base: str) -> str:
    base = unicodedata.normalize("NFKC", base)
    base = re.sub(r"[^\w\-. ()]+", "_", base)
    base = re.sub(r"_+", "_", base).strip(" ._")
    return base[:120].rstrip(" ._")


def _safe_stem(name: str) -> str:
    """A filesystem- and header-safe base name (no extension)."""
    base = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if "." in base.strip("."):
        base = base.rsplit(".", 1)[0]
    return _clean_name(base) or "document"


def _safe_extension(name: str) -> str:
    """The original extension, if it looks like a normal one (e.g. '.docx')."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    _, ext = os.path.splitext(base)
    if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
        return ext.lower()
    return ""


def safe_rel_dir(rel_path: Any) -> str:
    """The folder part of a relative upload path, made safe for a ZIP.

    ``"../a/./b\\c/file.pdf"`` -> ``"a/b/c"``. Empty, ``.`` and ``..`` parts,
    drive letters and leading slashes disappear, every part is cleaned like a
    file name, and depth and length are capped. The result never starts with
    ``/`` and never contains ``..`` as a part (no zip-slip).
    """
    if not isinstance(rel_path, str) or not rel_path:
        return ""
    parts = re.split(r"[\\/]+", rel_path[:2000])[:-1]  # drop the file name
    clean: List[str] = []
    for part in parts:
        if part in ("", ".", ".."):
            continue
        name = _clean_name(part.replace(":", "_"))[:80].rstrip(" ._")
        if name and name not in (".", ".."):
            clean.append(name)
    return "/".join(clean[:16])[:400].strip("/")


def _md_name_for_url(url: str, title: Optional[str]) -> str:
    if title and title.strip():
        return _safe_stem(title.strip() + ".x") + ".md"
    parsed = urlparse(url)
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    stem = f"{parsed.hostname or 'page'}-{last}" if last else (parsed.hostname or "")
    return _safe_stem(stem + ".x") + ".md"


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """``attachment`` header with an ASCII fallback and an RFC 5987 UTF-8 name."""
    ascii_name = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    )
    ascii_name = re.sub(r"[^A-Za-z0-9\-_. ()]+", "_", ascii_name).strip(" ._")
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = "document" + (ascii_name if ascii_name.startswith(".") else "")
    return (
        f'{disposition}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _error(
    status: int,
    message: str,
    *,
    hint: Optional[str] = None,
    detail: Optional[str] = None,
    code: Optional[str] = None,
) -> JSONResponse:
    return JSONResponse(
        _error_body(message, hint=hint, detail=detail, code=code), status_code=status
    )


def _error_body(
    message: str,
    *,
    hint: Optional[str] = None,
    detail: Optional[str] = None,
    code: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"error": message}
    if hint:
        payload["hint"] = hint
    if detail:
        payload["detail"] = detail[:1000]
    if code:
        payload["code"] = code
    return payload


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


def error_details(exc: BaseException) -> Tuple[int, Dict[str, Any]]:
    """Turn a conversion failure into (status, friendly JSON body)."""
    missing = _missing_dependency_in(exc)
    if missing is not None:
        return 422, _error_body(
            "An extra component is needed to read this file.",
            hint=str(missing).strip(),
        )
    if isinstance(exc, ConversionCancelled):
        return 409, _error_body("The conversion was cancelled.", code="cancelled")
    if isinstance(exc, UnsupportedFormatException):
        return 415, _error_body(
            "This file type is not supported yet.",
            hint="Try a PDF, Word, PowerPoint, Excel, HTML, CSV, JSON, XML, "
            "EPUB, ZIP, image or audio file.",
        )
    if isinstance(exc, BlockedURLException):
        return 400, _error_body(
            "This address points to this computer or a private network, which "
            "MarkitForge does not download from.",
            hint="Start MarkitForge with --allow-private-urls to allow it.",
            detail=str(exc),
        )
    if isinstance(exc, ResourceLimitException):
        return 413, _error_body(
            "This file is too large or too complex to convert safely.",
            hint=str(exc),
        )
    if isinstance(exc, FileConversionException):
        detail = None
        if exc.attempts:
            first = exc.attempts[0]
            if first.exc_info and first.exc_info[1] is not None:
                detail = f"{type(first.exc_info[1]).__name__}: {first.exc_info[1]}"
        return 422, _error_body(
            "We couldn't convert this file. It may be damaged, password-protected, "
            "or not what its name suggests.",
            detail=detail,
        )
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        suffix = f" (the site answered with error {status})" if status else ""
        return 502, _error_body(f"The web page could not be downloaded{suffix}.")
    if isinstance(exc, requests.exceptions.RequestException):
        return 502, _error_body(
            "Could not reach that address. Check the link and your internet "
            "connection.",
        )
    if isinstance(exc, (OptionsError, LLMSettingsError)):
        return 400, _error_body(str(exc))
    logger.error("Unexpected conversion failure: %s", type(exc).__name__)
    logger.debug("Conversion failure details", exc_info=exc)
    return 500, _error_body(
        "Something went wrong while converting this file.",
        detail=f"{type(exc).__name__}: {exc}",
    )


def error_for_exception(exc: BaseException) -> JSONResponse:
    """Turn a conversion failure into a friendly JSON error."""
    status, body = error_details(exc)
    return JSONResponse(body, status_code=status)


_IMAGE_LINK_RE = re.compile(r"(!\[[^\]\n]*\])\(([^)\s]+)(\s+\"[^\"\n]*\")?\)")


def rewrite_image_links(markdown: str, mapping: Dict[str, str]) -> str:
    """Point ``![alt](name)`` image links at ``mapping[name]``."""
    if not mapping:
        return markdown

    def repl(m: "re.Match[str]") -> str:
        new = mapping.get(m.group(2))
        return f"{m.group(1)}({new}{m.group(3) or ''})" if new else m.group(0)

    return _IMAGE_LINK_RE.sub(repl, markdown)


def _preview_markdown(entry: StoredResult, markdown: str) -> str:
    """Extracted images, inlined as data: URIs so the preview can show them
    (the sanitizer only lets data: images through)."""
    mapping: Dict[str, str] = {}
    budget = MAX_PREVIEW_IMAGE_BYTES
    for asset in entry.assets:
        mime = (asset.mime_type or "").lower()
        data = asset.data
        if data is None or mime not in _PREVIEW_IMAGE_TYPES or len(data) > budget:
            continue
        budget -= len(data)
        mapping[asset.name] = f"data:{mime};base64," + base64.b64encode(data).decode()
    return rewrite_image_links(markdown, mapping)


def _result_payload(entry: StoredResult) -> Dict[str, Any]:
    result = entry.result
    data = result.to_dict(include_markdown=False)
    sizes = entry.asset_sizes
    return {
        "id": entry.id,
        "filename": entry.source_name,
        "md_filename": entry.md_filename,
        "rel_dir": entry.rel_dir,
        "title": result.title,
        "markdown": result.markdown,
        "html": render_markdown(_preview_markdown(entry, result.markdown)),
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
                "size": sizes[i] if i < len(sizes) else a["size"],
                "description": a["description"],
            }
            for i, a in enumerate(data["assets"])
        ],
        "saved_assets": [name for _, name in safe_asset_files(entry)],
        "edited": entry.edited,
        "has_original": entry.original is not None,
        "created": entry.created,
    }


def _zip_name_ok(name: str) -> bool:
    parts = name.split("/")
    return (
        bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and ":" not in parts[0]
        and all(p not in ("", ".", "..") for p in parts)
    )


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


class TokenMiddleware:
    """Require the session token on every ``/api/`` request.

    Fetches send it as ``X-MarkitForge-Token``; plain GET links (downloads,
    the original-file view) may send ``?t=`` instead.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        supplied: Optional[bytes] = None
        for name, value in scope.get("headers", []):
            if name == TOKEN_HEADER.lower().encode():
                supplied = value
                break
        if supplied is None and scope["method"] in ("GET", "HEAD"):
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            values = query.get("t")
            if values:
                supplied = values[0].encode("latin-1", "replace")
        if supplied is None or not secrets.compare_digest(supplied, self.token):
            response = _error(
                403,
                "This page is out of date or did not come from MarkitForge.",
                hint="Reload the page. MarkitForge makes a new session key every "
                "time it starts.",
                code="token",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


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
                if (
                    scope["path"].startswith("/api/")
                    and b"cache-control" not in present
                ):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------


def _load_config_safely() -> Config:
    try:
        return load_config()
    except ValueError as exc:
        logger.warning("%s", exc)
        return Config()


def create_app(
    *,
    max_upload_mb: float = DEFAULT_MAX_UPLOAD_MB,
    allowed_hosts: Optional[Sequence[str]] = None,
    store_capacity: int = 50,
    converter_factory: Optional[Callable[..., MarkitForge]] = None,
    url_converter_factory: Optional[Callable[[], MarkitForge]] = None,
    max_workers: Optional[int] = None,
    history_dir: Optional[os.PathLike] = None,
    history_max_entries: int = 200,
    history_max_mb: float = 512,
    allow_private_urls: bool = False,
    config: Optional[Config] = None,
    max_original_mb: float = MAX_ORIGINAL_BYTES / (1024 * 1024),
    llm_client_factory: Optional[Callable[..., Any]] = None,
    job_ttl: float = 15 * 60,
    token: Optional[str] = None,
) -> Starlette:
    """Build the GUI web app.

    Args:
        max_upload_mb: largest accepted upload, in megabytes.
        allowed_hosts: accepted ``Host`` header values (default: loopback only;
            ``["*"]`` is refused).
        store_capacity: how many results to keep in memory (oldest dropped).
        converter_factory: builds a MarkitForge instance (one per worker
            thread). For web addresses it is called with
            ``block_private_networks`` and ``max_download_bytes``.
        url_converter_factory: builds the MarkitForge used for web addresses
            (overrides the default built from ``converter_factory``).
        max_workers: how many conversions may run at the same time.
        history_dir: keep results on disk here (None: no history).
        allow_private_urls: allow web addresses on this computer or the
            local network.
        config: settings (default: ``markitforge._config.load_config()``).
        max_original_mb: largest uploaded original kept for the side-by-side view.
        llm_client_factory: builds the OpenAI-compatible client (tests).
        token: the session token (default: a new random one).
    """
    max_upload_bytes = int(max_upload_mb * 1024 * 1024)
    max_original_bytes = int(max_original_mb * 1024 * 1024)
    body_limit = max_upload_bytes + _MULTIPART_OVERHEAD
    store = ResultStore(store_capacity)
    hosts = list(allowed_hosts) if allowed_hosts else list(LOOPBACK_HOSTS)
    if "*" in hosts:
        raise ValueError("allowed_hosts may not contain '*' (DNS rebinding risk).")
    allowed_origin_hosts = {h.strip("[]").lower() for h in hosts}
    cfg = config if config is not None else _load_config_safely()
    try:
        config_defaults = parse_options(dict(cfg.defaults))
    except OptionsError as exc:
        logger.warning("Ignoring [defaults] in the config file: %s", exc)
        config_defaults = {}
    config_limits = dict(cfg.limits)
    factory = converter_factory or MarkitForge
    instance = dict(cfg.instance)
    cfg_download = instance.get("max_download_bytes")
    download_cap = (
        min(int(cfg_download), max_upload_bytes)
        if isinstance(cfg_download, int) and cfg_download > 0
        else max_upload_bytes
    )
    url_kwargs: Dict[str, Any] = {
        k: v for k, v in instance.items() if k in ("fetch_deadline", "max_redirects")
    }
    url_kwargs["block_private_networks"] = not allow_private_urls
    url_kwargs["max_download_bytes"] = download_cap
    url_factory = url_converter_factory or partial(factory, **url_kwargs)
    history = (
        HistoryStore(
            history_dir,
            max_entries=history_max_entries,
            max_bytes=int(history_max_mb * 1024 * 1024),
        )
        if history_dir is not None
        else None
    )
    llm = LLMSettings(cfg.llm, client_factory=llm_client_factory)
    session_token = token or secrets.token_urlsafe(32)
    local = threading.local()
    workers = max_workers or max(2, min(4, os.cpu_count() or 2))
    limiter_box: Dict[str, anyio.CapacityLimiter] = {}
    jobs = JobManager(workers, ttl=job_ttl, error_mapper=error_details)
    preview_lock = threading.Lock()

    def converter() -> MarkitForge:
        conv = getattr(local, "converter", None)
        if conv is None:
            conv = factory()
            local.converter = conv
        return conv

    def url_converter() -> MarkitForge:
        conv = getattr(local, "url_converter", None)
        if conv is None:
            conv = url_factory()
            local.url_converter = conv
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

    def get_entry_sync(item_id: Any) -> Optional[StoredResult]:
        if not isinstance(item_id, str) or len(item_id) > 100:
            return None
        entry = store.get(item_id)
        if entry is None and history is not None and valid_id(item_id):
            entry = history.load(item_id)
            if entry is not None:
                store.put(entry)
        return entry

    async def get_entry(item_id: Any) -> Optional[StoredResult]:
        return await in_thread(partial(get_entry_sync, item_id))

    def gone() -> JSONResponse:
        return _error(
            404, "This result is no longer available. Please convert it again."
        )

    def conversion_kwargs(options: Dict[str, Any]) -> Dict[str, Any]:
        """Options + config defaults and limits (+ AI client when needed).

        Raises LLMSettingsError (e.g. no API key) before a job is started.
        """
        kwargs: Dict[str, Any] = {**config_defaults, **options, **config_limits}
        engine = kwargs.get("ocr_engine", "auto")
        needs_llm = bool(
            kwargs.get("describe_images")
            or kwargs.get("transcribe_formulas")
            or (kwargs.get("ocr_scanned_pages") and engine != "tesseract")
        )
        if needs_llm:
            # Only when asked: a client makes some converters send images out.
            kwargs["llm_client"] = llm.client()
            kwargs["llm_model"] = llm.model
        return kwargs

    def with_job(kwargs: Dict[str, Any], job: Job) -> Dict[str, Any]:
        return {**kwargs, "progress": job.on_progress, "cancel_event": job.cancel_event}

    def finish_sync(
        source_name: str,
        md_filename: str,
        result: DocumentConverterResult,
        *,
        rel_dir: str = "",
        original: Optional[bytes] = None,
        original_ext: str = "",
        original_size: Optional[int] = None,
        keep_assets: bool = False,
    ) -> Dict[str, Any]:
        sizes = [len(a.data) if a.data is not None else None for a in result.assets]
        kept = []
        if keep_assets:
            total = 0
            dropped = 0
            for asset in result.assets:
                if asset.data is None:
                    continue
                if len(kept) >= MAX_ASSETS or total + len(asset.data) > MAX_ASSET_BYTES:
                    dropped += 1
                    asset.data = None
                    continue
                total += len(asset.data)
                kept.append(asset)
            if dropped:
                result.warnings.append(
                    f"{dropped} extracted image(s) were not kept because the "
                    "result already had too many or too large images."
                )
        else:
            # The GUI lists assets but does not keep their bytes.
            for asset in result.assets:
                asset.data = None
        entry = StoredResult(
            id=new_id(),
            source_name=source_name,
            md_filename=md_filename,
            result=result,
            rel_dir=rel_dir,
            original=original,
            original_ext=original_ext,
            original_size=original_size,
            assets=kept,
            asset_sizes=sizes,
        )
        store.put(entry)
        if history is not None:
            try:
                history.save(entry)
            except (OSError, ValueError) as exc:
                logger.warning("Could not save to the history: %s", exc)
        return _result_payload(entry)

    def job_response(job: Job) -> JSONResponse:
        return JSONResponse(job.to_dict(), status_code=202)

    def submit(
        name: str,
        work: Callable[[Job], Dict[str, Any]],
        cleanup: Optional[Callable[[], None]] = None,
    ) -> Response:
        try:
            job = jobs.submit(name, work, cleanup)
        except TooManyJobs:
            return _error(
                429,
                "Too many conversions are waiting. Please wait for some to finish.",
            )
        return job_response(job)

    # ---- routes -----------------------------------------------------------

    async def index(request: Request) -> Response:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        page = page.replace(
            TOKEN_META,
            f'<meta name="mf-token" content="{html_lib.escape(session_token)}">',
            1,
        )
        return Response(
            page,
            media_type="text/html; charset=utf-8",
            headers={"cache-control": "no-store"},
        )

    async def info(request: Request) -> Response:
        return JSONResponse(
            {
                "version": __version__,
                "markitforge_version": markitforge_version,
                "max_upload_mb": max_upload_mb,
                "max_upload_bytes": max_upload_bytes,
                "max_original_bytes": max_original_bytes,
                "max_edit_bytes": MAX_EDIT_BYTES,
                "max_folder_files": MAX_FOLDER_FILES,
                "history_enabled": history is not None,
                "private_urls_allowed": allow_private_urls,
                "defaults": config_defaults,
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
            raw_rel = form.get("rel_path")
            rel_dir = safe_rel_dir(raw_rel) if isinstance(raw_rel, str) else ""

            original = upload.filename or "document"
            md_filename = _safe_stem(original) + ".md"
            suffix = _safe_extension(original)
            fd, tmp_path = tempfile.mkstemp(prefix="markitforge-gui-", suffix=suffix)

            def remove_tmp() -> None:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("Could not delete temporary file %s", tmp_path)

            try:
                with os.fdopen(fd, "wb") as fh:
                    await in_thread(
                        partial(shutil.copyfileobj, upload.file, fh, 1024 * 1024)
                    )
                await upload.close()
            except BaseException:
                remove_tmp()
                raise

        stream_info = StreamInfo(
            filename=_safe_stem(original) + suffix, extension=suffix or None
        )
        display = f"{rel_dir}/{original}" if rel_dir else original

        try:
            # Builds the AI client now, so a missing key is reported at once.
            kwargs = conversion_kwargs(options)
        except LLMSettingsError as exc:
            remove_tmp()
            return _error(400, str(exc))

        def work(job: Job) -> Dict[str, Any]:
            result = converter().convert_local(
                tmp_path, stream_info=stream_info, **with_job(kwargs, job)
            )
            size = os.path.getsize(tmp_path)
            kept = None
            if size <= max_original_bytes:
                with open(tmp_path, "rb") as fh:
                    kept = fh.read(max_original_bytes + 1)
                if len(kept) > max_original_bytes:
                    kept = None
            remove_tmp()
            return finish_sync(
                original,
                md_filename,
                result,
                rel_dir=rel_dir,
                original=kept,
                original_ext=suffix,
                original_size=size,
                keep_assets=bool(kwargs.get("extract_images")),
            )

        return submit(display, work, cleanup=remove_tmp)

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
        try:
            kwargs = conversion_kwargs(options)
        except LLMSettingsError as exc:
            return _error(400, str(exc))

        def work(job: Job) -> Dict[str, Any]:
            # Same size cap for web pages as for uploads.
            result = url_converter().convert_uri(
                url, max_download_bytes=download_cap, **with_job(kwargs, job)
            )
            return finish_sync(
                url,
                _md_name_for_url(url, result.title),
                result,
                keep_assets=bool(kwargs.get("extract_images")),
            )

        return submit(url, work)

    async def job_status(request: Request) -> Response:
        job = jobs.get(request.path_params["job_id"])
        if job is None:
            return _error(404, "This conversion is no longer known. Please try again.")
        return JSONResponse(job.to_dict())

    async def job_cancel(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        job = jobs.cancel(request.path_params["job_id"])
        if job is None:
            return _error(404, "This conversion is no longer known.")
        return JSONResponse(job.to_dict())

    async def render(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        body = await read_json(request)
        item_id = body.get("id")
        entry: Optional[StoredResult] = None
        if item_id is not None:
            if not isinstance(item_id, str):
                return _error(400, "Unknown result id.")
            entry = await get_entry(item_id)
            if entry is None:
                return gone()
            pages = body.get("pages")
            if pages is None or (isinstance(pages, str) and not pages.strip()):
                markdown = entry.result.markdown
            elif not isinstance(pages, str):
                return _error(400, "The page range must be text, e.g. 1-3,5")
            elif entry.edited:
                return _error(
                    409,
                    "Page selection is not available after the Markdown was edited.",
                )
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
            for_id = body.get("for_id")
            if isinstance(for_id, str):
                entry = await get_entry(for_id)
        shown = _preview_markdown(entry, markdown) if entry is not None else markdown
        html = await in_thread(partial(render_markdown, shown))
        return JSONResponse({"markdown": markdown, "html": html})

    async def result_get(request: Request) -> Response:
        entry = await get_entry(request.path_params["item_id"])
        if entry is None:
            return gone()
        payload = await in_thread(partial(_result_payload, entry))
        return JSONResponse(payload)

    async def result_put_markdown(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        entry = await get_entry(request.path_params["item_id"])
        if entry is None:
            return gone()
        body = await read_json(request)
        markdown = body.get("markdown")
        if not isinstance(markdown, str):
            return _error(400, "No Markdown was sent.")
        if len(markdown.encode("utf-8", "surrogatepass")) > MAX_EDIT_BYTES:
            return _error(
                413,
                f"The edited Markdown is too large (limit "
                f"{MAX_EDIT_BYTES // (1024 * 1024)} MB).",
            )
        entry.result.markdown = markdown
        entry.edited = True
        if history is not None:
            try:
                await in_thread(partial(history.save, entry))
            except (OSError, ValueError) as exc:
                logger.warning("Could not save the edit to the history: %s", exc)
        return JSONResponse({"id": entry.id, "edited": True, "size": len(markdown)})

    async def download(request: Request) -> Response:
        entry = await get_entry(request.path_params["item_id"])
        if entry is None:
            return gone()
        markdown = entry.result.markdown
        filename = entry.md_filename
        pages = request.query_params.get("pages", "").strip()
        if pages:
            if entry.edited:
                return _error(
                    409,
                    "Page selection is not available after the Markdown was edited.",
                )
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
            for item_id in ids_param.split(",")[:2000]:
                entry = await get_entry(item_id.strip())
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
                    prefix = f"{entry.rel_dir}/" if entry.rel_dir else ""
                    name = prefix + entry.md_filename
                    count = used.get(name.lower(), 0)
                    used[name.lower()] = count + 1
                    if count:
                        name = f"{name[: -len('.md')]} ({count + 1}).md"
                        used[name.lower()] = 1
                    if not _zip_name_ok(name):  # defence in depth
                        continue
                    markdown = entry.result.markdown
                    files = safe_asset_files(entry)
                    if files:
                        folder = name[: -len(".md")] + "_assets"
                        link_dir = folder.rsplit("/", 1)[-1]
                        mapping = {
                            a.name: quote(f"{link_dir}/{fname}") for a, fname in files
                        }
                        markdown = rewrite_image_links(markdown, mapping)
                        for asset, fname in files:
                            path = f"{folder}/{fname}"
                            if _zip_name_ok(path):
                                zf.writestr(path, asset.data or b"")
                    zf.writestr(name, markdown)
            return buf.getvalue()

        data = await in_thread(build)
        return Response(
            data,
            media_type="application/zip",
            headers={
                "content-disposition": content_disposition("markitforge-results.zip")
            },
        )

    # ---- original file ----------------------------------------------------

    def original_info_sync(entry: StoredResult) -> Dict[str, Any]:
        base: Dict[str, Any] = {"id": entry.id, "name": entry.source_name}
        if entry.original is None:
            if entry.original_size and entry.original_size > max_original_bytes:
                limit = max(1, round(max_original_bytes / (1024 * 1024)))
                message = (
                    f"The original is larger than {limit} MB, so it is not kept "
                    "for this view."
                )
            elif entry.source_name.startswith(("http://", "https://")):
                message = "The original view is only available for uploaded files."
            else:
                message = (
                    "The original file is only kept while MarkitForge is running. "
                    "Convert it again to see it here."
                )
            return {**base, "kind": "none", "message": message}
        data = entry.original
        kind = original_kind(entry.original_ext, data)
        if kind == "text":
            text, truncated = decode_text(data)
            return {**base, "kind": "text", "text": text, "truncated": truncated}
        if kind == "image":
            return {
                **base,
                "kind": "image",
                "mime_type": image_type(entry.original_ext, data),
            }
        if kind == "pdf":
            return {**base, "kind": "pdf"}
        if kind == "office":
            with preview_lock:
                if entry.preview_pdf is None and entry.preview_error is None:
                    try:
                        entry.preview_pdf = office_to_pdf(
                            data,
                            entry.original_ext,
                            float(config_limits.get("libreoffice_timeout") or 120),
                        )
                    except PreviewUnavailable as exc:
                        entry.preview_error = str(exc)
            if entry.preview_pdf is not None:
                return {**base, "kind": "pdf", "converted": True}
            return {**base, "kind": "none", "message": entry.preview_error}
        return {
            **base,
            "kind": "none",
            "message": "Preview not available for this file type.",
        }

    async def original_info(request: Request) -> Response:
        entry = await get_entry(request.path_params["item_id"])
        if entry is None:
            return gone()
        # Office previews run LibreOffice: off the event loop, own thread pool
        # slot (not the conversion jobs').
        data = await anyio.to_thread.run_sync(partial(original_info_sync, entry))
        return JSONResponse(data)

    async def original_file(request: Request) -> Response:
        entry = await get_entry(request.path_params["item_id"])
        if entry is None:
            return gone()
        data = entry.original
        if data is None:
            return _error(404, "The original file is not available.")
        name = entry.source_name.replace("\\", "/").rsplit("/", 1)[-1] or "original"
        common = {
            "x-content-type-options": "nosniff",
            "x-frame-options": "SAMEORIGIN",
            "cache-control": "private, no-store",
        }
        kind = original_kind(entry.original_ext, data)
        if kind == "office" and entry.preview_pdf is not None:
            data, kind = entry.preview_pdf, "pdf"
            name = _safe_stem(name) + ".pdf"
        if kind == "pdf":
            return Response(
                data,
                media_type="application/pdf",
                headers={
                    **common,
                    "content-disposition": content_disposition(name, "inline"),
                    "content-security-policy": ORIGINAL_PDF_CSP,
                },
            )
        mime = image_type(entry.original_ext, data) if kind == "image" else None
        if mime:
            return Response(
                data,
                media_type=mime,
                headers={
                    **common,
                    "content-disposition": content_disposition(name, "inline"),
                    "content-security-policy": ORIGINAL_IMAGE_CSP,
                },
            )
        return _error(415, "Preview not available for this file type.")

    # ---- history ----------------------------------------------------------

    async def history_list(request: Request) -> Response:
        if history is None:
            return JSONResponse({"enabled": False, "items": []})
        items = [s.to_dict() for s in history.list()]
        return JSONResponse(
            {
                "enabled": True,
                "items": items,
                "max_entries": history.max_entries,
            }
        )

    async def history_clear(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        removed = 0
        if history is not None:
            removed = await in_thread(history.clear)
        store.clear()
        return JSONResponse({"cleared": removed})

    # ---- AI settings ------------------------------------------------------

    async def llm_get(request: Request) -> Response:
        return JSONResponse(llm.public())

    async def llm_put(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        body = await read_json(request)
        try:
            llm.update(body)
        except LLMSettingsError as exc:
            return _error(400, str(exc))
        return JSONResponse(llm.public())

    async def llm_test(request: Request) -> Response:
        denied = check_client(request)
        if denied is not None:
            return denied
        try:
            data = await anyio.to_thread.run_sync(llm.test)
        except LLMSettingsError as exc:
            return _error(400, str(exc))
        return JSONResponse(data)

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

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        jobs.shutdown()

    middleware = [
        Middleware(SecurityHeadersMiddleware),
        Middleware(TrustedHostMiddleware, allowed_hosts=hosts),
        Middleware(TokenMiddleware, token=session_token),
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
            Route("/api/jobs/{job_id}", job_status, methods=["GET"]),
            Route("/api/jobs/{job_id}/cancel", job_cancel, methods=["POST"]),
            Route("/api/render", render, methods=["POST"]),
            Route("/api/results/{item_id}", result_get, methods=["GET"]),
            Route(
                "/api/results/{item_id}/markdown", result_put_markdown, methods=["PUT"]
            ),
            Route("/api/download/{item_id}", download, methods=["GET"]),
            Route("/api/zip", download_zip, methods=["GET"]),
            Route("/api/original/{item_id}", original_file, methods=["GET"]),
            Route("/api/original/{item_id}/info", original_info, methods=["GET"]),
            Route("/api/history", history_list, methods=["GET"]),
            Route("/api/history", history_clear, methods=["DELETE"]),
            Route("/api/llm", llm_get, methods=["GET"]),
            Route("/api/llm", llm_put, methods=["PUT"]),
            Route("/api/llm/test", llm_test, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        middleware=middleware,
        exception_handlers=handlers,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.history = history
    app.state.jobs = jobs
    app.state.llm = llm
    app.state.token = session_token
    app.state.max_upload_bytes = max_upload_bytes
    app.state.url_converter_kwargs = dict(url_kwargs)
    return app
