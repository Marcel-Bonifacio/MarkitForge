# MarkitForge GUI

> Part of [MarkitForge](https://github.com/Marcel-Bonifacio/MarkitForge), a fork of [MarkItDown](https://github.com/microsoft/markitdown) (MIT, Copyright (c) Microsoft Corporation). This package is new MarkitForge code under the Apache License 2.0; see `LICENSE-APACHE` and `NOTICE`.

A friendly app for converting files to Markdown without using a terminal.
Drop in PDFs, Word, PowerPoint and Excel files, web pages, EPUBs, images and
more. You can then read the result, look at the raw Markdown, check the
document details, and download the `.md` file.

Everything runs on your own computer. The app is a small local web server
that only your own browser can reach, and your files are not uploaded to the
internet. Web pages and YouTube links you paste are downloaded directly from
the site. The optional AI options use a model on this computer (Ollama) by
default; if you choose an online AI service instead, page images are sent
there.

## Start it without a terminal

The `launchers/` folder in this package has double-click starters:

| System  | File                                             |
|---------|--------------------------------------------------|
| macOS   | `packages/markitforge-gui/launchers/MarkitForge.command` |
| Windows | `packages/markitforge-gui/launchers/MarkitForge.bat`     |

1. Download or clone the whole MarkitForge folder. The launchers install
   MarkitForge from that folder, so keep them where they are.
2. Double-click the launcher for your system.
3. **The first start takes a few minutes.** The launcher creates a private
   Python environment in `launchers/.venv` and installs MarkitForge into it.
   It uses [`uv`](https://docs.astral.sh/uv/) if you have it, and otherwise
   uses Python 3.10 or newer from [python.org](https://www.python.org/downloads/).
   On Windows, tick "Add python.exe to PATH" when you install Python.
4. Your browser opens MarkitForge. Keep the small terminal window open while
   you use it. Close that window to stop MarkitForge.

Later starts take a few seconds. To reinstall from scratch, for example after
moving the folder, delete `launchers/.venv` and double-click the launcher again.

> **macOS:** if you get a message that the file "cannot be opened because it
> is from an unidentified developer", right-click (or Control-click) the
> `.command` file, choose **Open**, and confirm. You only need to do this once.
> If macOS says you don't have permission, run
> `chmod +x packages/markitforge-gui/launchers/MarkitForge.command` once.

## Using the app

* **Add files.** Drag files anywhere onto the window, or click
  **Choose files**. You can add several at once, and each one is converted
  separately.
* **Add a folder.** Click **Choose folder**, or drag a folder onto the
  window. Every file inside it (and in its subfolders) is converted. Hidden
  files are skipped, and at most 500 files are added at a time. The file list
  shows each file's path inside the folder, and **Download all (.zip)** keeps
  the same folder structure.
* **Web pages.** Paste a link (an article, a Wikipedia page, a YouTube video)
  into **Or convert a web page** and press **Convert**. Addresses on this
  computer or your local network are refused unless you start MarkitForge
  with `--allow-private-urls`.
* **Progress and cancelling.** Each file shows a progress bar while it
  converts (a moving bar when the number of pages is not known yet). Select a
  file that is still converting and press **Cancel** to stop it.
* **Options** (they apply to the next files you convert):
  * *Mark where each page starts* adds a marker before every page, slide or
    sheet.
  * *Tables*: simple Markdown tables, HTML tables (these keep merged cells),
    or automatic.
  * *PDF reading engine*: automatic, pdfplumber, pdfminer, or Docling.
    Docling needs an extra install.
  * *Include hidden spreadsheet sheets* (Excel) and *Include page headers and
    footers* (Word).
  * *Keep pictures inside the Markdown* embeds images in the text, which makes
    the file much larger.
* **AI options** (see [AI options](#ai-options) below): read scanned pages
  (OCR), describe images, turn formulas into LaTeX, and save images.
* **Results.** Your files are listed on the left. Each one shows whether it is
  still converting, finished, cancelled or failed, and a failed file shows the
  reason. Select a file to see four tabs:
  * **Preview** shows the Markdown formatted, with tables and code blocks.
  * **Markdown** shows the raw `.md` text with line numbers, and you can edit
    it. The preview follows your edits, and **Copy**, **Download .md** and
    **Download all (.zip)** use the edited text. Edits are saved
    automatically (the tab shows "edited"). Tab inserts a tab character;
    press Escape and then Tab to leave the editor with the keyboard.
  * **Original** shows the file you uploaded: PDFs in the browser's PDF
    viewer, pictures as pictures, and text files (txt, csv, md, json, xml,
    html and similar) as plain text. Word, PowerPoint and Excel files are
    shown as a PDF when LibreOffice is installed; otherwise the app says that
    no preview is available. Originals up to 50 MB are kept while the app
    runs.
  * **Details** shows any warnings, the list of pages, document information
    (author, dates and so on) and embedded items. Type a page range such as
    `1-3, 5` and the preview, copy and download then use only those pages.
    Page selection is turned off once you have edited the Markdown.
* **Side by side.** In a wide window, **Side by side** shows the original on
  the left and the preview (or Markdown, or Details) on the right.
* **Save.** Use **Download .md**, **Copy**, or **Download all (.zip)** for
  every finished file. When images were saved, the download becomes a `.zip`
  with the `.md` file and a `<name>_assets/` folder next to it.
* **Open .md file** opens a Markdown file you already have, so you can read it
  formatted (and edit it before downloading it again).
* **History.** Finished results are kept when you close MarkitForge and are
  listed again next time ("Saved" plus the date). **Clear list** only empties
  the list on screen; **Clear history** deletes the saved results from your
  computer (it asks first).
* **Light and dark.** The switch at the top right chooses **System** (follow
  your computer's setting, the default), **Light** or **Dark**. The choice is
  remembered in this browser.

The app works in a narrow window (down to about 400 pixels wide) and can be
used with the keyboard. Use Tab to move around, the arrow keys to switch tabs
and themes, and Up/Down to move through the file list.

## AI options

These need an AI model that can read images. Open **AI options** on the left.

| Option | What it does |
|--------|--------------|
| Read scanned pages (OCR) | Turns pages that are only a picture of text into text. *Read them with*: Automatic, Tesseract (runs on this computer, needs Tesseract installed) or the AI model. |
| Describe images | Adds a short description of each picture. |
| Turn formulas into LaTeX | Math in PDFs becomes LaTeX. |
| Save images | Keeps the document's pictures as files. They show in the preview and are put in the `.zip` download next to the `.md` file, with the image links pointing at `<name>_assets/<file>`. This does not use AI. |

**AI provider**

* **Local (Ollama)** keeps your files on this computer. Install
  [Ollama](https://ollama.com), run `ollama pull llama3.2-vision`, and keep
  the default address `http://localhost:11434/v1` and model
  `llama3.2-vision`.
* **OpenAI-compatible service**: enter the service's address (for example
  `https://api.openai.com/v1`), a model name and your API key. **With this
  choice, page images and pictures from your files are sent to that
  service** when an AI option is ticked. The panel says where your files go.

The API key is kept only in the app's memory while it runs. It is never
written to disk, to the history or to a log, and it is never sent back to the
page (the panel only says "Key set for this session"). Use **Forget key** to
remove it. If no key is typed in, the key is read from the environment
variable named by `api_key_env` in the config file (default
`OPENAI_API_KEY`).

**Test connection** saves the settings and asks the service for its list of
models (or sends a one-word request), with a 10 second time limit. The AI
options need the `openai` Python package (`pip install openai`); the panel
shows this hint when it is missing.

Defaults come from the MarkitForge config file (`~/.config/markitforge/config.toml`,
or `MARKITFORGE_CONFIG`): `[llm] base_url` and `model` pick the provider, and
`[defaults]` pre-fills the options (your own choices in the app win and are
remembered in the browser). `[limits]` always apply, also to web pages.

## Start it from a terminal

```bash
pip install -e 'packages/markitforge[all]' -e packages/markitforge-gui
markitforge-gui            # or: python -m markitforge_gui
```

| Flag                  | Meaning |
|-----------------------|---------|
| `--port N`            | Port to use (default `8765`). If it is busy, a free port is picked and printed. |
| `--no-browser`        | Don't open a browser, just print the address. |
| `--host H`            | Address to listen on (default `127.0.0.1`). Any non-loopback address prints a warning, because other computers can then use the app. |
| `--max-upload-mb N`   | Largest file you can upload (default `200`). Also the largest web page download. |
| `--window`            | Open in a native window instead of the browser. Needs `pip install 'markitforge-gui[window]'` (pywebview). Without it, the browser opens instead. |
| `--no-history`        | Don't keep results between restarts. |
| `--data-dir DIR`      | Where to keep the history (default: see below; or set `MARKITFORGE_GUI_DATA_DIR`). |
| `--allow-private-urls`| Allow converting web addresses on this computer or the local network. |

## Where results are kept

The history lives in a `history` folder inside the per-user data folder:

| System  | Folder |
|---------|--------|
| macOS   | `~/Library/Application Support/MarkitForge/history` |
| Windows | `%LOCALAPPDATA%\MarkitForge\history` |
| Linux   | `$XDG_DATA_HOME/markitforge/history` (usually `~/.local/share/markitforge/history`) |

Each result is one JSON file (Markdown, title, document information, pages
and warnings). Pictures are only stored when *Save images* was on, as files in
`<id>.assets/` next to it. The folders are private to your user (mode 0700),
the files too (0600), and every file is written in one step, so a crash never
leaves half a file. The history keeps at most 200 results and 512 MB; the
oldest are removed first. Uploaded originals are not stored there; they are
kept in memory while the app runs. Settings such as the AI provider or API
key are never stored.

## HTTP API

The page uses this API, and scripts can use it too.

* Every `/api/` request needs the **session token**, which changes every
  time MarkitForge starts. The page gets it from
  `<meta name="mf-token">` in the HTML of `/`. Send it as the header
  `X-MarkitForge-Token`, or, for plain links (`GET` only), as `?t=<token>`.
  Without it the answer is 403 with `code: "token"`.
* Every `POST`, `PUT` and `DELETE` must also send `X-MarkitForge-Client: 1`.
  Requests from other websites are refused.

| Endpoint | Description |
|----------|-------------|
| `GET /api/info` | Version, upload and edit limits, whether the history is on, and config defaults. |
| `POST /api/convert` | Multipart form with `file`, optional `options` (JSON text) and optional `rel_path` (the file's path inside a folder, e.g. `docs/a.pdf`). Starts a job and returns 202 `{job_id, status, progress}`. |
| `POST /api/convert-url` | JSON `{url, options}`. Only `http(s)` addresses. Starts a job like `/api/convert`. |
| `GET /api/jobs/{job_id}` | `{status: queued\|running\|done\|error\|cancelled, progress: {stage, current, total, fraction, message}, cancel_requested}`. When `done` it includes `result`; when `error`, `error` (`{error, hint?, detail?}`) and `http_status`. Finished jobs are forgotten after 15 minutes. |
| `POST /api/jobs/{job_id}/cancel` | Asks the converter to stop. A waiting job is cancelled at once. |
| `GET /api/results/{id}` | A result (from memory or the history). |
| `PUT /api/results/{id}/markdown` | JSON `{markdown}` replaces the result's Markdown (max 32 MB). Downloads and the ZIP use it from then on. |
| `POST /api/render` | JSON `{markdown, for_id?}` returns `{markdown, html}` (`for_id` lets saved images show). JSON `{id, pages: "1-3,5"}` renders only those pages of an earlier result. |
| `GET /api/download/{id}[?pages=1-3]` | The Markdown as a `text/markdown` attachment. |
| `GET /api/zip[?ids=a,b]` | A ZIP of the listed results (or of all results in memory), in their folders, with saved images in `<name>_assets/`. |
| `GET /api/original/{id}/info` | `{kind: pdf\|image\|text\|none, ...}`; `text` includes the decoded text, `none` a `message`. Office files are exported to PDF here when LibreOffice is installed. |
| `GET /api/original/{id}` | The original PDF or image, `inline`, with its own CSP. |
| `GET /api/history` | `{enabled, items: [{id, filename, md_filename, rel_dir, title, created, edited}]}`. |
| `DELETE /api/history` | Deletes the whole history. |
| `GET /api/llm` | AI provider settings: `{provider, base_url, model, key_set, key_source, is_local, openai_available}`. Never the key. |
| `PUT /api/llm` | JSON `{provider: "local"\|"openai", base_url, model, api_key?, clear_key?}`. |
| `POST /api/llm/test` | Tries the AI service and returns `{ok, models, message}`. |

A result looks like `{id, filename, md_filename, rel_dir, markdown, html,
title, metadata, pages[{index,kind,title}], warnings,
assets[{name,mime_type,size,description}], saved_assets, edited,
has_original, created}`.

Options are `page_markers`, `include_hidden_sheets`, `include_headers_footers`,
`keep_data_uris`, `ocr_scanned_pages`, `describe_images`,
`transcribe_formulas` and `extract_images` (true/false), plus `table_format`
(`markdown`|`html`|`auto`), `pdf_backend`
(`auto`|`pdfplumber`|`pdfminer`|`docling`) and `ocr_engine`
(`auto`|`llm`|`tesseract`). An AI client is only passed to the converter when
an option that uses it is on.

Errors come back as JSON `{error, hint?, detail?}` with a plain-language
`error`. For example, an unsupported file returns 415 with "This file type is
not supported yet", and a missing optional component returns 422 with the
install command in `hint`.

The 50 most recent results are kept in memory and the oldest are dropped
first (results in the history can be loaded again). Uploads are written to a
temporary file for the conversion and deleted as soon as it ends, also when
it is cancelled.

## Security notes

Converted documents are treated as untrusted. The Markdown is rendered to HTML
on the server with markdown-it-py and then cleaned with
[nh3](https://github.com/messense/nh3) against an allow-list. Scripts, event
handlers, `javascript:` links, styles, iframes and forms are removed. Images
may only be embedded `data:` images, so a document can't load anything from
the internet or make the page call the API. The page sends a strict
Content-Security-Policy (no inline scripts or styles, nothing from other
sites); the theme is set by a small external `theme.js`.

The original-file view is served with `X-Content-Type-Options: nosniff`,
`Content-Disposition: inline` and its own Content-Security-Policy
(`default-src 'none'; object-src 'self'; frame-ancestors 'self'`; images also
get `sandbox`). Only real PDFs and common raster images are served as such;
text-like files, including HTML and SVG, are only ever shown as text. Office
previews run LibreOffice the same way the converter does: a private profile,
a fixed import filter, no network where the system allows it
(`sandbox-exec` on macOS, `unshare -rn` on Linux), and a time limit.

By default the server only listens on `127.0.0.1` and only answers requests
whose `Host` is a loopback name, which blocks DNS-rebinding attacks. Every API
call also needs the per-launch session token, so other websites and other
programs that only know the port can't use the app or read results. Uploads
are limited to `--max-upload-mb`, edits to 32 MB, and folders to 500 files.
Web addresses are fetched with private-network blocking on (also for every
redirect) unless `--allow-private-urls` is given. Folder paths are cleaned
before they are used in ZIP files (no `..`, no absolute paths, no drive
letters).

## Development

```bash
cd packages/markitforge-gui
python -m pytest -q --timeout 120
```

Tests never touch your real config or data folder: `tests/conftest.py` points
`MARKITFORGE_CONFIG` and `MARKITFORGE_GUI_DATA_DIR` at a temporary folder.

The UI is plain HTML, CSS and JavaScript in `src/markitforge_gui/static/`.
There is no build step and nothing is loaded from a CDN, so the app works
offline.

## Desktop app

MarkitForge is also built as a desktop app that needs no Python install:
`MarkitForge-<version>-macos-arm64.dmg` (Apple Silicon, macOS 14 or newer) and
`MarkitForge-<version>-windows-x64.zip`, attached to each
[GitHub Release](https://github.com/Marcel-Bonifacio/MarkitForge/releases).

- **macOS:** open the `.dmg` and drag MarkitForge to Applications. The app is
  not notarized yet, so the first time right-click it, choose **Open** and
  confirm.
- **Windows:** unzip the folder anywhere and run `MarkitForge.exe`. If
  SmartScreen warns, choose **More info** > **Run anyway**.

The app opens in its own window; closing it stops MarkitForge. Command line
options from this page work too, for example
`MarkitForge.app/Contents/MacOS/MarkitForge --no-browser --port 8991`.
LibreOffice, tesseract, exiftool and ffmpeg are not included: install them
separately for the formats that need them. How the app is built, signed and
released is described in [`packaging/README.md`](../../packaging/README.md).
