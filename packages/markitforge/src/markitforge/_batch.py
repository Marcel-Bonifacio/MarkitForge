# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Batch conversion of many files, folders and URLs (used by the CLI)."""

import json
import os
import sys
import time
from concurrent.futures import (
    Executor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

_WORKER_CONVERTER: Any = None


@dataclass
class BatchItem:
    source: str  # local path or URL
    target: Path  # where the .md / .json goes


@dataclass
class BatchOutcome:
    item: BatchItem
    ok: bool
    message: str = ""
    warnings: int = 0
    seconds: float = 0.0


def _is_url(text: str) -> bool:
    return urlparse(text).scheme.lower() in ("http", "https")


def _url_stem(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).stem or parsed.netloc or "page"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:100]


def collect_items(
    inputs: List[str],
    *,
    output_dir: Optional[str],
    pattern: str = "*",
    recursive: bool = False,
    suffix: str = ".md",
) -> List[BatchItem]:
    """Expand inputs into a list of (source, target) pairs."""
    out_root = Path(output_dir) if output_dir else None
    planned: List[tuple] = []  # (source, dest_dir, stem, full_name)

    for raw in inputs:
        if _is_url(raw):
            planned.append((raw, out_root or Path.cwd(), _url_stem(raw), None))
            continue
        path = Path(raw)
        if path.is_dir():
            files = path.rglob(pattern) if recursive else path.glob(pattern)
            for f in sorted(files):
                rel = f.relative_to(path)
                if not f.is_file() or any(p.startswith(".") for p in rel.parts):
                    continue
                if f.suffix.lower() in (".md", ".markdown") and out_root is None:
                    continue  # don't convert our own output on a re-run
                dest_dir = (out_root / rel.parent) if out_root else f.parent
                planned.append((str(f), dest_dir, f.stem, f.name))
        elif path.is_file():
            planned.append((str(path), out_root or path.parent, path.stem, path.name))
        else:
            raise FileNotFoundError(f"No such file, folder or URL: {raw}")

    # Two sources with the same stem in one folder (a.pdf + a.docx) keep their
    # extension (a.pdf.md, a.docx.md); anything still clashing gets a number.
    counts: Dict[tuple, int] = {}
    for _, d, stem, _ in planned:
        counts[(d, stem)] = counts.get((d, stem), 0) + 1
    items: List[BatchItem] = []
    seen: set = set()
    for source, d, stem, name in planned:
        base = d / ((name if counts[(d, stem)] > 1 and name else stem) + suffix)
        target, n = base, 2
        while target in seen:
            target = base.with_name(f"{base.stem}-{n}{base.suffix}")
            n += 1
        seen.add(target)
        items.append(BatchItem(source, target))
    return items


def _render(result: Any, pages: Optional[str], as_json: bool) -> str:
    markdown = result.markdown
    if pages and result.pages:
        markdown = result.select_pages(pages)
    if as_json:
        data = result.to_dict()
        data["markdown"] = markdown
        return json.dumps(data, ensure_ascii=False, indent=2)
    return markdown


def _convert_one(
    converter: Any,
    item: BatchItem,
    options: Dict[str, Any],
    pages: Optional[str],
    as_json: bool,
) -> BatchOutcome:
    start = time.monotonic()
    try:
        result = converter.convert(item.source, **options)
        text = _render(result, pages, as_json)
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.target.write_text(text, encoding="utf-8")
        note = ""
        if pages and not result.pages:
            note = "no page info, wrote whole document"
        return BatchOutcome(
            item,
            True,
            note,
            warnings=len(result.warnings),
            seconds=time.monotonic() - start,
        )
    except Exception as exc:  # report and keep going
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        return BatchOutcome(
            item,
            False,
            f"{type(exc).__name__}: {msg}"[:300],
            seconds=time.monotonic() - start,
        )


def _process_worker_init(instance_kwargs: Dict[str, Any]) -> None:
    global _WORKER_CONVERTER
    from ._markitforge import MarkitForge

    _WORKER_CONVERTER = MarkitForge(**instance_kwargs)


def _process_worker(
    item: BatchItem, options: Dict[str, Any], pages: Optional[str], as_json: bool
) -> BatchOutcome:
    return _convert_one(_WORKER_CONVERTER, item, options, pages, as_json)


def run_batch(
    inputs: List[str],
    *,
    output_dir: Optional[str],
    pattern: str = "*",
    recursive: bool = False,
    workers: int = 1,
    overwrite: bool = False,
    as_json: bool = False,
    pages: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    instance_kwargs: Optional[Dict[str, Any]] = None,
    converter: Any = None,
) -> int:
    """Convert many inputs. Returns a process exit code (0 = all succeeded)."""
    options = dict(options or {})
    if output_dir and os.path.exists(output_dir) and not os.path.isdir(output_dir):
        print(f"-o {output_dir} must be a folder in batch mode.", file=sys.stderr)
        return 2
    try:
        items = collect_items(
            inputs,
            output_dir=output_dir,
            pattern=pattern,
            recursive=recursive,
            suffix=".json" if as_json else ".md",
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    skipped = [i for i in items if i.target.exists() and not overwrite]
    items = [i for i in items if overwrite or not i.target.exists()]
    for i in skipped:
        print(f"skip  {i.source} ({i.target} exists; use --overwrite)", file=sys.stderr)
    if not items:
        print("Nothing to convert.", file=sys.stderr)
        return 0

    # Processes give real parallelism for CPU-bound parsing. Threads are used when
    # options hold objects that can't be pickled (LLM clients) or when the caller
    # configured a converter we can't rebuild (cloud services).
    use_processes = (
        workers > 1
        and instance_kwargs is not None
        and "llm_client" not in options
        and len(items) > 1
    )
    executor: Executor
    if use_processes:
        executor = ProcessPoolExecutor(
            max_workers=workers,
            initializer=_process_worker_init,
            initargs=(instance_kwargs,),
        )
    else:
        if converter is None:
            from ._markitforge import MarkitForge

            converter = MarkitForge(**(instance_kwargs or {}))
        executor = ThreadPoolExecutor(max_workers=max(1, workers))

    failed = 0
    started = time.monotonic()
    with executor:
        if use_processes:
            futures = [
                executor.submit(_process_worker, i, options, pages, as_json)
                for i in items
            ]
        else:
            futures = [
                executor.submit(_convert_one, converter, i, options, pages, as_json)
                for i in items
            ]
        for done, fut in enumerate(as_completed(futures), 1):
            o = fut.result()
            tag = f"[{done}/{len(items)}]"
            if o.ok:
                extra = f", {o.warnings} warning(s)" if o.warnings else ""
                extra += f", {o.message}" if o.message else ""
                print(
                    f"{tag} ok    {o.item.source} -> {o.item.target} ({o.seconds:.1f}s{extra})",
                    file=sys.stderr,
                )
            else:
                failed += 1
                print(f"{tag} FAIL  {o.item.source}: {o.message}", file=sys.stderr)

    total = len(items)
    print(
        f"Converted {total - failed} of {total} file(s) in "
        f"{time.monotonic() - started:.1f}s"
        + (f"; {failed} failed." if failed else "."),
        file=sys.stderr,
    )
    return 1 if failed else 0
