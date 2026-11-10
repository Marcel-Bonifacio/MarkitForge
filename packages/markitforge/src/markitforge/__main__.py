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
from typing import Any, Dict
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
            enable_plugins=args.use_plugins, **_instance_kwargs(args)
        )

    options = _conversion_options(args)
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
                options=options,
                # Cloud-service converters carry config we don't rebuild in
                # worker processes, so those runs use threads + this instance.
                instance_kwargs=(
                    None
                    if (args.use_docintel or args.use_cu)
                    else {"enable_plugins": args.use_plugins, **_instance_kwargs(args)}
                ),
                converter=markitforge,
            )
        )

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


def _instance_kwargs(args) -> Dict[str, Any]:
    """Constructor options shared by single and batch mode."""
    kw: Dict[str, Any] = {}
    if args.no_magika:
        kw["use_magika"] = False
    if args.fetch_timeout is not None:
        kw["fetch_timeout"] = (min(10.0, args.fetch_timeout), args.fetch_timeout)
    if args.max_download_mb is not None:
        kw["max_download_bytes"] = int(args.max_download_mb * 1024 * 1024)
    return kw


def _conversion_options(args) -> Dict[str, Any]:
    """Per-conversion options passed to converters as keyword arguments."""
    opts: Dict[str, Any] = {"keep_data_uris": args.keep_data_uris}
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
    if args.describe_images or args.transcribe_formulas:
        if not args.llm_model:
            _exit_with_error(
                "--describe-images and --transcribe-formulas need --llm-model "
                "(or MARKITFORGE_LLM_MODEL)."
            )
        try:
            from openai import OpenAI
        except ImportError:
            _exit_with_error(
                "These options need the openai package: pip install openai"
            )
        opts["llm_client"] = OpenAI()
        opts["llm_model"] = args.llm_model
        opts["describe_images"] = bool(args.describe_images)
        opts["transcribe_formulas"] = bool(args.transcribe_formulas)
    return opts


def _render(args, result: DocumentConverterResult) -> str:
    """The text to write for one result, honouring --pages and --json."""
    markdown = result.markdown
    if args.pages:
        try:
            markdown = result.select_pages(args.pages)
        except ValueError as exc:
            _exit_with_error(f"--pages: {exc}")
    if args.json:
        import json

        data = result.to_dict()
        data["markdown"] = markdown
        return json.dumps(data, ensure_ascii=False, indent=2)
    return markdown


def _handle_output(args, result: DocumentConverterResult):
    """Handle output to stdout or file"""
    text = _render(args, result)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        # Handle stdout encoding errors more gracefully, with fallback for
        # cases where sys.stdout.encoding is None (e.g. redirected streams).
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding))


def _exit_with_error(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
