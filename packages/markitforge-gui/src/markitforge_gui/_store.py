# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A small, thread-safe, in-memory LRU store of conversion results."""

import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from markitforge import DocumentAsset, DocumentConverterResult

__all__ = ["StoredResult", "ResultStore", "new_id", "valid_id", "safe_asset_files"]

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def new_id() -> str:
    return secrets.token_urlsafe(16)


def valid_id(item_id: object) -> bool:
    """Ids end up in file names (history): only accept what new_id() makes."""
    return isinstance(item_id, str) and bool(_ID_RE.match(item_id))


@dataclass
class StoredResult:
    id: str
    source_name: str  # original file name or URL, for display
    md_filename: str  # safe "<name>.md" used for downloads
    result: DocumentConverterResult
    # Safe relative folder ("a/b") for folder uploads; "" otherwise.
    rel_dir: str = ""
    created: float = field(default_factory=time.time)
    edited: bool = False
    # The uploaded file, kept (up to a cap) for the side-by-side view.
    original: Optional[bytes] = field(default=None, repr=False)
    original_ext: str = ""
    original_size: Optional[int] = None
    # LibreOffice PDF rendering of an Office original, made on first view.
    preview_pdf: Optional[bytes] = field(default=None, repr=False)
    preview_error: Optional[str] = None
    # Extracted images (only with extract_images), bytes included.
    assets: List[DocumentAsset] = field(default_factory=list)
    # Byte size of every listed asset (result.assets), kept or not.
    asset_sizes: List[Optional[int]] = field(default_factory=list)

    def size(self) -> int:
        total = len(self.result.markdown)
        total += sum(len(p.markdown) for p in self.result.pages)
        total += len(self.original or b"") + len(self.preview_pdf or b"")
        total += sum(len(a.data or b"") for a in self.assets)
        return total


_ASSET_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_asset_name(name: str) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _ASSET_NAME_RE.sub("_", base).strip("._") or "asset"
    return base[:120]


def safe_asset_files(entry: "StoredResult") -> List[Tuple[DocumentAsset, str]]:
    """Kept assets paired with a safe, unique file name (no folders, no dots
    in front, case-insensitively unique)."""
    out: List[Tuple[DocumentAsset, str]] = []
    used: set = set()
    for asset in entry.assets:
        if asset.data is None:
            continue
        name = _safe_asset_name(asset.name)
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        n = 2
        while name.lower() in used:
            name = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
            n += 1
        used.add(name.lower())
        out.append((asset, name))
    return out


class ResultStore:
    """Keeps the most recent ``capacity`` results; the oldest is evicted first."""

    def __init__(self, capacity: int = 50, max_bytes: int = 1024 * 1024 * 1024) -> None:
        self.capacity = max(1, capacity)
        # Total bytes kept in memory (Markdown, originals, images). Oldest go first.
        self.max_bytes = max(1, max_bytes)
        self._items: "OrderedDict[str, StoredResult]" = OrderedDict()
        self._lock = threading.Lock()

    def add(
        self,
        source_name: str,
        md_filename: str,
        result: DocumentConverterResult,
        **extra: object,
    ) -> StoredResult:
        entry = StoredResult(
            id=new_id(),
            source_name=source_name,
            md_filename=md_filename,
            result=result,
            **extra,  # type: ignore[arg-type]
        )
        return self.put(entry)

    def put(self, entry: StoredResult) -> StoredResult:
        with self._lock:
            self._items[entry.id] = entry
            self._items.move_to_end(entry.id)
            while len(self._items) > self.capacity or (
                len(self._items) > 1 and self._size() > self.max_bytes
            ):
                self._items.popitem(last=False)
        return entry

    def _size(self) -> int:
        return sum(e.size() for e in self._items.values())

    def get(self, item_id: str) -> Optional[StoredResult]:
        with self._lock:
            entry = self._items.get(item_id)
            if entry is not None:
                self._items.move_to_end(item_id)
            return entry

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._items.pop(item_id, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def all(self) -> List[StoredResult]:
        with self._lock:
            return list(self._items.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
