<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/markitforge-logo-dark.svg">
    <img alt="MarkitForge" src="assets/brand/markitforge-logo-light.svg" width="440">
  </picture>
</p>

# MarkitForge

Turn PDFs, Word, PowerPoint, Excel, web pages and more into clean, structured Markdown — from Python, the command line, an MCP server, or a **desktop-style app that needs no terminal**.

MarkitForge is a fork of [microsoft/markitdown](https://github.com/microsoft/markitdown) (MIT). It keeps MarkItDown's API (`from markitforge import MarkItDown` still works, and plugins written for `markitdown.plugin` still load) and fixes the problems people hit most often.

> [!IMPORTANT]
> MarkitForge performs I/O with the privileges of the current process. Like open() or requests.get(), it will access resources that the process itself can access. Sanitize your inputs in untrusted environments, and call the narrowest `convert_*` function needed for your use case (e.g., `convert_stream()`, or `convert_local()`). See the [Security Considerations](#security-considerations) section of the documentation for more information.

## What MarkitForge adds

| Area | What changed |
|---|---|
| **PDF** | Tables found on every page (not only form-like pages), headings from font size, per-page sections, document metadata, warnings for scanned pages, optional [Docling](https://github.com/docling-project/docling) backend, LLM image descriptions and formula-to-LaTeX. |
| **Pages** | `--page-markers` puts `<!-- page N -->` between pages / slides / sheets. `--pages 1-3,7` outputs only those pages. |
| **Excel** | Blank cells stay blank (no more `NaN`), whole numbers stay whole, hidden sheets skipped unless `--include-hidden-sheets`. |
| **Word** | Page markers, document properties, optional headers/footers, LLM image descriptions. |
| **Tables** | `--table-format auto` writes HTML tables only where cells are merged, so nothing is lost. |
| **New formats** | `.doc`, `.rtf`, `.odt`, `.odp`, `.ods`, `.ppt`, `.wpd` (via LibreOffice), `.mht/.mhtml` (also OneNote and Word web exports), `.tsv`. |
| **Structured results** | Every result carries `metadata`, `pages`, `assets` and `warnings`; `--json` prints them. |
| **Batch** | `markitforge docs/ -r --glob "*.pdf" -o out/ -j 4` converts whole folders in parallel. |
| **Safety** | URL fetches have a timeout and a size limit. The MCP server refuses `file:` URIs over HTTP unless you allow a folder. |
| **MCP** | New tools: `convert_to_file` (keeps big docs out of the chat), `get_document_info`, `list_supported_formats`; `pages` and `max_chars` on `convert_to_markdown`. |
| **Lighter install** | Magika (file-type sniffing) is optional. |
| **Quality** | A [benchmark](benchmarks/) scores tables, headings, reading order and junk on fixed documents and runs in CI. |

## Desktop app (no terminal needed)

The `markitforge-gui` package is a local app that runs in your browser. Drop files in, see the rendered Markdown next to the raw `.md`, then download or copy it. Nothing leaves your computer.

**Easiest start:** open `packages/markitforge-gui/launchers/` and double-click **`MarkitForge.command`** (macOS) or **`MarkitForge.bat`** (Windows). The first run installs everything into a private folder (a few minutes); later runs start in seconds. You need Python 3.10+ installed ([python.org](https://www.python.org/downloads/)).

From a terminal:

```bash
git clone https://github.com/Marcel-Bonifacio/MarkitForge.git
cd MarkitForge
pip install -e 'packages/markitforge[all]' -e packages/markitforge-gui
markitforge-gui
```

See [`packages/markitforge-gui/README.md`](packages/markitforge-gui/README.md) for options.

## Quality benchmark

`python benchmarks/run_benchmark.py` builds tricky documents from code and scores the output (1.00 = perfect). Same documents, upstream MarkItDown vs MarkitForge:

| Document | What it tests | MarkItDown 0.1.x | MarkitForge |
|---|---|---|---|
| PDF report | headings, ruled table, page break | 0.60 | **1.00** |
| Two-column PDF | heading next to a column, reading order | 0.75 | **1.00** |
| Word, merged cells | headings, table cells | 1.00 | 1.00 |
| Excel, blanks + hidden sheet | no `NaN`, no hidden data, whole numbers | 0.64 | **1.00** |

Full details: [`benchmarks/README.md`](benchmarks/README.md).

## Supported formats

See [docs/FORMATS.md](docs/FORMATS.md), or run `markitforge --list-formats` to see what works on your machine.

NOTE: Image captioning, image descriptions, formula transcription and speech transcription rely on web-hosted services.

## Why Markdown?

Markdown is extremely close to plain text, with minimal markup or formatting, but still
provides a way to represent important document structure. Mainstream LLMs natively "_speak_" Markdown, and often incorporate Markdown into their
responses unprompted. As a side benefit, Markdown conventions are also highly token-efficient.

## Prerequisites
MarkitForge requires Python 3.10 through 3.14. It is recommended to use a virtual environment to avoid dependency conflicts.

```bash
python -m venv .venv
source .venv/bin/activate
```

With `uv`:

```bash
uv venv --python=3.12 .venv
source .venv/bin/activate
# NOTE: use 'uv pip install' rather than just 'pip install' in this environment
```

## Installation

> [!WARNING]
> MarkitForge is **not on PyPI yet**. Do not run `pip install markitforge` (or `markitforge-gui`, `-mcp`, `-ocr`): those names are unclaimed, so anyone could publish a package under them. Install from source, and install the sub-packages together with the core from the same checkout so pip never looks the core up on PyPI.

```bash
git clone https://github.com/Marcel-Bonifacio/MarkitForge.git
cd MarkitForge
pip install -e 'packages/markitforge[all]'
```

To convert `.doc`, `.rtf` and OpenDocument files, also install [LibreOffice](https://www.libreoffice.org/download/).

## Usage

### Command-Line

```bash
markitforge path-to-file.pdf > document.md
```

Or use `-o` to specify the output file:

```bash
markitforge path-to-file.pdf -o document.md
```

You can also pipe content:

```bash
cat path-to-file.pdf | markitforge
```

#### MarkitForge options

```bash
# Page markers, and only some pages
markitforge report.pdf --page-markers
markitforge slides.pptx --pages 2-4

# Everything as JSON (markdown + metadata + pages + warnings)
markitforge report.pdf --json > report.json

# Merged-cell tables as HTML, plain tables as Markdown
markitforge plan.docx --table-format auto

# Whole folder, sub-folders too, 4 at a time
markitforge ./inbox -r --glob "*.pdf" -o ./markdown -j 4

# Hard layouts: use Docling (pip install docling)
markitforge paper.pdf --pdf-backend docling

# Describe images and turn PDF math into LaTeX with an LLM
export OPENAI_API_KEY=...
markitforge paper.pdf --describe-images --transcribe-formulas --llm-model gpt-4o

# What can this machine convert?
markitforge --list-formats
```

Other flags: `--include-hidden-sheets`, `--include-headers-footers`, `--no-magika`, `--fetch-timeout SECONDS`, `--max-download-mb MB`, `--overwrite`. Run `markitforge --help` for all.

### Optional Dependencies
MarkitForge has optional dependencies for activating various file formats. Earlier in this document, we installed all optional dependencies with the `[all]` option. However, you can also install them individually for more control. For example:

```bash
pip install -e 'packages/markitforge[pdf, docx, pptx]'
```

will install only the dependencies for PDF, DOCX, and PPTX files.

At the moment, the following optional dependencies are available:

* `[all]` Installs all optional dependencies
* `[pptx]` Installs dependencies for PowerPoint files
* `[docx]` Installs dependencies for Word files
* `[xlsx]` Installs dependencies for Excel files
* `[xls]` Installs dependencies for older Excel files
* `[pdf]` Installs dependencies for PDF files
* `[outlook]` Installs dependencies for Outlook messages
* `[az-doc-intel]` Installs dependencies for Azure Document Intelligence
* `[az-content-understanding]` Installs dependencies for Azure Content Understanding
* `[audio-transcription]` Installs dependencies for audio transcription of wav and mp3 files
* `[youtube-transcription]` Installs dependencies for fetching YouTube video transcription

### Plugins

MarkitForge also supports 3rd-party plugins. Plugins are disabled by default. To list installed plugins:

```bash
markitforge --list-plugins
```

To enable plugins use:

```bash
markitforge --use-plugins path-to-file.pdf
```

To find available plugins, search GitHub for the hashtag `#markitforge-plugin`. To develop a plugin, see `packages/markitforge-sample-plugin`.

#### markitforge-ocr Plugin

The `markitforge-ocr` plugin adds OCR support to PDF, DOCX, PPTX, and XLSX converters, extracting text from embedded images using LLM Vision — the same `llm_client` / `llm_model` pattern that MarkitForge already uses for image descriptions. No new ML libraries or binary dependencies required.

**Installation:**

```bash
pip install -e packages/markitforge-ocr  # from the repo checkout
pip install openai  # or any OpenAI-compatible client
```

**Usage:**

Pass the same `llm_client` and `llm_model` you would use for image descriptions:

```python
from markitforge import MarkitForge
from openai import OpenAI

md = MarkitForge(
    enable_plugins=True,
    llm_client=OpenAI(),
    llm_model="gpt-4o",
)
result = md.convert("document_with_images.pdf")
print(result.markdown)
```

If no `llm_client` is provided the plugin still loads, but OCR is silently skipped and the standard built-in converter is used instead.

See [`packages/markitforge-ocr/README.md`](packages/markitforge-ocr/README.md) for detailed documentation.

### Azure Content Understanding

[Azure Content Understanding](https://learn.microsoft.com/azure/ai-services/content-understanding/) provides higher-quality conversion with structured field extraction (YAML front matter), multi-modal support (documents, images, audio, video), and configurable analyzers.

Install: `pip install -e 'packages/markitforge[az-content-understanding]'`

#### When to use Content Understanding

Content Understanding is ideal when you need capabilities beyond what built-in or Document Intelligence converters provide:

- **Audio and video files** — CU is the only option for video, and the higher-quality cloud option for audio. Built-in converters have no video support and only basic audio transcription.
- **Structured field extraction** — [Prebuilt](https://learn.microsoft.com/azure/ai-services/content-understanding/concepts/prebuilt-analyzers) or [custom-built](https://learn.microsoft.com/azure/ai-services/content-understanding/how-to/customize-analyzer-content-understanding-studio?tabs=portal) analyzers extract domain-specific fields (invoice amounts, receipt dates, contract clauses) serialized as YAML front matter. Neither built-in nor Doc Intel integration exposes fields.
- **Higher-quality document extraction** — Cloud-based layout analysis and OCR for scanned PDFs, complex tables, and multi-page documents.
- **Single API for all modalities** — One `cu_endpoint` handles documents, images, audio, and video with automatic analyzer routing.

| Capability | Built-in converters | Azure Document Intelligence | Azure Content Understanding |
|------------|---------------------|-----------------------------|-----------------------------|
| Document conversion | Offline, format-specific extraction | Cloud layout extraction | Cloud multimodal extraction |
| Structured fields | Not available | YAML front matter from model or query fields | YAML front matter from analyzer fields |
| Custom analyzers/models | Not available | Supported with `docintel_model_id` | Supported with `cu_analyzer_id` |
| Audio and video | Basic audio, no video | Not supported | Audio and video analyzers |
| Cost | Local compute only | Billable Azure API calls | Billable Azure API calls |

**CLI:**

```bash
markitforge path-to-file.pdf --use-cu --cu-endpoint "<content_understanding_endpoint>"
```

The endpoint can also be set once in the environment, so callers only need `--use-cu`:

```bash
export MARKITFORGE_CU_ENDPOINT="<content_understanding_endpoint>"
markitforge path-to-file.pdf --use-cu
```

**Python API:**

```python
from markitforge import MarkitForge

# Zero-config — auto-selects analyzer per file type
md = MarkitForge(cu_endpoint="<content_understanding_endpoint>")
result = md.convert("report.pdf")   # documents → prebuilt-documentSearch
result = md.convert("meeting.mp4")  # video → prebuilt-videoSearch
result = md.convert("call.wav")     # audio → prebuilt-audioSearch
print(result.markdown)
```

**With a custom analyzer** (for domain-specific field extraction):

```python
md = MarkitForge(
    cu_endpoint="<content_understanding_endpoint>",
    cu_analyzer_id="my-invoice-analyzer",
)
result = md.convert("invoice.pdf")
print(result.markdown)
# Output includes YAML front matter with extracted fields:
# ---
# contentType: document
# fields:
#   VendorName: CONTOSO LTD.
#   InvoiceDate: '2019-11-15'
# ---
# <!-- page 1 -->
# ...
```

When `cu_analyzer_id` is set, the converter automatically scopes it to compatible file types based on the analyzer's modality. Incompatible types (e.g., audio files with a document analyzer) auto-route to default prebuilt analyzers.

**Cost note:** Each `convert()` call for a CU-routed format is a billable Azure API call. Use `cu_file_types` to restrict which formats route to CU:

```python
from markitforge.converters import ContentUnderstandingFileType

md = MarkitForge(
    cu_endpoint="<content_understanding_endpoint>",
    cu_file_types=[ContentUnderstandingFileType.PDF],  # only PDFs use CU
)
```

More information about Azure Content Understanding can be found [here](https://learn.microsoft.com/azure/ai-services/content-understanding/).

### Azure Document Intelligence

To use Microsoft Document Intelligence for conversion:

```bash
export AZURE_API_KEY="<document_intelligence_api_key>"
markitforge path-to-file.pdf -o document.md -d -e "<document_intelligence_endpoint>"
```

The endpoint can also be set once in the environment, so callers only need `-d`:

```bash
export MARKITFORGE_DOCINTEL_ENDPOINT="<document_intelligence_endpoint>"
markitforge path-to-file.pdf -o document.md -d
```

If `AZURE_API_KEY` is not set, MarkitForge uses `DefaultAzureCredential`.

Select a model, configure analysis add-ons, or request fields from the CLI:

```bash
markitforge document.pdf --use-docintel \
  --endpoint "<document_intelligence_endpoint>" \
  --docintel-model-id prebuilt-layout \
  --docintel-features "ocrHighResolution,styleFont" \
  --docintel-query-fields "Title,Author"
```

No analysis add-ons are enabled by default. Use `--docintel-features` to opt in,
or `--docintel-features none` to explicitly request no add-ons. Analysis add-ons
and query fields apply only to OCR-supported inputs such as PDFs and images.

Available feature values are `barcodes`, `formulas`, `keyValuePairs`,
`languages`, `ocrHighResolution`, and `styleFont`. The `formulas`,
`ocrHighResolution`, and `styleFont` features incur additional charges, as does
the `queryFields` add-on, which is priced differently from the other add-ons.
See the
[Document Intelligence add-on documentation](https://learn.microsoft.com/azure/ai-services/document-intelligence/concept/add-on-capabilities?view=doc-intel-4.0.0)
and [pricing](https://azure.microsoft.com/pricing/details/document-intelligence/)
for details.

More information about how to set up an Azure Document Intelligence Resource can be found [here](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/how-to-guides/create-document-intelligence-resource?view=doc-intel-4.0.0)

### Python API

Basic usage in Python:

```python
from markitforge import MarkitForge

md = MarkitForge(enable_plugins=False) # Set to True to enable plugins
result = md.convert("test.xlsx")
print(result.markdown)
```

Structured results and options:

```python
from markitforge import MarkitForge

md = MarkitForge(fetch_timeout=(5, 30), max_download_bytes=50 * 1024**2)
result = md.convert("report.pdf", page_markers=True, table_format="auto")

result.metadata          # {"title": ..., "author": ..., "page_count": 12, ...}
result.pages             # [DocumentSection(index=1, kind="page", markdown=...), ...]
result.warnings          # ["Page 4 has no text layer; it may be a scan. ..."]
result.select_pages("1-3")   # Markdown for pages 1 to 3 only
result.to_dict()         # everything, JSON-ready
```

Document Intelligence conversion in Python using an API key:

```python
from azure.core.credentials import AzureKeyCredential
from markitforge import MarkitForge

md = MarkitForge(
    docintel_endpoint="<document_intelligence_endpoint>",
    docintel_credential=AzureKeyCredential("<document_intelligence_api_key>"),
)
result = md.convert("test.pdf")
print(result.markdown)
```

Omit `docintel_credential` to use `AZURE_API_KEY` when it is set, or
`DefaultAzureCredential` otherwise.

Configure the model, analysis add-ons, and structured query fields:

```python
from azure.ai.documentintelligence.models import DocumentAnalysisFeature
from markitforge import MarkitForge

md = MarkitForge(
    docintel_endpoint="<document_intelligence_endpoint>",
    docintel_model_id="prebuilt-layout",
    docintel_features=[
        DocumentAnalysisFeature.OCR_HIGH_RESOLUTION,
        DocumentAnalysisFeature.STYLE_FONT,
    ],
    docintel_query_fields=["Title", "Author"],
)
result = md.convert("test.pdf")
print(result.markdown)
```

Omit `docintel_features` or set `docintel_features=[]` to request no analysis
add-ons. When query fields are provided, MarkitForge automatically enables the
required `queryFields` add-on.
Models that return structured fields prepend them to the Markdown as YAML front
matter. A single document uses a top-level `fields` mapping. When the service
returns multiple documents, the front matter instead contains a `documents`
list in service order, with a separate `fields` mapping for each document.
Documents without extracted fields retain an empty mapping in that list; no
front matter is emitted when all documents have empty fields. Selection groups
are represented as lists of selected values.

To use Large Language Models for image descriptions (currently only for pptx and image files), provide `llm_client` and `llm_model`:

```python
from markitforge import MarkitForge
from openai import OpenAI

client = OpenAI(max_retries=5)
md = MarkitForge(llm_client=client, llm_model="gpt-4o", llm_prompt="optional custom prompt")
result = md.convert("example.jpg")
print(result.markdown)
```

`max_retries` controls the OpenAI client's automatic retries for retryable errors (the default is 2); `5` allows up to six attempts with backoff. See the [OpenAI SDK retry documentation](https://github.com/openai/openai-python#retries).

If any attempt succeeds, image conversion continues normally. If the client raises an error after exhausting its retries, or encounters a non-retryable error, MarkitForge tries other applicable converters and raises `FileConversionException` only if none succeeds.

### Docker

```sh
docker build -t markitforge:latest .
docker run --rm -i markitforge:latest < ~/your-file.pdf > output.md
```

## Contributing

Before starting significant work, please read [What to Contribute](#what-to-contribute), which describes what is in and out of scope for this repository.

Contributions are welcome. Open an issue first for big changes. By contributing you agree your work is released under the MIT license.

Fixes that are not MarkitForge-specific are best sent upstream to [microsoft/markitdown](https://github.com/microsoft/markitdown) too.

### What to Contribute

MarkitForge is a Python utility for converting files to Markdown for use with LLMs and related text analysis pipelines. The core packages are Python libraries for other systems to build on. The one end-user app, `markitforge-gui`, stays a thin layer over the library.

#### In scope

- Improvements to the fidelity of existing converters (New formats are added sparingly -- especially if they incur new dependencies. In most cases, new formats can be better supported via [3rd-party plugins](#extending-markitforge-without-changing-this-repository).)
- Bug fixes, performance improvements, and security fixes
- The `markitforge` command-line interface
- The `markitforge-mcp` package
- Tests, documentation, and developer tooling

#### Out of scope

We cannot accept additional applications, services, or servers. This includes:

- Web servers, REST or HTTP APIs, and hosted conversion services
- Web frontends and browser-based user interfaces
- Desktop and mobile applications (PyQt, PySide, Tkinter, Electron, Flutter, and similar)

Projects like these are genuinely useful, and we would rather see them thrive than be turned away. If you are interested in providing a web service, API, or graphical application for MarkitForge, please maintain it as a separate package or project that depends on [`markitforge` from PyPI](https://pypi.org/project/markitforge/).

### Extending MarkitForge Without Changing This Repository

MarkitForge supports 3rd-party plugins, so support for a new format can be published and installed independently of this repository:

```sh
markitforge --list-plugins
markitforge --use-plugins path-to-file.pdf
```

See `packages/markitforge-sample-plugin` to get started, and tag your repository `#markitforge-plugin` so that others can find it.

### How to Contribute

You can help by looking at issues or helping review PRs. We have also marked some issues as 'open for contribution' and PRs as 'open for reviewing' to help facilitate community contributions. These labels are suggestions; contributions within the scope described above are welcome.

<div align="center">

|            | All                                                          | Especially Needs Help from Community                                                                                                      |
| ---------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **Issues** | [All Issues](https://github.com/microsoft/markitdown/issues) | [Issues open for contribution](https://github.com/microsoft/markitdown/issues?q=is%3Aissue+is%3Aopen+label%3A%22open+for+contribution%22) |
| **PRs**    | [All PRs](https://github.com/microsoft/markitdown/pulls)     | [PRs open for reviewing](https://github.com/microsoft/markitdown/pulls?q=is%3Apr+is%3Aopen+label%3A%22open+for+reviewing%22)              |

</div>

### Running Tests and Checks

- Navigate to the MarkitForge package:

  ```sh
  cd packages/markitforge
  ```

- Install `hatch` in your environment and run tests:

  ```sh
  pip install hatch  # Other ways of installing hatch: https://hatch.pypa.io/dev/install/
  hatch shell
  hatch test
  ```

  (Alternative) Use the Devcontainer which has all the dependencies installed:

  ```sh
  # Reopen the project in Devcontainer and run:
  hatch test
  ```

- Run pre-commit checks before submitting a PR: `pre-commit run --all-files`

### Security Considerations

MarkitForge performs I/O with the privileges of the current process. Like `open()` or `requests.get()`, it will access resources that the process itself can access.

**Sanitize your inputs:** Do not pass untrusted input directly to MarkitForge. If any part of the input may be controlled by an untrusted user or system, such as in hosted or server-side applications, it must be validated and restricted before calling MarkitForge. Depending on your environment, this may include restricting file paths, limiting URI schemes and network destinations, and blocking access to private, loopback, link-local, or metadata-service addresses.

**Block internal URLs on servers:** `MarkitForge(block_private_networks=True)` (or `MARKITFORGE_BLOCK_PRIVATE_URLS=1`) refuses URLs, including redirect hops, that resolve to private, loopback, link-local or cloud-metadata addresses. It is a resolve-then-connect check, so a hostile DNS server can still race it; for high-risk services also restrict egress at the network level. Downloads are capped by `max_download_bytes` (512 MB), `fetch_timeout` and `fetch_deadline` (300 s total).

**Call only the conversion method you need:** Prefer the narrowest conversion API that fits your use case. MarkitForge's `convert()` method is intentionally permissive and can handle local files, remote URIs, and byte streams. If your application only needs to read local files, call `convert_local()` instead. If you need more control over URI fetching, call `requests.get()` yourself and pass the response object to `convert_response()`. For maximum control, open a stream to the input you want converted and call `convert_stream()`.

Additionally, image captioning, speech transcription, Azure Document Intelligence, and Azure Content Understanding rely on web-hosted services to function. For local-only operations, install only the optional dependencies that you need and want to use.

## License and attribution

MarkitForge is a fork of [MarkItDown](https://github.com/microsoft/markitdown) by Microsoft Corporation, taken at upstream commit `ed17895`. It uses two licenses:

| Code | License | File |
|---|---|---|
| Code from MarkItDown | **MIT**, Copyright (c) Microsoft Corporation. The original notice and text are unchanged. | [LICENSE](LICENSE) |
| New MarkitForge code, and MarkitForge's changes to MarkItDown files | **Apache License 2.0**, Copyright 2026 MarkitForge contributors | [LICENSE-APACHE](LICENSE-APACHE), [NOTICE](NOTICE) |

Each source file has an `SPDX-License-Identifier` header: `Apache-2.0` for new files, `MIT AND Apache-2.0` for MarkItDown files that MarkitForge changed, and `MIT` (or no header) for MarkItDown files that were only renamed. Every package ships the license files that apply to it. Code adapted from [dwml](https://github.com/xiilei/dwml) (Apache-2.0) is listed in [ThirdPartyNotices.md](packages/markitforge/ThirdPartyNotices.md).

If you redistribute MarkitForge, keep `LICENSE`, `LICENSE-APACHE` and `NOTICE` with it.

MarkitForge is an independent project. It is not affiliated with, sponsored by, or endorsed by Microsoft.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
