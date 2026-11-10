# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Command line entry point: ``markitforge-gui`` / ``python -m markitforge_gui``."""

import argparse
import ipaddress
import socket
import sys
import threading
import time
import webbrowser
from typing import List, Optional, Sequence

import uvicorn

from .__about__ import __version__
from ._app import DEFAULT_MAX_UPLOAD_MB, LOOPBACK_HOSTS, create_app

__all__ = ["main", "bind_socket", "build_parser"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="markitforge-gui",
        description="Open the MarkitForge app: convert files to Markdown in your "
        "browser. Everything stays on this computer.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Address to listen on (default: 127.0.0.1, this computer only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT}; a free port is used "
        "if it is taken).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser window automatically.",
    )
    parser.add_argument(
        "--window",
        action="store_true",
        help="Open in a native window instead of the browser (needs the "
        "[window] extra: pip install 'markitforge-gui[window]').",
    )
    parser.add_argument(
        "--max-upload-mb",
        type=float,
        default=DEFAULT_MAX_UPLOAD_MB,
        help=f"Largest file you can upload, in MB (default: {DEFAULT_MAX_UPLOAD_MB}).",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def is_loopback(host: str) -> bool:
    if host.strip("[]").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _is_wildcard(host: str) -> bool:
    return host.strip("[]") in ("0.0.0.0", "::", "")


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind a listening socket on ``port``, or on any free port if it is taken."""
    bare = host.strip("[]")
    infos = socket.getaddrinfo(bare, None, type=socket.SOCK_STREAM)
    family = infos[0][0] if infos else socket.AF_INET
    last_error: Optional[OSError] = None
    for candidate in (port, 0) if port else (0,):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind((bare, candidate))
            sock.listen(128)
            sock.set_inheritable(True)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    assert last_error is not None
    raise last_error


def _display_url(host: str, port: int) -> str:
    bare = host.strip("[]")
    if _is_wildcard(bare):
        bare = "127.0.0.1"
    if ":" in bare:
        bare = f"[{bare}]"
    return f"http://{bare}:{port}/"


def _own_addresses() -> List[str]:
    """This machine's host name and the IPs it resolves to."""
    names = {socket.gethostname()}
    try:
        names.add(socket.getfqdn())
    except OSError:
        pass
    found = set()
    for name in names:
        if name:
            found.add(name.lower())
        try:
            for info in socket.getaddrinfo(name, None):
                ip = info[4][0].split("%", 1)[0]
                found.add(f"[{ip}]" if ":" in ip else ip)
        except OSError:
            continue
    return sorted(found)


def _allowed_hosts(host: str) -> List[str]:
    if is_loopback(host):
        return list(LOOPBACK_HOSTS)
    if _is_wildcard(host):
        # Never "*": that lets a DNS-rebinding website talk to this server.
        # Accept loopback plus this machine's own names and addresses only.
        return list(LOOPBACK_HOSTS) + _own_addresses()
    bare = host.strip("[]")
    return list(LOOPBACK_HOSTS) + [bare, f"[{bare}]" if ":" in bare else bare]


def _open_browser_when_ready(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        if server.should_exit:
            return
        time.sleep(0.05)
    if server.started:
        try:
            webbrowser.open(url, new=2)
        except Exception:  # noqa: BLE001 - opening a browser is best-effort
            pass


def _run_window(server: uvicorn.Server, sock: socket.socket, url: str) -> bool:
    """Run the app in a pywebview window. Returns False if pywebview is missing."""
    try:
        import webview  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError:
        print(
            "The native window needs pywebview. Install it with:\n"
            "    pip install 'markitforge-gui[window]'\n"
            "Opening in your browser instead.",
            file=sys.stderr,
        )
        return False

    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        settings = getattr(webview, "settings", None)
        if isinstance(settings, dict) and "ALLOW_DOWNLOADS" in settings:
            settings["ALLOW_DOWNLOADS"] = True
        webview.create_window("MarkitForge", url, width=1240, height=820)
        webview.start()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.max_upload_mb <= 0:
        print("--max-upload-mb must be greater than 0.", file=sys.stderr)
        return 2
    if not 0 <= args.port <= 65535:
        print("--port must be between 0 and 65535.", file=sys.stderr)
        return 2

    if not is_loopback(args.host):
        print(
            f"WARNING: listening on {args.host!r} makes MarkitForge reachable from "
            "other computers on your network. Anyone who can reach it can convert "
            "files and read the results. Only do this on a network you trust.",
            file=sys.stderr,
        )

    try:
        sock = bind_socket(args.host, args.port)
    except OSError as exc:
        print(f"Could not start the server on {args.host}: {exc}", file=sys.stderr)
        return 1
    port = sock.getsockname()[1]
    if args.port and port != args.port:
        print(f"Port {args.port} is busy, using port {port} instead.", flush=True)

    app = create_app(
        max_upload_mb=args.max_upload_mb, allowed_hosts=_allowed_hosts(args.host)
    )
    config = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    url = _display_url(args.host, port)

    print(f"MarkitForge is running at {url}", flush=True)

    if args.window and _run_window(server, sock, url):
        return 0

    print(
        "Keep this window open while you use MarkitForge. Press Ctrl+C to stop.",
        flush=True,
    )
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready, args=(server, url), daemon=True
        ).start()
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0
