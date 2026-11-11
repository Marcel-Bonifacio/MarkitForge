# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Output helpers for retrieval pipelines: chunks, front matter, asset files."""

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from .converter_utils._archive_limits import (
    asset_file_name,
    checked_image_extension,
    safe_asset_name,
)
from .converter_utils._yaml import escape_yaml_controls

if TYPE_CHECKING:  # pragma: no cover
    from ._base_converter import DocumentConverterResult

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_MARKER_RE = re.compile(r"^<!--\s*(page|slide|sheet|chapter)\s+(\d{1,7})\s*-->\s*$")
_FENCE_RE = re.compile(r"^(```|~~~)")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------- tokens ---


def estimate_tokens(text: str) -> int:
    """Rough token count. Uses tiktoken (cl100k_base) when installed."""
    try:
        import tiktoken  # type: ignore[import-not-found]

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        # ~4 characters per token for English prose; good enough for sizing.
        return max(1, math.ceil(len(text) / 4)) if text else 0


# ----------------------------------------------------------- front matter ---


def _yaml_scalar(value: Any) -> str:
    # JSON scalars are valid YAML; quoting every string avoids YAML gotchas
    # ("no" -> false, "1e3" -> number, ": " in titles, ...).
    return escape_yaml_controls(json.dumps(value, ensure_ascii=False, default=str))


def _yaml_lines(key: str, value: Any, indent: int = 0) -> List[str]:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{key}: {{}}"]
        out = [f"{pad}{key}:"]
        for k, v in value.items():
            out += _yaml_lines(str(k), v, indent + 1)
        return out
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{pad}{key}: []"]
        return [f"{pad}{key}:"] + [f"{pad}  - {_yaml_scalar(v)}" for v in value]
    return [f"{pad}{key}: {_yaml_scalar(value)}"]


def front_matter(
    result: "DocumentConverterResult", extra: Optional[Dict[str, Any]] = None
) -> str:
    """A YAML front matter block (``---`` ... ``---``) for the result."""
    data = result.to_dict(include_markdown=False)
    fields: Dict[str, Any] = {}
    if data.get("title"):
        fields["title"] = data["title"]
    fields.update(data.get("metadata") or {})
    if data.get("pages"):
        fields.setdefault("page_count", len(data["pages"]))
    if data.get("warnings"):
        fields["warnings"] = data["warnings"]
    if extra:
        fields.update(extra)
    lines = ["---"]
    for k, v in fields.items():
        lines += _yaml_lines(_SAFE_NAME_RE.sub("_", str(k)) or "field", v)
    lines.append("---")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- assets ---


def rewrite_asset_links(markdown: str, mapping: Dict[str, str]) -> str:
    """Point ``![alt](name)`` image links at new paths (``mapping[name]``)."""
    if not mapping:
        return markdown

    def repl(m: "re.Match[str]") -> str:
        target = m.group(2)
        new = mapping.get(target)
        return f"{m.group(1)}({new}{m.group(3) or ''})" if new else m.group(0)

    return re.sub(r"(!\[[^\]\n]*\])\(([^)\s]+)(\s+\"[^\"\n]*\")?\)", repl, markdown)


def save_assets(
    result: "DocumentConverterResult",
    directory: os.PathLike,
    *,
    link_prefix: Optional[str] = None,
) -> Dict[str, str]:
    """Write asset bytes into ``directory``; return {asset name: link target}.

    Never follows symlinks and never writes outside ``directory``. Existing
    regular files with the same name are replaced atomically (re-runs).
    File extensions follow the asset's media type, not its name; an asset
    whose bytes are not that kind of image, or an SVG, is left out with a
    warning on the result.
    The returned targets are ``link_prefix/<file>`` (or just ``<file>``), ready
    for :func:`rewrite_asset_links`.
    """
    out_dir = Path(directory)
    written: Dict[str, str] = {}
    used: set = set()
    assets = [a for a in result.assets if a.data is not None]
    if not assets:
        return written
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_dir.is_symlink():
        raise ValueError(f"Refusing to write assets into a symlink: {out_dir}")
    for asset in assets:
        ext, reason = checked_image_extension(asset.mime_type, asset.data)  # type: ignore[arg-type]
        if ext is None:
            label = safe_asset_name(asset.name, "asset")
            result.warnings.append(f"Did not save asset `{label}`: {reason}.")
            continue
        name = asset_file_name(asset.name, ext, "asset")
        stem = name[: -len(ext)]
        n = 2
        while name in used:
            name = f"{stem}-{n}{ext}"
            n += 1
        used.add(name)
        target = out_dir / name
        if target.is_symlink():
            raise ValueError(f"Refusing to overwrite a symlink: {target}")
        fd, tmp = tempfile.mkstemp(prefix=".mf-", dir=out_dir)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(asset.data)  # type: ignore[arg-type]
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        written[asset.name] = (
            f"{link_prefix.rstrip('/')}/{name}" if link_prefix else name
        )
    return written


# ----------------------------------------------------------------- chunks ---


@dataclass
class Chunk:
    index: int
    text: str
    tokens: int
    heading_path: List[str] = field(default_factory=list)
    page_start: Optional[int] = None
    page_end: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "tokens": self.tokens,
            "heading_path": list(self.heading_path),
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


@dataclass
class _Block:
    text: str
    page: Optional[int]
    heading: Optional[Tuple[int, str]]  # (level, title) when the block is a heading
    path: List[str]


def _blocks(result: "DocumentConverterResult") -> List[_Block]:
    """Split the document into paragraph-level blocks with page + heading context."""
    if result.pages:
        segments: Iterable[Tuple[Optional[int], str]] = [
            (p.index, p.markdown) for p in result.pages
        ]
    else:
        segments = [(None, result.markdown)]
    blocks: List[_Block] = []
    path: List[Tuple[int, str]] = []
    for page, text in segments:
        cur: List[str] = []
        in_fence = False
        current_page = page

        def flush() -> None:
            body = "\n".join(cur).strip("\n")
            cur.clear()
            if not body.strip():
                return
            first = body.split("\n", 1)[0]
            m = _HEADING_RE.match(first) if "\n" not in body else None
            heading = None
            if m:
                level, title = len(m.group(1)), m.group(2).strip()
                while path and path[-1][0] >= level:
                    path.pop()
                path.append((level, title))
                heading = (level, title)
            blocks.append(_Block(body, current_page, heading, [t for _, t in path]))

        for line in (text or "").split("\n"):
            if _FENCE_RE.match(line.strip()):
                in_fence = not in_fence
                cur.append(line)
                continue
            if in_fence:
                cur.append(line)
                continue
            marker = _MARKER_RE.match(line.strip())
            if marker:
                flush()
                current_page = int(marker.group(2))
                continue
            if _HEADING_RE.match(line):
                flush()
                cur.append(line)
                flush()
                continue
            if not line.strip():
                flush()
                continue
            cur.append(line)
        flush()
    return blocks


def _split_long(text: str, max_tokens: int) -> List[str]:
    """Split one oversized block at line, then sentence, then hard boundaries."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    parts = (
        re.split(r"(?<=\n)", text) if "\n" in text else re.split(r"(?<=[.!?])\s+", text)
    )
    out: List[str] = []
    buf = ""
    for part in parts:
        if buf and estimate_tokens(buf + part) > max_tokens:
            out.append(buf)
            buf = ""
        if estimate_tokens(part) > max_tokens:
            start = 0
            while start < len(part):
                # ~4 characters per token, less for dense scripts such as CJK.
                width = max_tokens * 4
                while (
                    width > 1
                    and estimate_tokens(part[start : start + width]) > max_tokens
                ):
                    width //= 2
                out.append(part[start : start + width])
                start += width
            continue
        buf += part if not buf or buf.endswith("\n") else " " + part
    if buf:
        out.append(buf)
    return [p.strip() for p in out if p.strip()]


def chunk_result(
    result: "DocumentConverterResult",
    *,
    by: str = "heading",
    max_tokens: int = 800,
    overlap: int = 0,
    split_level: int = 3,
) -> List[Chunk]:
    """Split a result into retrieval chunks.

    by="heading": a new chunk starts at every heading of level <= split_level,
    and sections larger than max_tokens are split at paragraph boundaries.
    by="tokens": paragraphs are packed greedily up to max_tokens.
    ``overlap`` (tokens) repeats the tail of the previous chunk at the start
    of the next one. Each chunk records its heading path and page range.
    """
    if by not in ("heading", "tokens"):
        raise ValueError('by must be "heading" or "tokens"')
    if max_tokens < 16:
        raise ValueError("max_tokens must be at least 16")
    overlap = max(0, min(overlap, max_tokens // 2))

    chunks: List[Chunk] = []
    cur: List[_Block] = []
    cur_tokens = 0

    def emit() -> None:
        nonlocal cur, cur_tokens
        if not cur:
            return
        text = "\n\n".join(b.text for b in cur).strip()
        if text:
            pages = [b.page for b in cur if b.page is not None]
            path = next((b.path for b in reversed(cur) if b.path), [])
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=text,
                    tokens=estimate_tokens(text),
                    heading_path=list(path),
                    page_start=min(pages) if pages else None,
                    page_end=max(pages) if pages else None,
                )
            )
        tail: List[_Block] = []
        if overlap:
            got = 0
            for b in reversed(cur):
                if b.heading:
                    continue
                t = estimate_tokens(b.text)
                if got + t > overlap:
                    break
                tail.insert(0, b)
                got += t
        cur = tail
        cur_tokens = sum(estimate_tokens(b.text) for b in cur)

    for block in _blocks(result):
        pieces = (
            _split_long(block.text, max_tokens) if not block.heading else [block.text]
        )
        for piece in pieces:
            b = _Block(piece, block.page, block.heading, block.path)
            t = estimate_tokens(piece)
            starts_section = (
                by == "heading" and b.heading and b.heading[0] <= split_level
            )
            only_headings = all(x.heading for x in cur)
            too_big = cur_tokens + t > max_tokens and not only_headings
            if cur and (starts_section or too_big):
                emit()
                if starts_section:
                    cur, cur_tokens = [], 0  # no overlap across sections
                elif cur and cur_tokens + t > max_tokens:
                    cur, cur_tokens = [], 0  # the overlap would not fit
            cur.append(b)
            cur_tokens += t
    emit()
    return chunks
