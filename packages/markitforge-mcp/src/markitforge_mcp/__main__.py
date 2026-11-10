# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from markitforge import (
    MarkitForge,
    FileConversionException,
    ResourceLimitException,
    UnsupportedFormatException,
)
from markitforge._uri_utils import file_uri_to_path
import requests
import uvicorn

logger = logging.getLogger(__name__)

# Initialize the MCP server for MarkitForge
mcp = MCPServer("markitforge")


# --- File access policy -------------------------------------------------------
# file: URIs let a client read local files. Over stdio the client is the local user,
# so all paths are allowed unless roots are configured. Over HTTP, file: is OFF
# unless roots are given (--allow-file-root or MARKITFORGE_MCP_FILE_ROOTS).
_file_roots: Optional[List[str]] = None  # None = no restriction
_files_enabled = True
# SSRF guard for http(s) fetches; on by default over HTTP (see main()).
_block_private_urls = False


def configure_file_access(*, enabled: bool, roots: Optional[List[str]]) -> None:
    global _file_roots, _files_enabled
    _files_enabled = enabled
    _file_roots = [os.path.realpath(r) for r in roots] if roots else None


def _roots_from_env() -> List[str]:
    raw = os.getenv("MARKITFORGE_MCP_FILE_ROOTS", "")
    return [r for r in raw.split(os.pathsep) if r.strip()]


def _check_file_access(uri: str) -> None:
    if urlparse(uri.strip()).scheme.lower() != "file":
        return
    if not _files_enabled:
        raise ToolError(
            "file: URIs are disabled on this server. Start it with "
            "--allow-file-root DIR to allow reading files under DIR."
        )
    if _file_roots is None:
        return
    try:
        _, path = file_uri_to_path(uri.strip())
    except Exception as exc:
        raise ToolError("Invalid file URI.") from exc
    real = os.path.realpath(path)
    for root in _file_roots:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return
    raise ToolError("That file is outside the folders this server may read.")


def _output_dir() -> Path:
    """The folder convert_to_file writes into, created private to this user.

    The default lives under the per-user cache dir, not a shared /tmp path that
    another local user could pre-create or fill with symlinks.
    """
    configured = os.getenv("MARKITFORGE_MCP_OUTPUT_DIR")
    if configured:
        d = Path(configured)
    else:
        base = os.getenv("XDG_CACHE_HOME") or (
            os.getenv("LOCALAPPDATA") if os.name == "nt" else None
        )
        d = Path(base or Path.home() / ".cache") / "markitforge-mcp" / "output"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ToolError("The output folder is not a real directory.")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ToolError("The output folder is owned by another user.")
    return d


def _write_new_file(directory: Path, name: str, text: str) -> Path:
    """Create a new file without following symlinks or overwriting anything."""
    stem, suffix = os.path.splitext(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    data = text.encode("utf-8")
    for n in range(1, 1000):
        candidate = directory / (name if n == 1 else f"{stem}-{n}{suffix}")
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return candidate
    raise ToolError("Could not pick a free output file name.")


def _safe_name(name: str) -> str:
    base = os.path.basename(name or "").strip() or "document"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120].lstrip(".") or "document"
    return base if base.lower().endswith(".md") else base + ".md"


# --- Conversion core ----------------------------------------------------------


def _config():
    """Limits from the user config file (see markitforge._config); cached."""
    global _CONFIG
    if _CONFIG is None:
        from markitforge._config import Config, load_config

        try:
            _CONFIG = load_config()
        except ValueError as exc:
            logger.warning("Ignoring config file: %s", exc)
            _CONFIG = Config()
    return _CONFIG


_CONFIG = None


def _convert_sync(uri: str, **options: Any):
    _check_file_access(uri)
    config = _config()
    instance = {**config.instance}
    if _block_private_urls:
        instance["block_private_networks"] = True
    converter = MarkitForge(enable_plugins=check_plugins_enabled(), **instance)
    try:
        return converter.convert_uri(uri, **{**config.limits, **options})

    # SDK 2.x only exposes ToolError messages.
    except UnsupportedFormatException as exc:
        raise ToolError(str(exc)) from exc
    except ResourceLimitException as exc:
        raise ToolError(str(exc)) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise ToolError(
            f"Fetching the resource failed with HTTP status {status}."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ToolError("Could not fetch the resource.") from exc
    except FileConversionException as exc:
        raise ToolError("File conversion failed.") from exc
    except OSError as exc:
        # str(exc) embeds the resolved path; errno's fixed strerror does not.
        detail = (
            os.strerror(exc.errno) if exc.errno else "the resource could not be read"
        )
        raise ToolError(f"Could not read the resource: {detail}.") from exc
    except ValueError as exc:
        # URI validation failures raised by convert_uri itself, which only
        # restate the URI the client supplied.
        raise ToolError(str(exc)) from exc


async def _convert(uri: str, **options: Any):
    # Conversion is CPU/IO bound; keep the event loop free for other requests.
    return await asyncio.to_thread(_convert_sync, uri, **options)


def _apply_pages(result: Any, pages: Optional[str]) -> str:
    if not pages:
        return result.markdown
    if len(pages) > 1000:
        raise ToolError("pages: range is too long.")
    try:
        return result.select_pages(pages)
    except ValueError as exc:
        raise ToolError(f"pages: {exc}") from exc


def _outline(markdown: str, limit: int = 40) -> List[str]:
    heads = [ln.strip() for ln in markdown.splitlines() if re.match(r"^#{1,4} \S", ln)]
    return heads[:limit]


# --- Tools --------------------------------------------------------------------


@mcp.tool()
async def convert_to_markdown(
    uri: str,
    pages: Optional[str] = None,
    page_markers: bool = False,
    max_chars: Optional[int] = None,
) -> str:
    """Convert a resource described by an http:, https:, file: or data: URI to markdown.

    pages: only return these pages/slides/sheets, e.g. "1-3,7" (PDF, PPTX, XLSX, DOCX).
    page_markers: put <!-- page N --> between pages.
    max_chars: cut the output to this many characters (a note says how much was cut).
    For big documents prefer convert_to_file, then read the file in parts.
    """
    options = {"page_markers": True} if page_markers else {}
    result = await _convert(uri, **options)
    text = _apply_pages(result, pages)
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        cut = len(text) - max_chars
        text = (
            text[:max_chars]
            + f"\n\n[... {cut:,} more characters cut. Use pages= or convert_to_file.]"
        )
    return text


@mcp.tool()
async def convert_to_file(
    uri: str,
    filename: Optional[str] = None,
    pages: Optional[str] = None,
    page_markers: bool = True,
) -> str:
    """Convert a resource to a .md file on the server and return a short summary.

    Keeps large documents out of the conversation: the reply has the file path,
    size, title, page count, headings outline and warnings, not the content.
    Files go to MARKITFORGE_MCP_OUTPUT_DIR (default: ~/.cache/markitforge-mcp/output).
    """
    options = {"page_markers": True} if page_markers else {}
    result = await _convert(uri, **options)
    text = _apply_pages(result, pages)
    stem = filename or os.path.basename(urlparse(uri).path) or "document"
    target = await asyncio.to_thread(
        _write_new_file,
        _output_dir(),
        _safe_name(os.path.splitext(stem)[0] if not filename else stem),
        text,
    )
    return json.dumps(
        {
            "path": str(target),
            "characters": len(text),
            "lines": text.count("\n") + 1,
            "title": result.title,
            "page_count": len(result.pages) or None,
            "outline": _outline(text),
            "warnings": result.warnings,
            "preview": text[:500],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def get_document_info(uri: str) -> str:
    """Metadata, page list (index, title, size) and warnings for a document, without its text."""
    result = await _convert(uri)
    info = result.to_dict(include_markdown=False)
    for page, sec in zip(info["pages"], result.pages):
        page["characters"] = len(sec.markdown)
    info["characters"] = len(result.markdown)
    info["outline"] = _outline(result.markdown)
    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_supported_formats() -> str:
    """Formats this server can convert, and which ones are missing dependencies."""
    from markitforge._formats import format_status

    return json.dumps(format_status(), indent=2)


def check_plugins_enabled() -> bool:
    return os.getenv("MARKITFORGE_ENABLE_PLUGINS", "false").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def create_starlette_app(
    mcp_server: MCPServer, *, host: str = "127.0.0.1", debug: bool = False
) -> Starlette:
    sse_app = mcp_server.sse_app(host=host)
    http_app = mcp_server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        host=host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Run both sub-apps' lifespans (the Streamable HTTP session manager)."""
        async with sse_app.router.lifespan_context(app):
            async with http_app.router.lifespan_context(app):
                yield

    return Starlette(
        debug=debug,
        routes=[*sse_app.routes, *http_app.routes],
        lifespan=lifespan,
    )


# Main entry point
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run a MarkitForge MCP server")

    parser.add_argument(
        "--http",
        action="store_true",
        help="Run the server with Streamable HTTP and SSE transport rather than STDIO (default: False)",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="(Deprecated) An alias for --http (default: False)",
    )
    parser.add_argument(
        "--host", default=None, help="Host to bind to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="Port to listen on (default: 3001)"
    )
    parser.add_argument(
        "--allow-file-root",
        action="append",
        default=[],
        metavar="DIR",
        help="Allow file: URIs under DIR (repeatable). Over HTTP, file: URIs are off unless this is set. Also MARKITFORGE_MCP_FILE_ROOTS.",
    )
    parser.add_argument(
        "--allow-private-urls",
        action="store_true",
        help="Over HTTP, also fetch URLs on private/loopback/link-local addresses (blocked by default).",
    )
    parser.add_argument(
        "--block-private-urls",
        action="store_true",
        help="Over stdio, refuse URLs on private/loopback/link-local addresses.",
    )
    parser.add_argument(
        "--no-file-access",
        action="store_true",
        help="Refuse all file: URIs.",
    )
    args = parser.parse_args()

    use_http = args.http or args.sse

    roots = list(args.allow_file_root) + _roots_from_env()
    configure_file_access(
        enabled=not args.no_file_access and (bool(roots) or not use_http),
        roots=roots or None,
    )
    global _block_private_urls
    _block_private_urls = args.block_private_urls or (
        use_http and not args.allow_private_urls
    )

    if not use_http and (args.host or args.port):
        parser.error(
            "Host and port arguments are only valid when using streamable HTTP or SSE transport (see: --http)."
        )
        sys.exit(1)

    if use_http:
        host = args.host if args.host else "127.0.0.1"
        if args.host and args.host not in ("127.0.0.1", "localhost"):
            print(
                "\n"
                "WARNING: The server is being bound to a non-localhost interface "
                f"({host}).\n"
                "This exposes the server to other machines on the network or Internet.\n"
                "The server has NO authentication and runs with your user's privileges.\n"
                "Any process or user that can reach this interface can fetch network\n"
                "resources accessible to this user, and read files under any\n"
                "--allow-file-root folders.\n"
                "Only proceed if you understand the security implications.\n",
                file=sys.stderr,
            )
        starlette_app = create_starlette_app(mcp, host=host, debug=False)
        uvicorn.run(
            starlette_app,
            host=host,
            port=args.port if args.port else 3001,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
