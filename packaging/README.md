<!--
SPDX-FileCopyrightText: 2026-present MarkitForge contributors
SPDX-License-Identifier: Apache-2.0
-->

# Packaging MarkitForge

This folder builds the MarkitForge GUI as a desktop app that people can install
without Python, and documents the Docker images.

| Artifact | Built by | Output |
| --- | --- | --- |
| macOS app (Apple Silicon) | `packaging/build_macos.sh` or CI | `MarkitForge.app`, `MarkitForge-<version>-macos-arm64.dmg` |
| Windows app (x64) | PyInstaller with `markitforge_gui.spec`, or CI | `MarkitForge\MarkitForge.exe` folder, zipped in CI |
| Docker, CLI | `docker build .` | image with `markitforge` as entry point |
| Docker, GUI | `docker build --target gui .` | image running the web app on port 8765 |

Files:

- `markitforge_gui.spec`: PyInstaller spec (one-folder build). It produces
  `MarkitForge.app` on macOS and `MarkitForge.exe` on Windows from the same file.
- `launch_gui.py`: the app's entry script. It calls the `markitforge-gui` entry
  point (`markitforge_gui._cli.main`) and nothing else from the GUI package, so
  new GUI features are picked up by rebuilding.
- `make_icons.py`: draws `assets/brand/markitforge-icon.svg` into `.png`, `.ico`
  and `.icns` (Pillow only). The spec runs it if the icons are missing.
- `collect_licenses.py`: writes `THIRD_PARTY_LICENSES.txt` (MarkitForge's
  LICENSE, LICENSE-APACHE and NOTICE, a summary of copyleft licenses and of
  packages without a license file, then name, version, declared license and
  the full license files of every bundled distribution, plus CPython and the
  PyInstaller bootloader). It runs before PyInstaller; the file is bundled in
  `MarkitForge.app/Contents/Resources/`, next to `MarkitForge.exe`, in the
  `.dmg` root and in the Windows zip. The spec runs it itself when
  `MARKITFORGE_LICENSES_FILE` is not set.
- `build_macos.sh`: builds the app, signs it ad hoc and creates the `.dmg`.
- `smoke_test.py`: starts a built app with `--no-browser --no-history`, checks
  the page, reads the session token from it and converts `test.docx` and
  `test.pdf` through the HTTP API (following the background job), checks that
  requests without the client header or token are refused, then stops it.
- `requirements-build.txt`: pinned build tools (PyInstaller, pywebview) and the
  optional runtime extras bundled into the app (openai, pytesseract).

## How the app behaves

Double-clicking the app starts the local server on `127.0.0.1` (port 8765, or
a free one) and opens it in a native window (pywebview: WebKit on macOS, Edge
WebView2 on Windows). Closing the window or quitting the app stops the server.
If the window cannot be opened, the app falls back to the default browser.

On Windows the app also has a console window: it shows the address and closing
it stops MarkitForge.

Results are kept between launches in the GUI's data folder (macOS
`~/Library/Application Support/MarkitForge`, Windows `%LOCALAPPDATA%\MarkitForge`)
unless the app is started with `--no-history`. Apps opened from Finder get a
minimal `PATH`; the launcher appends `/opt/homebrew/bin` and `/usr/local/bin`
so Homebrew-installed helpers (exiftool, ffmpeg, tesseract, flac) are found.

Command line arguments are passed through to `markitforge-gui`, for example:

```sh
# macOS
/Applications/MarkitForge.app/Contents/MacOS/MarkitForge --no-browser --port 8991
# Windows
MarkitForge\MarkitForge.exe --no-browser --port 8991
```

## Build locally

Use a separate virtual environment for the build (not the development one) so
nothing extra ends up in the bundle. From the repository root:

### macOS

```sh
uv venv /tmp/mf-build/venv --python 3.12
VIRTUAL_ENV=/tmp/mf-build/venv uv pip install \
  "./packages/markitforge[all]" ./packages/markitforge-gui -r packaging/requirements-build.txt
PYTHON=/tmp/mf-build/venv/bin/python packaging/build_macos.sh /tmp/mf-build
/tmp/mf-build/venv/bin/python packaging/smoke_test.py \
  /tmp/mf-build/dist/MarkitForge.app/Contents/MacOS/MarkitForge
```

Results: `/tmp/mf-build/dist/MarkitForge.app` and
`/tmp/mf-build/MarkitForge-<version>-macos-arm64.dmg`. Without an argument the
script writes to `packaging/build/` (ignored by git).

The app is built for the machine's architecture (arm64 on Apple Silicon). An
Intel build needs an Intel Mac or runner (a universal2 build would need every
binary wheel to be universal2, which onnxruntime and others are not).

### Windows (PowerShell)

```powershell
py -3.12 -m venv C:\mf-build\venv
C:\mf-build\venv\Scripts\python -m pip install "./packages/markitforge[all]" ./packages/markitforge-gui -r packaging/requirements-build.txt
C:\mf-build\venv\Scripts\python -m PyInstaller --noconfirm --clean `
  --distpath C:\mf-build\dist --workpath C:\mf-build\work packaging\markitforge_gui.spec
C:\mf-build\venv\Scripts\python packaging\smoke_test.py C:\mf-build\dist\MarkitForge\MarkitForge.exe
```

Ship the whole `C:\mf-build\dist\MarkitForge` folder (for example as a zip).

### Docker

```sh
docker build -t markitforge .                       # CLI (default target)
docker run --rm -i markitforge < report.pdf > report.md
docker run --rm -v "$PWD:/data" markitforge report.docx -o report.md

docker build --target gui -t markitforge-gui .      # web app
docker run --rm -p 127.0.0.1:8765:8765 markitforge-gui
# open http://localhost:8765/ on this computer
```

- Both images run as the unprivileged user `markitforge` (uid/gid 10001; change
  with `--build-arg USERID=... --build-arg GROUPID=...`). The working directory
  is `/data`; a mounted folder must be readable (and writable for `-o`) by that
  user. To run as yourself instead, use
  `docker run --user "$(id -u):$(id -g)" -e HOME=/tmp ...` (LibreOffice needs a
  writable home folder).
- LibreOffice (headless, `*-nogui` packages), exiftool, ffmpeg and flac are
  included, plus the `openai` client for the optional AI features.
  `--build-arg WITH_LIBREOFFICE=false` builds an image without LibreOffice
  (about 0.55 GB smaller unpacked).
- The GUI keeps its history in `/home/markitforge/.local/share/markitforge`.
  Mount a volume there to keep it across container restarts
  (`-v markitforge-gui:/home/markitforge/.local/share/markitforge`), or pass
  the arguments yourself to switch it off:
  `docker run --rm -p 127.0.0.1:8765:8765 markitforge-gui --host 0.0.0.0 --no-browser --port 8765 --no-history`.
- An AI service on the host (for example Ollama) is not `localhost` inside the
  container; use `http://host.docker.internal:11434/v1` (Docker Desktop) as the
  address.
- The GUI image listens on `0.0.0.0` inside the container, but the app only
  accepts requests whose `Host` is a loopback name (`localhost`, `127.0.0.1`,
  `[::1]`) or the container's own name/address. That blocks DNS-rebinding
  attacks. So publish the port on loopback (`-p 127.0.0.1:8765:8765`) and open
  `http://localhost:8765/` (any host port works, e.g. `-p 127.0.0.1:9000:8765`
  and `http://localhost:9000/`). Opening it through the host's LAN name or IP
  is rejected (HTTP 400) on purpose; the app has no login, so do not expose
  it to a network.
- The build stages install the packages into `/opt/venv`; only that virtualenv
  is copied into the final images, which contain no compiler or build tools.

## CI

`.github/workflows/release-apps.yml` runs on `v*` tags and on manual dispatch:

1. `macos` (macos-latest, arm64): installs the packages and
   `requirements-build.txt`, runs `build_macos.sh`, smoke-tests the app and
   uploads the `.dmg` (artifact `app-macos`).
2. `windows` (windows-latest, x64): collects the licenses, builds with the spec, smoke-tests
   `MarkitForge.exe`, zips the folder (artifact `app-windows`).
3. `release` (tags only): attaches the `.dmg`, `.zip` and SHA-256 sums to the
   GitHub Release of the tag, creating a draft release if there is none yet.
   Only this job has `contents: write`.

## Signing and notarization

The macOS app is signed **ad hoc** only (`codesign -s -`). Downloaded from the
internet it is quarantined, and Gatekeeper refuses to open it with a message
that the developer cannot be verified. Users can open it once with right-click
> Open (or System Settings > Privacy & Security > Open Anyway), or remove the
quarantine flag: `xattr -dr com.apple.quarantine /Applications/MarkitForge.app`.

Proper distribution needs an Apple Developer ID (paid Apple Developer Program
membership). This is a manual step for the maintainer and is not automated
here:

```sh
# 1. Sign with the hardened runtime (entitlements needed for Python/onnxruntime
#    code loading may include com.apple.security.cs.disable-library-validation).
codesign --force --deep --options runtime --timestamp \
  --sign "Developer ID Application: <Name> (<TEAMID>)" MarkitForge.app
# 2. Build the .dmg (as in build_macos.sh), sign it, then notarize and staple.
codesign --sign "Developer ID Application: <Name> (<TEAMID>)" --timestamp MarkitForge-<version>-macos-arm64.dmg
xcrun notarytool submit MarkitForge-<version>-macos-arm64.dmg \
  --keychain-profile <profile> --wait
xcrun stapler staple MarkitForge-<version>-macos-arm64.dmg
```

To automate it, store the certificate and an App Store Connect API key as
repository secrets and add these steps to the `macos` job.

Windows builds are unsigned, so SmartScreen warns on first start ("Windows
protected your PC" > More info > Run anyway). Signing needs an Authenticode
code-signing certificate (`signtool sign /fd SHA256 /tr <timestamp-url> ...`).

## Known limits

- **LibreOffice is not bundled** in the desktop app. Legacy Office files
  (`.doc`, `.xls`, `.ppt`), OpenDocument, RTF and similar formats need a local
  LibreOffice install (macOS: `brew install --cask libreoffice`), or set
  `MARKITFORGE_SOFFICE` to the `soffice` program. The Docker images include it.
- **Tesseract is not bundled** (in the app or the images). `pytesseract` is
  included in the app, so local OCR works once `tesseract` is installed
  (macOS: `brew install tesseract`). OCR through an LLM needs a provider: an
  OpenAI-compatible service set in the app's AI settings or in `config.toml`
  (`[llm]`), for example a local Ollama or an API with a key. The `openai`
  client is bundled; no service is.
- **exiftool and ffmpeg are not bundled** in the app. Image metadata needs
  `exiftool`, audio transcription needs `ffmpeg`/`ffprobe` and a `flac`
  encoder on every platform. SpeechRecognition's bundled encoders are the
  GPL-licensed FLAC tool (and the macOS one is an old x86_64-only binary), so
  they are left out of all app builds. Audio transcription also uses an online
  speech service.
- **Plugins** (`markitforge.plugin` entry points such as `markitforge-ocr`) are
  not available in the frozen app: it contains only the packages that were
  installed when it was built.
- **docling** (the optional PDF layout backend) and PyTorch are excluded to
  keep the app small; `pdf_backend = "docling"` reports a missing dependency.
- The app is about 195 MB installed (the `.dmg` about 82 MB); onnxruntime,
  used by magika for file-type detection, is the largest part (70 MB). The
  Docker images are about 2 GB unpacked (530 MB compressed), mostly LibreOffice.
- The minimum macOS version is taken from the bundled binaries (macOS 14 with
  the current onnxruntime wheels) and written to `LSMinimumSystemVersion`.
- `THIRD_PARTY_LICENSES.txt` is built from package metadata. The list is the
  dependency closure of the app's packages, so it can name a few packages that
  PyInstaller then leaves out (it never misses one that is bundled). Native
  libraries inside wheels are covered by the license files those wheels ship.
  Read its summary before each release: some packages publish no license file
  (only a license name); add their upstream license text by hand before
  publishing a release (currently cobble, flatbuffers, magika, proxy_tools and
  two pyobjc frameworks). The GPL FLAC encoders shipped with SpeechRecognition
  are not bundled.
