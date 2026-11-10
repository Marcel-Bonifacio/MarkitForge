# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Conversion history kept on disk, so results survive a restart.

Layout (inside the per-user data directory, mode 0700)::

    history/<id>.json          markdown, title, metadata, pages, warnings
    history/<id>.assets/<file> extracted images (only with extract_images)

Every file is created with mode 0600 and written atomically (temporary file
in the same directory, fsync, ``os.replace``). Ids are checked against the
format :func:`new_id` produces before they are used in a path. The history is
capped by entry count and total bytes; the oldest entries are removed first.
Nothing about how a result was made (options, AI provider, API keys) is
stored: only the result itself.
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from markitforge import DocumentAsset, DocumentConverterResult, DocumentSection

from ._store import StoredResult, safe_asset_files, valid_id

__all__ = ["HistoryStore", "HistorySummary", "default_data_dir", "DATA_DIR_ENV"]

logger = logging.getLogger(__name__)

DATA_DIR_ENV = "MARKITFORGE_GUI_DATA_DIR"
_FORMAT_VERSION = 1
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


def default_data_dir() -> Path:
    """The per-user data directory for the GUI.

    ``MARKITFORGE_GUI_DATA_DIR`` wins; otherwise macOS
    ``~/Library/Application Support/MarkitForge``, Windows
    ``%LOCALAPPDATA%\\MarkitForge`` and elsewhere
    ``$XDG_DATA_HOME/markitforge`` (``~/.local/share/markitforge``).
    """
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MarkitForge"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "MarkitForge"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "markitforge"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"Refusing to use {path}: not a plain directory.")
    try:
        os.chmod(path, 0o700)
    except OSError:  # e.g. some network or Windows file systems
        pass


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".mf-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_bytes(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} is larger than expected.")
    return data


@dataclass
class HistorySummary:
    id: str
    source_name: str
    md_filename: str
    rel_dir: str
    title: Optional[str]
    created: float
    edited: bool
    bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.source_name,
            "md_filename": self.md_filename,
            "rel_dir": self.rel_dir,
            "title": self.title,
            "created": self.created,
            "edited": self.edited,
        }


class HistoryStore:
    """Results on disk, capped by ``max_entries`` and ``max_bytes``."""

    def __init__(
        self,
        directory: os.PathLike,
        *,
        max_entries: int = 200,
        max_bytes: int = 512 * 1024 * 1024,
        max_entry_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.dir = Path(directory)
        self.max_entries = max(1, max_entries)
        self.max_bytes = max(1, max_bytes)
        self.max_entry_bytes = max(1, max_entry_bytes)
        self._lock = threading.Lock()
        self._index: Dict[str, HistorySummary] = {}
        _private_dir(self.dir.parent)
        _private_dir(self.dir)
        self._scan()

    # ---- paths ------------------------------------------------------------

    def _json_path(self, item_id: str) -> Path:
        if not valid_id(item_id):
            raise ValueError("Bad history id.")
        return self.dir / f"{item_id}.json"

    def _assets_dir(self, item_id: str) -> Path:
        if not valid_id(item_id):
            raise ValueError("Bad history id.")
        return self.dir / f"{item_id}.assets"

    # ---- loading ----------------------------------------------------------

    def _scan(self) -> None:
        found: Dict[str, HistorySummary] = {}
        for path in self.dir.glob("*.json"):
            item_id = path.stem
            if not valid_id(item_id) or path.is_symlink():
                continue
            try:
                data = json.loads(_read_bytes(path, self.max_entry_bytes))
                found[item_id] = self._summary(item_id, data, path)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Skipping unreadable history file %s: %s", path, exc)
        with self._lock:
            self._index = dict(sorted(found.items(), key=lambda kv: kv[1].created))
            self._enforce_caps()

    def _summary(
        self, item_id: str, data: Dict[str, Any], path: Path
    ) -> HistorySummary:
        if data.get("version") != _FORMAT_VERSION or data.get("id") != item_id:
            raise ValueError("unknown history format")
        size = path.stat().st_size
        assets_dir = self._assets_dir(item_id)
        if assets_dir.is_dir() and not assets_dir.is_symlink():
            size += sum(p.stat().st_size for p in assets_dir.iterdir() if p.is_file())
        return HistorySummary(
            id=item_id,
            source_name=str(data["source_name"]),
            md_filename=str(data["md_filename"]),
            rel_dir=str(data.get("rel_dir") or ""),
            title=data.get("title") if isinstance(data.get("title"), str) else None,
            created=float(data.get("created") or 0),
            edited=bool(data.get("edited")),
            bytes=size,
        )

    def list(self) -> List[HistorySummary]:
        with self._lock:
            return list(reversed(list(self._index.values())))

    def __contains__(self, item_id: object) -> bool:
        with self._lock:
            return item_id in self._index

    def __len__(self) -> int:
        with self._lock:
            return len(self._index)

    def total_bytes(self) -> int:
        with self._lock:
            return sum(s.bytes for s in self._index.values())

    def load(self, item_id: str) -> Optional[StoredResult]:
        """Read a result back from disk (None when it is not in the history)."""
        if not valid_id(item_id):
            return None
        with self._lock:
            if item_id not in self._index:
                return None
            try:
                data = json.loads(
                    _read_bytes(self._json_path(item_id), self.max_entry_bytes)
                )
                return self._entry_from(item_id, data)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Could not read history entry %s: %s", item_id, exc)
                return None

    def _entry_from(self, item_id: str, data: Dict[str, Any]) -> StoredResult:
        pages = [
            DocumentSection(
                index=int(p["index"]),
                markdown=str(p.get("markdown") or ""),
                title=p.get("title") if isinstance(p.get("title"), str) else None,
                kind=str(p.get("kind") or "page"),
            )
            for p in data.get("pages") or []
        ]
        assets: List[DocumentAsset] = []
        listed: List[DocumentAsset] = []
        sizes: List[Optional[int]] = []
        assets_dir = self._assets_dir(item_id)
        for a in data.get("assets") or []:
            asset = DocumentAsset(
                name=str(a.get("name") or "asset"),
                mime_type=a.get("mime_type")
                if isinstance(a.get("mime_type"), str)
                else None,
                description=a.get("description")
                if isinstance(a.get("description"), str)
                else None,
                page=a.get("page") if isinstance(a.get("page"), int) else None,
            )
            file_name = a.get("file")
            if (
                isinstance(file_name, str)
                and file_name == os.path.basename(file_name)
                and not file_name.startswith(".")
            ):
                try:
                    asset.data = _read_bytes(
                        assets_dir / file_name, self.max_entry_bytes
                    )
                    assets.append(asset)
                except (OSError, ValueError):
                    pass
            listed.append(asset)
            size = a.get("size")
            sizes.append(size if isinstance(size, int) else None)
        metadata = (
            data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        )
        warnings = [str(w) for w in data.get("warnings") or []]
        result = DocumentConverterResult(
            markdown=str(data.get("markdown") or ""),
            title=data.get("title") if isinstance(data.get("title"), str) else None,
            metadata=metadata,
            pages=pages,
            assets=listed,
            warnings=warnings,
        )
        return StoredResult(
            id=item_id,
            source_name=str(data["source_name"]),
            md_filename=str(data["md_filename"]),
            result=result,
            rel_dir=str(data.get("rel_dir") or ""),
            created=float(data.get("created") or 0),
            edited=bool(data.get("edited")),
            assets=assets,
            asset_sizes=sizes,
        )

    # ---- saving -----------------------------------------------------------

    def save(self, entry: StoredResult) -> None:
        """Write (or rewrite) one entry, then apply the caps."""
        result = entry.result
        data = result.to_dict(include_markdown=True)
        # Asset bytes only when they were extracted on purpose; names are
        # made safe and unique, so they cannot leave the entry's folder.
        files = safe_asset_files(entry)
        name_for = {id(a): name for a, name in files}
        assets_out = []
        for i, asset in enumerate(result.assets):
            out = asset.to_dict()
            if out.get("size") is None and i < len(entry.asset_sizes):
                out["size"] = entry.asset_sizes[i]
            if id(asset) in name_for:
                out["file"] = name_for[id(asset)]
            assets_out.append(out)
        payload = {
            "version": _FORMAT_VERSION,
            "id": entry.id,
            "source_name": entry.source_name,
            "md_filename": entry.md_filename,
            "rel_dir": entry.rel_dir,
            "created": entry.created,
            "edited": entry.edited,
            "title": result.title,
            "markdown": result.markdown,
            "metadata": data["metadata"],
            "pages": data["pages"],
            "warnings": data["warnings"],
            "assets": assets_out,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        asset_bytes = sum(len(a.data or b"") for a, _ in files)
        if len(raw) + asset_bytes > self.max_entry_bytes:
            logger.info("Result %s is too large for the history; not saved.", entry.id)
            return
        with self._lock:
            json_path = self._json_path(entry.id)
            assets_dir = self._assets_dir(entry.id)
            if files:
                _private_dir(assets_dir)
                for asset, name in files:
                    _atomic_write(assets_dir / name, asset.data or b"")
            _atomic_write(json_path, raw)
            self._index.pop(entry.id, None)
            self._index[entry.id] = HistorySummary(
                id=entry.id,
                source_name=entry.source_name,
                md_filename=entry.md_filename,
                rel_dir=entry.rel_dir,
                title=result.title,
                created=entry.created,
                edited=entry.edited,
                bytes=len(raw) + asset_bytes,
            )
            # Keep creation order: re-sort after an edit rewrote an old entry.
            self._index = dict(
                sorted(self._index.items(), key=lambda kv: kv[1].created)
            )
            self._enforce_caps()

    def _enforce_caps(self) -> None:
        # Caller holds the lock.
        while len(self._index) > self.max_entries or (
            len(self._index) > 1
            and sum(s.bytes for s in self._index.values()) > self.max_bytes
        ):
            oldest = next(iter(self._index))
            self._delete_files(oldest)
            self._index.pop(oldest, None)

    def _delete_files(self, item_id: str) -> None:
        try:
            self._json_path(item_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not delete history entry %s: %s", item_id, exc)
        assets_dir = self._assets_dir(item_id)
        if assets_dir.is_symlink():
            assets_dir.unlink()
        elif assets_dir.is_dir():
            shutil.rmtree(assets_dir, ignore_errors=True)

    def delete(self, item_id: str) -> None:
        if not valid_id(item_id):
            return
        with self._lock:
            self._delete_files(item_id)
            self._index.pop(item_id, None)

    def clear(self) -> int:
        """Delete every history entry; returns how many were removed."""
        with self._lock:
            ids = set(self._index)
            for path in self.dir.iterdir():
                stem = path.name.split(".", 1)[0]
                if valid_id(stem) and path.name in (f"{stem}.json", f"{stem}.assets"):
                    ids.add(stem)
            for item_id in ids:
                self._delete_files(item_id)
            for leftover in self.dir.glob(".mf-*.tmp"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            count = len(self._index)
            self._index.clear()
            return count
