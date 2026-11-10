# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read OOXML core properties (docProps/core.xml) from .docx/.xlsx/.pptx packages."""

import zipfile
from typing import Any, BinaryIO, Dict, Optional

from defusedxml import ElementTree as ET

_CORE_PART = "docProps/core.xml"

# Local element name -> metadata key. Namespaces differ (dc, dcterms, cp) but
# the local names are unique within core.xml.
_FIELDS = {
    "title": "title",
    "creator": "author",
    "lastModifiedBy": "last_modified_by",
    "created": "created",
    "modified": "modified",
    "subject": "subject",
    "keywords": "keywords",
    "description": "description",
    "category": "category",
    "revision": "revision",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_core_properties(xml: bytes) -> Dict[str, Any]:
    """Parse the bytes of a core.xml part into a flat dict of non-empty values."""
    out: Dict[str, Any] = {}
    root = ET.fromstring(xml)
    for child in root:
        key = _FIELDS.get(_local(child.tag))
        if key is None:
            continue
        value = (child.text or "").strip()
        if value:
            out[key] = value
    return out


def read_core_properties_from_zip(archive: zipfile.ZipFile) -> Dict[str, Any]:
    """Core properties from an open OOXML package, or {} when absent/unreadable."""
    try:
        return parse_core_properties(archive.read(_CORE_PART))
    except Exception:
        return {}


def read_core_properties(file_stream: BinaryIO) -> Dict[str, Any]:
    """Core properties from a seekable OOXML stream. The position is restored."""
    position: Optional[int] = None
    try:
        position = file_stream.tell()
        with zipfile.ZipFile(file_stream) as archive:
            return read_core_properties_from_zip(archive)
    except Exception:
        return {}
    finally:
        if position is not None:
            file_stream.seek(position)
