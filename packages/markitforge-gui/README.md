# MarkitForge GUI

> Part of [MarkitForge](https://github.com/Marcel-Bonifacio/MarkitForge), a fork of [MarkItDown](https://github.com/microsoft/markitdown) (MIT, Copyright (c) Microsoft Corporation). This package is new MarkitForge code under the Apache License 2.0; see `LICENSE-APACHE` and `NOTICE`.

A friendly app for converting files to Markdown without using a terminal.
Drop in PDFs, Word, PowerPoint and Excel files, web pages, EPUBs, images and
more. You can then read the result, look at the raw Markdown, check the
document details, and download the `.md` file.

Everything runs on your own computer. The app is a small local web server
that only your own browser can reach, and your files are not uploaded to the
internet. Web pages and YouTube links you paste are downloaded directly from
the site.

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
* **Web pages.** Paste a link (an article, a Wikipedia page, a YouTube video)
  into **Or convert a web page** and press **Convert**.
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
* **Results.** Your files are listed on the left. Each one shows whether it is
  still converting, finished, or failed, and a failed file shows the reason.
  Select a file to see three tabs:
  * **Preview** shows the Markdown formatted, with tables and code blocks.
  * **Markdown** shows the raw `.md` text with line numbers.
  * **Details** shows any warnings, the list of pages, document information
    (author, dates and so on) and embedded items. Type a page range such as
    `1-3, 5` and the preview, copy and download then use only those pages.
* **Save.** Use **Download .md**, **Copy**, or **Download all (.zip)** for
  every finished file.
* **Open .md file** opens a Markdown file you already have, so you can read it
  formatted.

The app follows your system's light or dark setting, works in a narrow window,
and can be used with the keyboard. Use Tab to move around, the arrow keys to
switch tabs, and Up/Down to move through the file list.

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
| `--max-upload-mb N`   | Largest file you can upload (default `200`). |
| `--window`            | Open in a native window instead of the browser. Needs `pip install 'markitforge-gui[window]'` (pywebview). Without it, the browser opens instead. |

## HTTP API

The page uses this API, and scripts can use it too. Every `POST` must send
the header `X-MarkitForge-Client: 1`. Requests from other websites are
refused.

| Endpoint | Description |
|----------|-------------|
| `GET /api/info` | Version and upload limit. |
| `POST /api/convert` | Multipart form with `file` and an optional `options` (JSON text). Returns `{id, filename, md_filename, markdown, html, title, metadata, pages[{index,kind,title}], warnings, assets[{name,mime_type,size,description}]}`. |
| `POST /api/convert-url` | JSON `{url, options}`. Only `http(s)` addresses are accepted. Returns the same shape as `/api/convert`. |
| `POST /api/render` | JSON `{markdown}` returns `{markdown, html}`. JSON `{id, pages: "1-3,5"}` renders only those pages of an earlier result. |
| `GET /api/download/{id}[?pages=1-3]` | The Markdown as a `text/markdown` attachment. |
| `GET /api/zip[?ids=a,b]` | A ZIP of the listed results, or of all kept results. |

Options are `page_markers`, `include_hidden_sheets`, `include_headers_footers`
and `keep_data_uris` (true/false), plus `table_format`
(`markdown`|`html`|`auto`) and `pdf_backend`
(`auto`|`pdfplumber`|`pdfminer`|`docling`).

Errors come back as JSON `{error, hint?, detail?}` with a plain-language
`error`. For example, an unsupported file returns 415 with "This file type is
not supported yet", and a missing optional component returns 422 with the
install command in `hint`.

The 50 most recent results are kept in memory and the oldest are dropped
first. Nothing is written to disk except a temporary copy of each upload,
which is deleted once its conversion finishes.

## Security notes

Converted documents are treated as untrusted. The Markdown is rendered to HTML
on the server with markdown-it-py and then cleaned with
[nh3](https://github.com/messense/nh3) against an allow-list. Scripts, event
handlers, `javascript:` links, styles, iframes and forms are removed. Images
may only be embedded `data:` images or relative paths, so a document can't
load anything from the internet. The page also sends a strict
Content-Security-Policy.

By default the server only listens on `127.0.0.1` and only answers requests
whose `Host` is a loopback name, which blocks DNS-rebinding attacks. Uploads
are limited to `--max-upload-mb`.

## Development

```bash
cd packages/markitforge-gui
python -m pytest -q --timeout 120
```

The UI is plain HTML, CSS and JavaScript in `src/markitforge_gui/static/`.
There is no build step and nothing is loaded from a CDN, so the app works
offline.
