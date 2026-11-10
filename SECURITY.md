# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
**Security → Report a vulnerability** form on this repository, not in a public
issue. Include the input file (or how to build it), the command or API call,
and what happened.

Bugs that also affect upstream [microsoft/markitdown](https://github.com/microsoft/markitdown)
should be reported to Microsoft as well, following their `SECURITY.md`.

## Threat model

MarkitForge converts **untrusted documents and URLs**. It runs with the
privileges of the calling process, so treat it like any parser of hostile
input: run it with least privilege, and in a container or VM for
internet-facing services.

## Built-in limits

All limits are keyword arguments to `convert()` (or `MarkitForge(...)` for the
fetch settings). Hitting one raises `ResourceLimitException` (ZIP containers
skip the offending member and add a warning instead).

| Risk | Limit (default) |
|---|---|
| Zip bombs in ZIP, DOCX, XLSX, PPTX, EPUB | `max_archive_member_bytes` (200 MB), `max_archive_bytes` (500 MB per document, shared across nesting), `max_compression_ratio` (200x for members over 1 MB), `max_archive_entries` (2000), `max_archive_depth` (3) |
| Embedded-image blow-up (MHTML, DOCX with `keep_data_uris`) | `max_inline_bytes` (50 MB) |
| Huge PDFs | `max_pdf_pages` (5000), `max_render_pixels` (4 MP per rendered region) |
| LLM cost amplification | `max_llm_calls` (50 per document, identical images reuse one caption) |
| Prompt-injected LLM output | captions are stripped to plain alt text; formula LaTeX is validated and rejected if it could break out of `$$…$$` |
| Fake page markers in document text | marker-like lines in page text are escaped |
| Downloads | `max_download_bytes` (512 MB), `fetch_timeout` ((10, 60) s), `fetch_deadline` (300 s total), `max_redirects` (10) |
| SSRF | `block_private_networks=True` / `MARKITFORGE_BLOCK_PRIVATE_URLS=1` refuses private, loopback, link-local and metadata addresses, re-checked on each redirect (on by default for the MCP server over HTTP) |
| Page-range abuse | ranges are clamped to the document; max 200 parts / 1000 characters |
| LibreOffice (`.doc`, `.rtf`, ODF, …) | explicit import filter per extension (no content sniffing), fresh profile per run, process-group kill on timeout (`libreoffice_timeout`, 120 s), no network (macOS `sandbox-exec`, Linux `unshare -rn` when available; `MARKITFORGE_LIBREOFFICE_SANDBOX=0` turns it off) |

## Server components

- **MCP server** (`markitforge-mcp`): see its README for the `file:` and
  private-URL policy. Over HTTP, `file:` URIs are off unless you allow folders.
- **GUI** (`markitforge-gui`): binds to loopback, accepts only loopback (or this
  machine's own) Host names, requires a custom header on every write, sends a
  strict CSP, and sanitizes rendered HTML with an allow-list (no remote images,
  no scripts).

## Supply chain

The `markitforge*` names are **not published on PyPI**. Install from this
repository and install sub-packages together with the core from the same
checkout, so pip never resolves `markitforge` from PyPI. CI installs from local
paths only.
