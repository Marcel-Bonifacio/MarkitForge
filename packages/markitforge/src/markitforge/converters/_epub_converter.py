# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import io
import mimetypes
import os
import posixpath
import zipfile
from urllib.parse import unquote
from defusedxml import minidom
from xml.dom.minidom import Document

from typing import BinaryIO, Any, Dict, List, Optional, Set

from ._html_converter import HtmlConverter
from .._base_converter import DocumentConverterResult
from .._progress import check_cancelled, report_progress
from .._stream_info import StreamInfo
from ..converter_utils._archive_limits import (
    ArchiveBudget,
    ImageAssets,
    check_archive,
    get_budget,
    read_member,
    wants_images,
)
from ..converter_utils._text_safety import safe_alt_text

ACCEPTED_MIME_TYPE_PREFIXES = [
    "application/epub",
    "application/epub+zip",
    "application/x-epub+zip",
]

ACCEPTED_FILE_EXTENSIONS = [".epub"]

#: Most spine documents converted from one EPUB (after deduplication).
MAX_SPINE_ITEMS = 5000

MIME_TYPE_MAPPING = {
    ".html": "text/html",
    ".xhtml": "application/xhtml+xml",
}


class EpubConverter(HtmlConverter):
    """
    Converts EPUB files to Markdown. Style information (e.g., headings) and tables are preserved where possible.

    The archive is checked against the archive resource limits before any
    member is read (``ResourceLimitException`` on a hit). Each spine document
    is converted once, and at most ``MAX_SPINE_ITEMS`` of them (with a warning).

    With ``extract_images`` every distinct image the chapters show (by its
    bytes) becomes an asset named after its file, and the Markdown refers to
    it by that name.
    """

    def __init__(self):
        super().__init__()
        self._html_converter = HtmlConverter()

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        budget = get_budget(kwargs)
        warnings: List[str] = []
        with zipfile.ZipFile(file_stream, "r") as z:
            # Validate every member's declared size before reading anything;
            # this raises ResourceLimitException for bombs.
            check_archive(z, budget)

            # Extracts metadata (title, authors, language, publisher, date, description, cover) from an EPUB file.

            # Locate content.opf
            container_dom = minidom.parse(z.open("META-INF/container.xml"))
            opf_path = container_dom.getElementsByTagName("rootfile")[0].getAttribute(
                "full-path"
            )

            # Parse content.opf
            opf_dom = minidom.parse(z.open(opf_path))
            metadata: Dict[str, Any] = {
                "title": self._get_text_from_node(opf_dom, "dc:title"),
                "authors": self._get_all_texts_from_nodes(opf_dom, "dc:creator"),
                "language": self._get_text_from_node(opf_dom, "dc:language"),
                "publisher": self._get_text_from_node(opf_dom, "dc:publisher"),
                "date": self._get_text_from_node(opf_dom, "dc:date"),
                "description": self._get_text_from_node(opf_dom, "dc:description"),
                "identifier": self._get_text_from_node(opf_dom, "dc:identifier"),
            }

            # Extract manifest items (ID → href mapping)
            manifest = {
                item.getAttribute("id"): item.getAttribute("href")
                for item in opf_dom.getElementsByTagName("item")
            }
            export = ImageAssets() if wants_images(kwargs) else None

            # Extract spine order (ID refs)
            spine_items = opf_dom.getElementsByTagName("itemref")
            spine_order = [item.getAttribute("idref") for item in spine_items]

            # Convert spine order to actual file paths
            base_path = "/".join(
                opf_path.split("/")[:-1]
            )  # Get base directory of content.opf
            zip_names = set(z.namelist())
            # A spine entry is converted once even if it is listed repeatedly,
            # and the number of documents is capped.
            spine = list(
                dict.fromkeys(
                    self._resolve_manifest_href(manifest[item_id], base_path, zip_names)
                    for item_id in spine_order
                    if item_id in manifest
                )
            )
            if len(spine) > MAX_SPINE_ITEMS:
                warnings.append(
                    f"The EPUB spine lists {len(spine)} documents; only the first "
                    f"{MAX_SPINE_ITEMS} were converted."
                )
                spine = spine[:MAX_SPINE_ITEMS]

            # Extract and convert the content
            markdown_content: List[str] = []
            exported: Dict[str, Optional[str]] = {}
            for chapter, file in enumerate(spine, start=1):
                check_cancelled(kwargs)
                if file in zip_names:
                    data = read_member(z, z.getinfo(file), budget)
                    filename = os.path.basename(file)
                    extension = os.path.splitext(filename)[1].lower()
                    mimetype = MIME_TYPE_MAPPING.get(extension)
                    if export is not None:
                        data = self._export_images(
                            z, file, data, zip_names, budget, export, exported, chapter
                        )
                    converted_content = self._html_converter.convert(
                        io.BytesIO(data),
                        StreamInfo(
                            mimetype=mimetype,
                            extension=extension,
                            filename=filename,
                        ),
                        **kwargs,
                    )
                    markdown_content.append(converted_content.markdown.strip())
                report_progress(kwargs, "page", chapter, len(spine))

            # Format and add the metadata
            metadata_markdown = []
            for key, value in metadata.items():
                if isinstance(value, list):
                    value = ", ".join(value)
                if value:
                    metadata_markdown.append(f"**{key.capitalize()}:** {value}")

            markdown_content.insert(0, "\n".join(metadata_markdown))

            return DocumentConverterResult(
                markdown="\n\n".join(markdown_content),
                title=metadata["title"],
                assets=export.assets if export is not None else [],
                warnings=warnings,
            )

    @staticmethod
    def _export_images(
        z: zipfile.ZipFile,
        chapter_path: str,
        data: bytes,
        zip_names: Set[str],
        budget: ArchiveBudget,
        export: ImageAssets,
        exported: Dict[str, Optional[str]],
        chapter: int,
    ) -> bytes:
        """Point the chapter's <img> (and SVG <image>) at exported assets.

        Images are resolved inside the archive only; one that is missing,
        external or not an image keeps its reference. Returns the rewritten
        chapter as UTF-8 HTML.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(data, "html.parser")
        base = posixpath.dirname(chapter_path)
        changed = False
        for img in soup.find_all(["img", "image"]):
            attr = "src"
            if img.name == "image":
                attr = next(
                    (a for a in ("xlink:href", "href") if img.get(a)), "xlink:href"
                )
            src = str(img.get(attr) or "").strip()
            if not src or ":" in src.split("/", 1)[0] or src.startswith("/"):
                continue  # external, data: or absolute: not package content
            path = posixpath.normpath(posixpath.join(base, unquote(src.split("#")[0])))
            if path not in exported:
                exported[path] = None
                if path in zip_names:
                    image = read_member(z, z.getinfo(path), budget)
                    mime = mimetypes.guess_type(path)[0] or ""
                    if mime.startswith("image/"):
                        exported[path] = export.add(
                            image,
                            mime,
                            name=posixpath.basename(path),
                            page=chapter,
                            description=str(img.get("alt") or "") or None,
                        )
            name = exported[path]
            if name is None:
                continue
            if img.name == "image":
                # markdownify ignores SVG <image>; show it as a plain <img>.
                img.replace_with(soup.new_tag("img", attrs={"src": name, "alt": ""}))
            else:
                img["src"] = name
                if img.get("alt"):
                    img["alt"] = safe_alt_text(str(img["alt"]))
            changed = True
        return str(soup).encode("utf-8") if changed else data

    def _resolve_manifest_href(
        self, href: str, base_path: str, zip_names: Set[str]
    ) -> str:
        """Resolve a manifest href to the matching ZIP entry name.

        Manifest hrefs are URI references relative to the OPF, so reserved
        characters such as spaces arrive percent-encoded, while ZIP entry names
        are not encoded. Prefer the decoded form, but fall back to the raw href
        so archives that store a literally-encoded name still resolve.
        """
        candidates: List[str] = []
        for candidate in (unquote(href), href):
            resolved = posixpath.join(base_path, candidate) if base_path else candidate
            resolved = posixpath.normpath(resolved)
            if resolved not in candidates:
                candidates.append(resolved)

        for candidate in candidates:
            if candidate in zip_names:
                return candidate

        return candidates[0]

    def _get_text_from_node(self, dom: Document, tag_name: str) -> str | None:
        """Convenience function to extract a single occurrence of a tag (e.g., title)."""
        texts = self._get_all_texts_from_nodes(dom, tag_name)
        if len(texts) > 0:
            return texts[0]
        else:
            return None

    def _get_all_texts_from_nodes(self, dom: Document, tag_name: str) -> List[str]:
        """Helper function to extract all occurrences of a tag (e.g., multiple authors)."""
        texts: List[str] = []
        for node in dom.getElementsByTagName(tag_name):
            text_parts: List[str] = []
            self._collect_node_text(node, text_parts)
            text_val = "".join(text_parts).strip()
            if text_val:
                texts.append(text_val)
        return texts

    def _collect_node_text(self, node: Any, text_parts: List[str]) -> None:
        """Recursively collect text node values from a DOM node."""
        for child in node.childNodes:
            if child.nodeType in (child.TEXT_NODE, child.CDATA_SECTION_NODE):
                if child.nodeValue:
                    text_parts.append(child.nodeValue)
            elif child.hasChildNodes():
                self._collect_node_text(child, text_parts)
