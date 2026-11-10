# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Entry script for the packaged MarkitForge desktop app (PyInstaller).

It only calls the public ``markitforge-gui`` entry point, so the bundle picks up
whatever the GUI package offers at build time. Two things are added for the
frozen app:

* When the app is started without arguments (double-click) and pywebview is
  bundled, it opens in a native window (``--window``) instead of a browser
  tab, so closing the window quits the app. Any arguments given on the
  command line are passed through unchanged, e.g.
  ``MarkitForge --no-browser --port 8991``.
* Windowed apps have no console on Windows (``sys.stdout`` is ``None``), which
  breaks logging; output then goes to a log file instead.
* Apps opened from the macOS Finder get a minimal ``PATH`` without Homebrew,
  so helper programs (exiftool, ffmpeg, tesseract, soffice) installed with
  Homebrew would not be found; the Homebrew folders are appended.
"""

import multiprocessing
import os
import sys
from pathlib import Path


def _log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "MarkitForge"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "MarkitForge" / "Logs"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "markitforge"


def _ensure_streams() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stream = open(
            folder / "markitforge-gui.log", "a", encoding="utf-8", buffering=1
        )
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _extend_path() -> None:
    if sys.platform != "darwin":
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        if folder not in parts and os.path.isdir(folder):
            parts.append(folder)
    os.environ["PATH"] = os.pathsep.join(p for p in parts if p)


def _has_webview() -> bool:
    try:
        import webview  # noqa: F401  # type: ignore[import-not-found,unused-ignore]
    except Exception:  # noqa: BLE001 - any import problem means "no window"
        return False
    return True


def run() -> int:
    _ensure_streams()
    if getattr(sys, "frozen", False):
        _extend_path()
    from markitforge_gui._cli import main

    args = sys.argv[1:]
    # macOS passes -psn_* when an app is opened from Finder on old releases.
    args = [a for a in args if not a.startswith("-psn_")]
    if args or not getattr(sys, "frozen", False) or not _has_webview():
        return main(args)
    try:
        return main(["--window"])
    except Exception as exc:  # noqa: BLE001 - fall back to the browser
        print(
            f"Could not open the app window ({exc}); using the browser.",
            file=sys.stderr,
        )
        return main([])


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run())
