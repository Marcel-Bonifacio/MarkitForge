# SPDX-FileCopyrightText: 2024-present Adam Fourney <adamfo@microsoft.com>
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.
import argparse
import os
import sys
import codecs
import io
from pathlib import Path
from typing import Any, Dict, NoReturn, Optional
from textwrap import dedent
from importlib.metadata import entry_points
from .__about__ import __version__
from ._markitforge import MarkitForge, StreamInfo, DocumentConverterResult


def main():
    parser = argparse.ArgumentParser(
        description="Convert various file formats to markdown.",
        prog="markitforge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage=dedent("""
            SYNTAX:

                markitforge <OPTIONAL: FILENAME>
                If FILENAME is empty, markitforge reads from stdin.

            EXAMPLE:

                markitforge example.pdf

                OR

                cat example.pdf | markitforge

                OR

                markitforge < example.pdf

                OR to save to a file use

                markitforge example.pdf -o example.md

                OR

                markitforge example.pdf > example.md
            """).strip(),
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show the version number and exit",
    )

    parser.add_argument(
        "-o",
        "--output",
        help="Output file name. If not provided, output is written to stdout.",
    )

    parser.add_argument(
        "-x",
        "--extension",
        help="Provide a hint about the file extension (e.g., when reading from stdin).",
    )

    parser.add_argument(
        "-m",
        "--mime-type",
        help="Provide a hint about the file's MIME type.",
    )

    parser.add_argument(
        "-c",
        "--charset",
        help="Provide a hint about the file's charset (e.g., UTF-8).",
    )

    cloud_group = parser.add_mutually_exclusive_group()
    cloud_group.add_argument(
        "-d",
        "--use-docintel",
        action="store_true",
        help="Use Document Intelligence to extract text instead of offline conversion. Requires a valid Document Intelligence Endpoint.",
    )

    cloud_group.add_argument(
        "--use-cu",
        "--use-content-understanding",
        action="store_true",
        dest="use_cu",
        help="Use Azure Content Understanding to extract text. Requires --cu-endpoint.",
    )

    parser.add_argument(
        "-e",
        "--endpoint",
        type=str,
        default=os.environ.get("MARKITFORGE_DOCINTEL_ENDPOINT") or None,
        help="Document Intelligence Endpoint. Required if using Document Intelligence. Defaults to the MARKITFORGE_DOCINTEL_ENDPOINT environment variable.",
    )

    parser.add_argument(
        "--cu-endpoint",
        type=str,
        default=os.environ.get("MARKITFORGE_CU_ENDPOINT") or None,
        help="Content Understanding Endpoint. Required if using --use-cu. Defaults to the MARKITFORGE_CU_ENDPOINT environment variable.",
    )

    parser.add_argument(
        "--cu-analyzer",
        type=str,
        help="Content Understanding analyzer ID. If not specified, auto-selects by file type.",
    )

    parser.add_argument(
        "--cu-file-types",
        type=str,
        help="Comma-separated list of file types to route to Content Understanding (e.g., pdf,jpeg,mp4). If omitted, all supported types are routed.",
    )

    parser.add_argument(
        "--docintel-model-id",
        type=str,
        default=None,
        help="Document Intelligence model ID (e.g., 'prebuilt-layout', 'prebuilt-invoice', or a custom model ID). Defaults to 'prebuilt-layout'.",
    )

    parser.add_argument(
        "--docintel-query-fields",
        type=str,
        default=None,
        help="Comma-separated list of field names to extract via the billable Document Intelligence queryFields add-on (OCR file types only).",
    )

    parser.add_argument(
        "--docintel-features",
        type=str,
        default=None,
        help=(
            "Comma-separated Document Intelligence analysis add-ons for OCR file types. "
            "Available values: barcodes, formulas, keyValuePairs, languages, "
            "ocrHighResolution, and styleFont. The formulas, ocrHighResolution, and "
            "styleFont incur additional charges. No add-ons are enabled by default; "
            "use 'none' to explicitly request none."
        ),
    )

    parser.add_argument(
        "-p",
        "--use-plugins",
        action="store_true",
        help="Use 3rd-party plugins to convert files. Use --list-plugins to see installed plugins.",
    )

    parser.add_argument(
        "--list-plugins",
        action="store_true",
        help="List installed 3rd-party plugins. Plugins are loaded when using the -p or --use-plugins option.",
    )

    parser.add_argument(
        "--keep-data-uris",
        action="store_true",
        help="Keep data URIs (like base64-encoded images) in the output. By default, data URIs are truncated.",
    )

    forge = parser.add_argument_group("MarkitForge options")
    forge.add_argument(
        "--page-markers",
        action="store_true",
        help="Put <!-- page N --> (or slide/sheet) markers between pages.",
    )
    forge.add_argument(
        "--pages",
        metavar="RANGE",
        help='Only output these pages, e.g. "1-3,5,9-". Needs a format with page info (PDF, PPTX, XLSX, DOCX).',
    )
    forge.add_argument(
        "--table-format",
        choices=["markdown", "html", "auto"],
        default=None,
        help="markdown (default), html, or auto (HTML only for tables with merged cells).",
    )
    forge.add_argument(
        "--include-hidden-sheets",
        action="store_true",
        help="Also convert hidden Excel sheets (skipped by default).",
    )
    forge.add_argument(
        "--include-headers-footers",
        action="store_true",
        help="Add Word headers and footers to the output.",
    )
    forge.add_argument(
        "--pdf-backend",
        choices=["auto", "pdfplumber", "pdfminer", "docling"],
        default=None,
        help="PDF engine. auto (default) uses pdfplumber with tables and headings.",
    )
    forge.add_argument(
        "--describe-images",
        action="store_true",
        help="Describe images inline with an LLM (needs --llm-model and OPENAI_API_KEY).",
    )
    forge.add_argument(
        "--transcribe-formulas",
        action="store_true",
        help="Turn PDF math into LaTeX with an LLM (needs --llm-model and OPENAI_API_KEY).",
    )
    forge.add_argument(
        "--llm-model",
        default=os.environ.get("MARKITFORGE_LLM_MODEL"),
        help="Model for image/formula features. Uses the OpenAI client; OPENAI_BASE_URL works for compatible servers.",
    )
    forge.add_argument(
        "--json",
        action="store_true",
        help="Output JSON with markdown, metadata, pages, assets and warnings.",
    )
    forge.add_argument(
        "--list-formats",
        action="store_true",
        help="List supported formats and whether their dependencies are installed.",
    )
    forge.add_argument(
        "--no-magika",
        action="store_true",
        help="Skip Magika content sniffing (use extension and MIME type only).",
    )
    forge.add_argument(
        "--fetch-timeout",
        type=float,
        default=None,
        help="Seconds to wait for a URL to respond (default 60).",
    )
    forge.add_argument(
        "--max-download-mb",
        type=float,
        default=None,
        help="Largest URL download allowed, in MB (default 512).",
    )

    forge.add_argument(
        "--ocr",
        action="store_true",
        help="Read scanned PDF pages with OCR (an LLM via --llm-model, or tesseract).",
    )
    forge.add_argument(
        "--ocr-engine",
        choices=["auto", "llm", "tesseract"],
        default=None,
        help="OCR engine for --ocr (default auto: LLM if configured, else tesseract).",
    )
    forge.add_argument(
        "--llm-base-url",
        default=os.environ.get("MARKITFORGE_LLM_BASE_URL"),
        help="OpenAI-compatible API URL, e.g. http://localhost:11434/v1 for Ollama.",
    )
    forge.add_argument(
        "--llm-api-key-env",
        default=None,
        metavar="NAME",
        help="Environment variable holding the LLM API key (default OPENAI_API_KEY).",
    )
    forge.add_argument(
        "--config",
        metavar="FILE",
        help="Config file (default ~/.config/markitforge/config.toml or MARKITFORGE_CONFIG).",
    )
    forge.add_argument(
        "--no-config", action="store_true", help="Ignore the config file."
    )
    forge.add_argument(
        "--progress",
        action="store_true",
        help="Show progress on stderr.",
    )

    out = parser.add_argument_group("output for RAG pipelines")
    out.add_argument(
        "--front-matter",
        action="store_true",
        help="Start the Markdown with YAML front matter (title, metadata, page count).",
    )
    out.add_argument(
        "--chunks",
        choices=["heading", "tokens"],
        default=None,
        help="Write JSON Lines chunks instead of Markdown: split at headings, or by size.",
    )
    out.add_argument(
        "--chunk-size",
        type=int,
        default=800,
        metavar="TOKENS",
        help="Largest chunk, in (estimated) tokens (default 800).",
    )
    out.add_argument(
        "--chunk-overlap",
        type=int,
        default=0,
        metavar="TOKENS",
        help="Tokens repeated from the previous chunk (default 0).",
    )
    out.add_argument(
        "--assets-dir",
        metavar="DIR",
        help="Save embedded images as files in DIR and link to them. In batch "
        "mode each document gets <name>_assets/ next to its output.",
    )

    limits = parser.add_argument_group("safety limits (see SECURITY.md)")
    limits.add_argument(
        "--max-archive-mb",
        type=float,
        default=None,
        help="Most uncompressed data per document in ZIP-based files (default 500).",
    )
    limits.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Refuse PDFs with more pages (default 5000; 0 = no limit).",
    )
    limits.add_argument(
        "--max-llm-calls",
        type=int,
        default=None,
        help="Most LLM calls per document (default 50).",
    )
    limits.add_argument(
        "--block-private-urls",
        action="store_true",
        help="Refuse URLs (and redirects) to private, loopback or link-local addresses.",
    )

    batch = parser.add_argument_group(
        "batch mode",
        "Used when several inputs or a folder are given. -o is then an output folder.",
    )
    batch.add_argument(
        "--glob",
        default="*",
        help='File pattern inside folders, e.g. "*.pdf" (default: all files).',
    )
    batch.add_argument(
        "-r", "--recursive", action="store_true", help="Look in sub-folders too."
    )
    batch.add_argument(
        "-j",
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Files to convert at the same time (default: up to 4).",
    )
    batch.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing .md files in the output folder.",
    )

    parser.add_argument("filename", nargs="*", help="Files, folders or URLs.")
    args = parser.parse_args()

    config = _load_config(args)

    if args.list_formats:
        from ._formats import format_table

        print(format_table(as_json=args.json))
        sys.exit(0)

    # Parse the extension hint
    extension_hint = args.extension
    if extension_hint is not None:
        extension_hint = extension_hint.strip().lower()
        if len(extension_hint) > 0:
            if not extension_hint.startswith("."):
                extension_hint = "." + extension_hint
        else:
            extension_hint = None

    # Parse the mime type
    mime_type_hint = args.mime_type
    if mime_type_hint is not None:
        mime_type_hint = mime_type_hint.strip()
        if len(mime_type_hint) > 0:
            if mime_type_hint.count("/") != 1:
                _exit_with_error(f"Invalid MIME type: {mime_type_hint}")
        else:
            mime_type_hint = None

    # Parse the charset
    charset_hint = args.charset
    if charset_hint is not None:
        charset_hint = charset_hint.strip()
        if len(charset_hint) > 0:
            try:
                charset_hint = codecs.lookup(charset_hint).name
            except LookupError:
                _exit_with_error(f"Invalid charset: {charset_hint}")
        else:
            charset_hint = None

    stream_info = None
    if (
        extension_hint is not None
        or mime_type_hint is not None
        or charset_hint is not None
    ):
        stream_info = StreamInfo(
            extension=extension_hint, mimetype=mime_type_hint, charset=charset_hint
        )

    if args.list_plugins:
        # List installed plugins, then exit
        print("Installed MarkitForge 3rd-party Plugins:\n")
        plugin_entry_points = list(entry_points(group="markitforge.plugin")) + list(
            entry_points(group="markitdown.plugin")
        )
        if len(plugin_entry_points) == 0:
            print("  * No 3rd-party plugins installed.")
            print(
                "\nFind plugins by searching for the hashtag #markitforge-plugin on GitHub.\n"
            )
        else:
            for entry_point in plugin_entry_points:
                print(f"  * {entry_point.name:<16}\t(package: {entry_point.value})")
            print(
                "\nUse the -p (or --use-plugins) option to enable 3rd-party plugins.\n"
            )
        sys.exit(0)

    if args.use_docintel:
        if args.endpoint is None:
            _exit_with_error(
                "Document Intelligence Endpoint is required when using Document Intelligence. "
                "Pass -e/--endpoint or set MARKITFORGE_DOCINTEL_ENDPOINT."
            )
        elif args.filename is None:
            _exit_with_error("Filename is required when using Document Intelligence.")

        docintel_kwargs: Dict[str, Any] = {
            "docintel_endpoint": args.endpoint,
        }
        if args.docintel_model_id:
            docintel_kwargs["docintel_model_id"] = args.docintel_model_id
        if args.docintel_features is not None:
            if args.docintel_features.strip().lower() == "none":
                docintel_kwargs["docintel_features"] = []
            else:
                docintel_kwargs["docintel_features"] = [
                    feature.strip()
                    for feature in args.docintel_features.split(",")
                    if feature.strip()
                ]
        if args.docintel_query_fields:
            fields = [
                f.strip() for f in args.docintel_query_fields.split(",") if f.strip()
            ]
            if fields:
                docintel_kwargs["docintel_query_fields"] = fields

        markitforge = MarkitForge(enable_plugins=args.use_plugins, **docintel_kwargs)
    elif args.use_cu:
        if args.cu_endpoint is None:
            _exit_with_error(
                "Content Understanding Endpoint (--cu-endpoint) is required when using --use-cu. "
                "Pass --cu-endpoint or set MARKITFORGE_CU_ENDPOINT."
            )

        cu_kwargs: Dict[str, Any] = {
            "cu_endpoint": args.cu_endpoint,
        }
        if args.cu_analyzer is not None:
            cu_kwargs["cu_analyzer_id"] = args.cu_analyzer
        if args.cu_file_types is not None:
            # Parse comma-separated file types into ContentUnderstandingFileType list
            from .converters import ContentUnderstandingFileType

            type_names = [
                t.strip().lower() for t in args.cu_file_types.split(",") if t.strip()
            ]
            cu_types = []
            for name in type_names:
                # Try matching by value (e.g., "pdf", "jpeg", "mp4")
                try:
                    cu_types.append(ContentUnderstandingFileType(name))
                except ValueError:
                    _exit_with_error(f"Unknown file type: {name}")
            cu_kwargs["cu_file_types"] = cu_types

        markitforge = MarkitForge(enable_plugins=args.use_plugins, **cu_kwargs)
    else:
        markitforge = MarkitForge(
            enable_plugins=args.use_plugins, **_instance_kwargs(args, config)
        )

    options = _conversion_options(args, config)
    if args.progress:
        options["progress"] = _progress_printer()
    inputs = list(args.filename or [])

    if len(inputs) > 1 or (len(inputs) == 1 and os.path.isdir(inputs[0])):
        from ._batch import run_batch

        sys.exit(
            run_batch(
                inputs,
                output_dir=args.output,
                pattern=args.glob,
                recursive=args.recursive,
                workers=args.workers,
                overwrite=args.overwrite,
                as_json=args.json,
                pages=args.pages,
                front_matter=args.front_matter,
                chunks=_chunk_opts(args),
                save_assets=bool(args.assets_dir),
                options={k: v for k, v in options.items() if k != "progress"},
                # Cloud-service converters carry config we don't rebuild in
                # worker processes, so those runs use threads + this instance.
                instance_kwargs=(
                    None
                    if (args.use_docintel or args.use_cu)
                    else {
                        "enable_plugins": args.use_plugins,
                        **_instance_kwargs(args, config),
                    }
                ),
                converter=markitforge,
            )
        )

    if args.assets_dir:
        options["extract_images"] = True

    if not inputs:
        # Windows pipe-backed stdin can report seekable() even though it cannot rewind.
        result = markitforge.convert_stream(
            io.BytesIO(sys.stdin.buffer.read()),
            stream_info=stream_info,
            **options,
        )
    else:
        result = markitforge.convert(inputs[0], stream_info=stream_info, **options)

    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    _handle_output(args, result)


def _load_config(args):
    from ._config import Config, load_config

    if args.no_config:
        return Config()
    try:
        return load_config(args.config)
    except ValueError as exc:
        _exit_with_error(str(exc))


def _instance_kwargs(args, config=None) -> Dict[str, Any]:
    """Constructor options shared by single and batch mode."""
    kw: Dict[str, Any] = dict(config.instance) if config is not None else {}
    if args.no_magika:
        kw["use_magika"] = False
    if args.fetch_timeout is not None:
        kw["fetch_timeout"] = (min(10.0, args.fetch_timeout), args.fetch_timeout)
    if args.max_download_mb is not None:
        kw["max_download_bytes"] = int(args.max_download_mb * 1024 * 1024)
    if args.block_private_urls:
        kw["block_private_networks"] = True
    return kw


def _llm_client(args, config):
    """An OpenAI-compatible client from flags, else from the config file."""
    base_url = args.llm_base_url or (config.llm.get("base_url") if config else None)
    key_env = args.llm_api_key_env or (
        config.llm.get("api_key_env") if config else None
    )
    try:
        from openai import OpenAI
    except ImportError:
        _exit_with_error("LLM features need the openai package: pip install openai")
    kwargs: Dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    api_key = os.environ.get(key_env or "OPENAI_API_KEY")
    if api_key:
        kwargs["api_key"] = api_key
    elif base_url:
        kwargs["api_key"] = "not-needed"  # local servers such as Ollama
    return OpenAI(**kwargs)


def _conversion_options(args, config=None) -> Dict[str, Any]:
    """Per-conversion options passed to converters as keyword arguments.

    Config file defaults and limits come first; command-line flags win.
    """
    opts: Dict[str, Any] = dict(config.conversion_kwargs()) if config else {}
    if args.keep_data_uris or "keep_data_uris" not in opts:
        opts["keep_data_uris"] = args.keep_data_uris
    if args.page_markers:
        opts["page_markers"] = True
    if args.table_format:
        opts["table_format"] = args.table_format
    if args.include_hidden_sheets:
        opts["include_hidden_sheets"] = True
    if args.include_headers_footers:
        opts["include_headers_footers"] = True
    if args.pdf_backend:
        opts["pdf_backend"] = args.pdf_backend
    if args.max_archive_mb is not None:
        opts["max_archive_bytes"] = int(args.max_archive_mb * 1024 * 1024)
    if args.max_pages is not None:
        opts["max_pdf_pages"] = args.max_pages
    if args.max_llm_calls is not None:
        opts["max_llm_calls"] = args.max_llm_calls
    if args.ocr:
        opts["ocr_scanned_pages"] = True
    if args.ocr_engine:
        opts["ocr_engine"] = args.ocr_engine

    model = args.llm_model or (config.llm.get("model") if config else None)
    wants_llm = (
        args.describe_images
        or args.transcribe_formulas
        or opts.get("describe_images")
        or opts.get("transcribe_formulas")
        or (
            opts.get("ocr_scanned_pages")
            and opts.get("ocr_engine", "auto") != "tesseract"
        )
    )
    if args.describe_images or args.transcribe_formulas:
        if not model:
            _exit_with_error(
                "--describe-images and --transcribe-formulas need --llm-model "
                "(or MARKITFORGE_LLM_MODEL, or [llm] model in the config file)."
            )
    if wants_llm and model:
        opts["llm_client"] = _llm_client(args, config)
        opts["llm_model"] = model
        if config and config.llm.get("prompt"):
            opts.setdefault("llm_prompt", config.llm["prompt"])
    if args.describe_images:
        opts["describe_images"] = True
    if args.transcribe_formulas:
        opts["transcribe_formulas"] = True
    return opts


def _chunk_opts(args) -> Optional[Dict[str, Any]]:
    if not args.chunks:
        return None
    if args.chunk_size < 16:
        _exit_with_error("--chunk-size must be at least 16.")
    return {
        "by": args.chunks,
        "max_tokens": args.chunk_size,
        "overlap": max(0, args.chunk_overlap),
    }


def _progress_printer():
    """A progress callback that redraws one line on stderr."""
    last = {"text": ""}

    tty = sys.stderr.isatty()

    def show(event) -> None:
        total = f"/{event.total}" if event.total else ""
        if not tty:  # logs: one line per event, no carriage returns
            msg = f"  {event.message[:60]}" if event.message else ""
            print(f"{event.stage} {event.current}{total}{msg}", file=sys.stderr)
            return
        text = f"\r{event.stage} {event.current}{total}"
        if event.message:
            text += f"  {event.message[:60]}"
        pad = max(0, len(last["text"]) - len(text))
        print(text + " " * pad, end="", file=sys.stderr, flush=True)
        last["text"] = text

    return show


def _render(args, result: DocumentConverterResult) -> str:
    """The text to write for one result, honouring the output flags."""
    from ._batch import render_output

    assets_dir = None
    prefix = None
    if args.assets_dir:
        assets_dir = Path(args.assets_dir)
        base = Path(args.output).resolve().parent if args.output else Path.cwd()
        try:
            prefix = os.path.relpath(assets_dir.resolve(), base).replace(os.sep, "/")
        except ValueError:  # different drive on Windows
            prefix = assets_dir.resolve().as_posix()
    opts = {
        "pages": args.pages,
        "as_json": args.json,
        "front_matter": args.front_matter,
        "chunks": _chunk_opts(args),
    }
    try:
        return render_output(
            result,
            opts,
            source=(args.filename or [None])[0],
            assets_dir=assets_dir,
            link_prefix=prefix,
        )
    except ValueError as exc:
        _exit_with_error(f"--pages: {exc}" if args.pages else str(exc))


def _handle_output(args, result: DocumentConverterResult):
    """Handle output to stdout or file"""
    if args.progress:
        print(file=sys.stderr)
    text = _render(args, result)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        # Handle stdout encoding errors more gracefully, with fallback for
        # cases where sys.stdout.encoding is None (e.g. redirected streams).
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding))


def _exit_with_error(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
