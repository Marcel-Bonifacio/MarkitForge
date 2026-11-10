# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A small, thread-safe, in-memory LRU store of conversion results."""

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional

from markitforge import DocumentConverterResult

__all__ = ["StoredResult", "ResultStore"]


@dataclass
class StoredResult:
    id: str
    source_name: str  # original file name or URL, for display
    md_filename: str  # safe "<name>.md" used for downloads
    result: DocumentConverterResult


class ResultStore:
    """Keeps the most recent ``capacity`` results; the oldest is evicted first."""

    def __init__(self, capacity: int = 50, max_bytes: int = 1024 * 1024 * 1024) -> None:
        self.capacity = max(1, capacity)
        # Total Markdown kept in memory (characters, ~bytes). Oldest go first.
        self.max_bytes = max(1, max_bytes)
        self._items: "OrderedDict[str, StoredResult]" = OrderedDict()
        self._lock = threading.Lock()

    def add(
        self, source_name: str, md_filename: str, result: DocumentConverterResult
    ) -> StoredResult:
        entry = StoredResult(
            id=secrets.token_urlsafe(16),
            source_name=source_name,
            md_filename=md_filename,
            result=result,
        )
        with self._lock:
            self._items[entry.id] = entry
            while len(self._items) > self.capacity or (
                len(self._items) > 1 and self._size() > self.max_bytes
            ):
                self._items.popitem(last=False)
        return entry

    def _size(self) -> int:
        return sum(len(e.result.markdown) for e in self._items.values())

    def get(self, item_id: str) -> Optional[StoredResult]:
        with self._lock:
            entry = self._items.get(item_id)
            if entry is not None:
                self._items.move_to_end(item_id)
            return entry

    def all(self) -> List[StoredResult]:
        with self._lock:
            return list(self._items.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
