# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Plain-text extraction of DOCX page headers and footers."""

import re
import zipfile
from typing import BinaryIO, List, Tuple

from defusedxml import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_CHOICE = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice"
_PART = re.compile(r"^word/(header|footer)(\d*)\.xml$", re.IGNORECASE)


def _paragraph_text(paragraph) -> str:  # type: ignore[no-untyped-def]
    parts: List[str] = []

    def walk(el) -> None:  # type: ignore[no-untyped-def]
        for child in el:
            if child.tag == _MC_CHOICE:
                continue  # the Fallback branch carries the same text
            if child.tag == _W + "t":
                parts.append(child.text or "")
            elif child.tag in (_W + "tab", _W + "ptab"):
                parts.append("\t")
            elif child.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
            elif child.tag == _W + "p":
                continue  # nested paragraphs (text boxes) are visited separately
            else:
                walk(child)

    walk(paragraph)
    text = "".join(parts)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _part_text(xml: bytes) -> str:
    root = ET.fromstring(xml)
    paragraphs = [_paragraph_text(p) for p in root.iter(_W + "p")]
    return "\n".join(p for p in paragraphs if p)


def read_headers_footers(file_stream: BinaryIO) -> Tuple[List[str], List[str]]:
    """Return (headers, footers): the distinct non-empty texts, in part order.

    Word keeps one part per header/footer variant (first page, even pages,
    default) and per section, so identical texts are reported once.
    """
    position = file_stream.tell()
    found: dict = {"header": [], "footer": []}
    try:
        with zipfile.ZipFile(file_stream) as archive:
            names = []
            for name in archive.namelist():
                match = _PART.match(name)
                if match:
                    kind = match.group(1).lower()
                    number = int(match.group(2) or 0)
                    names.append((kind, number, name))
            for kind, _, name in sorted(names):
                try:
                    text = _part_text(archive.read(name))
                except Exception:
                    continue
                if text and text not in found[kind]:
                    found[kind].append(text)
    except zipfile.BadZipFile:
        pass
    finally:
        file_stream.seek(position)
    return found["header"], found["footer"]
