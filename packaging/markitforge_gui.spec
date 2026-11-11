# -*- mode: python ; coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# PyInstaller spec for the MarkitForge desktop app (one-folder build).
#
#   pyinstaller --noconfirm --clean packaging/markitforge_gui.spec
#
# macOS: dist/MarkitForge.app (plus the bare folder dist/MarkitForge/)
# Windows: dist/MarkitForge/MarkitForge.exe
#
# Build from an environment that has markitforge[all], markitforge-gui,
# pyinstaller and (optionally, for the native window) pywebview installed.
# See packaging/README.md.

import os
import re
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

PACKAGING = Path(SPECPATH)  # noqa: F821 - provided by PyInstaller
ROOT = PACKAGING.parent
ABOUT = ROOT / "packages" / "markitforge-gui" / "src" / "markitforge_gui" / "__about__.py"
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', ABOUT.read_text("utf-8")).group(1)
# CFBundleShortVersionString must be numeric (1.2.3): drop pre-release tags.
SHORT_VERSION = re.match(r"\d+(\.\d+){0,2}", VERSION).group(0)
BUNDLE_ID = "io.github.marcel-bonifacio.markitforge"

# ---- icons ------------------------------------------------------------------
ICON_DIR = Path(os.environ.get("MARKITFORGE_ICON_DIR") or Path(workpath) / "icons")  # noqa: F821
if not (ICON_DIR / "markitforge.ico").exists() or not (ICON_DIR / "markitforge.icns").exists():
    subprocess.run(
        [sys.executable, str(PACKAGING / "make_icons.py"), "--out", str(ICON_DIR)],
        check=True,
    )
ICON = str(ICON_DIR / ("markitforge.icns" if IS_MAC else "markitforge.ico"))

# ---- third-party licenses ---------------------------------------------------
# THIRD_PARTY_LICENSES.txt (packaging/collect_licenses.py) ships with the app:
# macOS Contents/Resources, Windows next to MarkitForge.exe.
LICENSES = Path(
    os.environ.get("MARKITFORGE_LICENSES_FILE")
    or Path(workpath) / "THIRD_PARTY_LICENSES.txt"  # noqa: F821
)
if not os.environ.get("MARKITFORGE_LICENSES_FILE") or not LICENSES.exists():
    subprocess.run(
        [sys.executable, str(PACKAGING / "collect_licenses.py"), "--out", str(LICENSES)],
        check=True,
    )

# ---- what to bundle -----------------------------------------------------------


def _optional(func, *names):
    """Collect for packages that may not be installed (no error if missing)."""
    out = []
    for name in names:
        try:
            out += func(name)
        except Exception:  # noqa: BLE001
            pass
    return out


datas = []
# Top level of the bundle: Contents/Resources in the .app (Windows: _internal,
# and copied next to the exe below).
datas += [(str(LICENSES), ".")]
# The GUI's web page (static/), and any data files the core package ships.
datas += collect_data_files("markitforge_gui")
datas += collect_data_files("markitforge")
# Packages that read files next to their modules at runtime. Most of these
# also have a hook in pyinstaller-hooks-contrib; listing them is harmless and
# keeps the bundle correct if a hook is missing or changes.
datas += _optional(
    collect_data_files,
    "magika",  # ONNX model + content-type knowledge base
    "pdfminer",  # CMaps for CJK and other encoded fonts
    "pptx",  # default templates
    "mammoth",
    "certifi",  # CA bundle for URL conversions
    "tzdata",
    "dateutil",
    "youtube_transcript_api",
    "speech_recognition",
)
# Distribution metadata: version lookups (importlib.metadata) at runtime.
datas += _optional(
    copy_metadata,
    "markitforge",
    "markitforge-gui",
    "magika",
    "pdfminer.six",
    "pdfplumber",
    "python-pptx",
    "mammoth",
    "starlette",
    "uvicorn",
    "requests",
    "youtube-transcript-api",
    "azure-ai-documentintelligence",
    "azure-ai-contentunderstanding",
    "azure-core",
    "azure-identity",
    "pywebview",
    "openai",
    "pytesseract",
)

hiddenimports = []
# Converters and helpers are imported normally, but collecting every submodule
# guards against imports done lazily or by name (and new converters added
# later by the core package).
hiddenimports += collect_submodules("markitforge")
hiddenimports += collect_submodules("markitforge_gui")
# Optional extras that are imported inside functions (bundled when installed).
hiddenimports += _optional(collect_submodules, "openai")
hiddenimports += _optional(collect_submodules, "pytesseract")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "multipart",
    "python_multipart",
]
if IS_MAC:
    hiddenimports += ["webview.platforms.cocoa"]
elif IS_WIN:
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium"]

excludes = [
    # Heavy optional extras the desktop app does not ship.
    "torch",
    "torchvision",
    "docling",
    "docling_core",
    "transformers",
    "tensorflow",
    "matplotlib",
    "scipy",
    "IPython",
    "notebook",
    "jupyter",
    "tkinter",
    "_tkinter",
    # Other pywebview backends (only cocoa / winforms are used).
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "qtpy",
    "gi",
    "cefpython3",
    # Test and build tooling.
    "pytest",
    "_pytest",
    "hypothesis",
    "setuptools",
    "pip",
    "PyInstaller",
]

a = Analysis(  # noqa: F821
    [str(PACKAGING / "launch_gui.py")],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Drop files that are never used by the app:
# * speech_recognition's offline Sphinx models (~37 MB; the converter uses the
#   online recognizer) and its FLAC encoders for other platforms. On macOS
#   the bundled encoder is dropped too: it is an old x86_64-only binary that
#   breaks notarization; a Homebrew ``flac`` on PATH is used instead.
#   The bundled encoders are the GPL-licensed FLAC command-line tool, so none
#   are shipped on any platform: audio transcription uses a ``flac`` program
#   the user installs (Homebrew, apt, or https://xiph.org/flac/ on Windows),
# * test suites that some packages ship inside their wheels.
_flac_keep = None


def _keep(dest: str) -> bool:
    d = dest.replace("\\", "/")
    if "speech_recognition/pocketsphinx-data" in d:
        return False
    if "speech_recognition/flac-" in d and (_flac_keep is None or _flac_keep not in d):
        return False
    parts = d.split("/")
    if any(p in ("tests", "test") for p in parts[:-1]):
        return False
    return True


a.datas = [entry for entry in a.datas if _keep(entry[0])]
a.binaries = [entry for entry in a.binaries if _keep(entry[0])]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MarkitForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windows keeps a console: it shows where the app runs and closing it
    # stops the server. macOS uses a regular windowed .app.
    console=not IS_MAC,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MarkitForge",
)

if IS_WIN:
    import shutil

    shutil.copy2(LICENSES, Path(DISTPATH) / "MarkitForge" / LICENSES.name)  # noqa: F821

if IS_MAC:
    # The oldest macOS the bundle runs on is set by its newest binary (wheels
    # such as onnxruntime are built for recent macOS releases only).
    from PyInstaller.utils.osx import macosx_version_min

    _min = (11, 0, 0)
    for _dest, _src, _kind in a.binaries:
        try:
            _min = max(_min, macosx_version_min(_src))
        except Exception:  # noqa: BLE001 - not a Mach-O file, or unreadable
            pass
    MIN_MACOS = f"{_min[0]}.{_min[1]}"

    app = BUNDLE(  # noqa: F821
        coll,
        name="MarkitForge.app",
        icon=ICON,
        bundle_identifier=BUNDLE_ID,
        version=SHORT_VERSION,
        info_plist={
            "CFBundleName": "MarkitForge",
            "CFBundleDisplayName": "MarkitForge",
            "CFBundleShortVersionString": SHORT_VERSION,
            "CFBundleVersion": SHORT_VERSION,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "LSMinimumSystemVersion": MIN_MACOS,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "MarkitForge contributors. Apache-2.0 / MIT.",
        },
    )
