# Changelog

All notable changes to MarkitForge. Versions follow [Semantic Versioning](https://semver.org/);
the format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.0] - Unreleased

First MarkitForge release, forked from MarkItDown at upstream commit `ed17895`.

### Added
- **Desktop app** (`markitforge-gui`): drag-and-drop conversion with a rendered preview, raw
  Markdown, details, page selection, folder conversion, editing before download, progress and
  cancel, history across restarts, original-file view, AI options (local Ollama or any
  OpenAI-compatible service), and macOS/Windows installers.
- **Structured results**: `metadata`, `pages`, `assets`, `warnings`; `--json`, `--pages`,
  `--page-markers`.
- **RAG output**: `--chunks heading|tokens` (JSON Lines with heading path and page range),
  `--front-matter`, `--assets-dir` (images saved as files); `result.chunks()`,
  `result.front_matter()`, `result.save_assets()`.
- **Config file** (`~/.config/markitforge/config.toml`) for defaults, limits and the LLM provider.
- **Progress and cancel** hooks for the Python API (`progress=`, `cancel_event=`); `--progress`.
- **PDF**: tables on every page, headings (outline, tags, font heuristics), XY-cut reading order,
  header/footer removal, reflow, borderless and merged-cell tables, inline math, scanned-page OCR
  (LLM or tesseract), image export, Docling backend, LLM image descriptions and formula LaTeX.
- **Office**: Excel blank cells, whole numbers, hidden sheets, multi-table sheets; Word and
  PowerPoint metadata, page/slide sections, headers/footers; HTML tables for merged cells with
  inline formatting; image export.
- **Formats**: `.doc`, `.rtf`, OpenDocument, `.ppt`, `.wpd`, Apple iWork (via LibreOffice),
  `.mht/.mhtml`, `.eml`, `.tsv`.
- **CLI**: batch conversion of folders in parallel, `--list-formats`, `--ocr`, safety-limit flags.
- **MCP**: `convert_to_file`, `get_document_info`, `list_supported_formats`; `pages` and
  `max_chars`; file-root and private-URL policy.
- **Tooling**: quality benchmark with a CI regression check, upstream sync script and weekly
  check, release workflow (PyPI trusted publishing), coverage and mypy in CI.

### Changed
- Renamed `markitdown*` to `markitforge*` (`MarkItDown` remains an import alias; plugins on the
  `markitdown.plugin` entry point still load).
- Magika is optional (`markitforge[magika]`, included in `[all]`).
- Errors go to stderr. Safety-limit hits raise `ResourceLimitException` directly.
- Ruff replaces Black.
- License: MarkItDown code stays MIT; MarkitForge code is Apache-2.0 (see `NOTICE`).

### Security
- Limits for zip bombs, nesting, rendering, inlined data, LLM calls, pages and downloads.
- SSRF guard with per-redirect checks; total download deadline.
- LibreOffice runs with a fixed import filter per extension, sandboxed, without network.
- LLM output is validated before it reaches Markdown.
- GUI: loopback/own-host only, session token, strict CSP, allow-list sanitizer.
- MCP: `file:` off over HTTP unless allowed; safe output files.

[0.2.0]: https://github.com/Marcel-Bonifacio/MarkitForge/compare/ed17895...v0.2.0
