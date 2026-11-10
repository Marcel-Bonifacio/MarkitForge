# SPDX-FileCopyrightText: Copyright (c) Microsoft Corporation (MarkItDown)
# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.

import struct
import zipfile
from functools import partial
from io import BytesIO
from typing import BinaryIO, Callable, Dict, Optional, Tuple
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag

from .._archive_limits import (
    ArchiveBudget,
    check_archive,
    copy_member,
    read_member,
)
from .math.omml import OMML_NS, oMath2Latex
from .page_breaks import mark_page_breaks

MATH_ROOT_TEMPLATE = "".join(
    (
        "<w:document ",
        'xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" ',
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" ',
        'xmlns:o="urn:schemas-microsoft-com:office:office" ',
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ',
        'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" ',
        'xmlns:v="urn:schemas-microsoft-com:vml" ',
        'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" ',
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" ',
        'xmlns:w10="urn:schemas-microsoft-com:office:word" ',
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ',
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" ',
        'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" ',
        'xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk" ',
        'xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml" ',
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" mc:Ignorable="w14 wp14">',
        "{0}</w:document>",
    )
)


def _convert_omath_to_latex(tag: Tag) -> str:
    """
    Converts an OMML (Office Math Markup Language) tag to LaTeX format.

    Args:
        tag (Tag): A BeautifulSoup Tag object representing the OMML element.

    Returns:
        str: The LaTeX representation of the OMML element.
    """
    # Format the tag into a complete XML document string
    math_root = ET.fromstring(MATH_ROOT_TEMPLATE.format(str(tag)))
    # Find the 'oMath' element within the XML document
    math_element = math_root.find(OMML_NS + "oMath")
    if math_element is None:
        return ""
    # Convert the 'oMath' element to LaTeX using the oMath2Latex function
    latex = oMath2Latex(math_element).latex
    return latex


def _get_omath_tag_replacement(tag: Tag, block: bool = False) -> Tag:
    """
    Creates a replacement tag for an OMML (Office Math Markup Language) element.

    Args:
        tag (Tag): A BeautifulSoup Tag object representing the "oMath" element.
        block (bool, optional): If True, the LaTeX will be wrapped in double dollar signs for block mode. Defaults to False.

    Returns:
        Tag: A BeautifulSoup Tag object representing the replacement element.
    """
    t_tag = Tag(name="w:t")
    t_tag.string = (
        f"$${_convert_omath_to_latex(tag)}$$"
        if block
        else f"${_convert_omath_to_latex(tag)}$"
    )
    r_tag = Tag(name="w:r")
    r_tag.append(t_tag)
    return r_tag


def _replace_equations(tag: Tag):
    """
    Replaces OMML (Office Math Markup Language) elements with their LaTeX equivalents.

    Args:
        tag (Tag): A BeautifulSoup Tag object representing the OMML element. Could be either "oMathPara" or "oMath".

    Raises:
        ValueError: If the tag is not supported.
    """
    if tag.name == "oMathPara":
        # Create a new paragraph tag
        p_tag = Tag(name="w:p")
        # Replace each 'oMath' child tag with its LaTeX equivalent as block equations
        for child_tag in tag.find_all("oMath"):
            p_tag.append(_get_omath_tag_replacement(child_tag, block=True))
        # Replace the original 'oMathPara' tag with the new paragraph tag
        tag.replace_with(p_tag)
    elif tag.name == "oMath":
        # Replace the 'oMath' tag with its LaTeX equivalent as inline equation
        tag.replace_with(_get_omath_tag_replacement(tag, block=False))
    else:
        raise ValueError(f"Not supported tag: {tag.name}")


def _pre_process_strike(content: bytes) -> bytes:
    """
    Pre-processes the strikethrough content in a DOCX -> XML file by normalizing double
    strikethrough runs to single strikethrough runs.

    Word marks double strikethrough with "w:dstrike", which downstream converters do not
    recognize, causing those runs to lose their strikethrough entirely. Renaming the tag to
    "w:strike" preserves the strikethrough semantics.

    Args:
        content (bytes): The XML content of the DOCX file as bytes.

    Returns:
        bytes: The processed content with "dstrike" elements renamed to "strike", encoded as bytes.
    """
    # Double strikethrough is rare, and parsing/reserializing the XML is expensive
    # on large documents, so skip the round-trip when there is nothing to rename.
    if b"dstrike" not in content:
        return content

    soup = BeautifulSoup(content.decode(), features="xml")
    for tag in soup.find_all("dstrike"):
        tag.name = "strike"
    return str(soup).encode()


def _pre_process_math(content: bytes) -> bytes:
    """
    Pre-processes the math content in a DOCX -> XML file by converting OMML (Office Math Markup Language) elements to LaTeX.
    This preprocessed content can be directly replaced in the DOCX file -> XMLs.

    Args:
        content (bytes): The XML content of the DOCX file as bytes.

    Returns:
        bytes: The processed content with OMML elements replaced by their LaTeX equivalents, encoded as bytes.
    """
    soup = BeautifulSoup(content.decode(), features="xml")
    for tag in soup.find_all("oMathPara"):
        _replace_equations(tag)
    for tag in soup.find_all("oMath"):
        _replace_equations(tag)
    return str(soup).encode()


def _fix_zip_filename_casing(input_docx: BinaryIO) -> BinaryIO:
    """
    Fix ZIP files where local file header filenames differ in casing
    from the central directory filenames.

    Some document generators (e.g. certain Microsoft Word versions,
    legal document systems) produce .docx/.pptx files where the central
    directory records one casing (e.g. 'customXml/item2.xml') but
    the local file headers record another (e.g. 'customXML/item2.xml').
    Python's zipfile module raises BadZipFile when reading such files.

    This function patches local file header filenames to match the
    central directory, which is the authoritative source used by
    zipfile.ZipFile.
    """
    input_docx.seek(0)
    raw = bytearray(input_docx.read())

    # Read the central directory to get authoritative filenames
    try:
        with zipfile.ZipFile(BytesIO(raw), "r") as zf:
            cd_entries = {
                zi.header_offset: (zi.orig_filename, zi.flag_bits)
                for zi in zf.infolist()
            }
    except zipfile.BadZipFile:
        # Can't even read central directory — return as-is, let it fail later
        input_docx.seek(0)
        return input_docx

    patched = False
    for offset, (cd_name, flag_bits) in cd_entries.items():
        # Verify local file header signature
        if offset + 30 > len(raw) or raw[offset : offset + 4] != b"PK\x03\x04":
            continue

        local_fname_len = struct.unpack_from("<H", raw, offset + 26)[0]
        if offset + 30 + local_fname_len > len(raw):
            continue

        local_name = bytes(raw[offset + 30 : offset + 30 + local_fname_len])
        # ZIP filenames are cp437 unless flag bit 11 marks them as UTF-8, which is
        # how zipfile decoded orig_filename in the first place.
        central_name = cd_name.encode("utf-8" if flag_bits & 0x800 else "cp437")

        # Only patch if lengths match but content differs (casing mismatch)
        if (
            local_name != central_name
            and len(local_name) == len(central_name)
            and local_name.lower() == central_name.lower()
        ):
            raw[offset + 30 : offset + 30 + local_fname_len] = central_name
            patched = True

    if patched:
        return BytesIO(bytes(raw))
    input_docx.seek(0)
    return input_docx


def _pre_process_styles(content: bytes) -> bytes:
    """
    Repairs DOCX style definitions that Mammoth cannot read.

    Mammoth indexes ``w:type`` and ``w:styleId`` directly, so a ``w:style``
    element missing either attribute causes conversion to fail with a
    ``KeyError`` before any document text can be extracted.

    ``w:type`` is optional in OOXML: when it is absent the style type defaults
    to ``paragraph``, so the attribute is filled in rather than dropping the
    style, which would discard its formatting (a heading would be emitted as
    plain body text). A style with no ``w:styleId`` cannot be referenced by the
    document body, so it is removed. Double strikethrough is normalized to
    single strikethrough in the same namespace-aware pass.

    Match elements and attributes by namespace URI, preserving their qualified
    names when repairing the XML. Return the original bytes if no repair is needed.
    """
    from lxml import etree

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser=parser)
    changed = False

    for style in root.findall(namespace + "style"):
        if namespace + "styleId" not in style.attrib:
            root.remove(style)
            changed = True
        elif namespace + "type" not in style.attrib:
            style.set(namespace + "type", "paragraph")
            changed = True

    for strike in root.iter(namespace + "dstrike"):
        strike.tag = namespace + "strike"
        changed = True

    if not changed:
        return content

    return etree.tostring(root.getroottree(), encoding="utf-8", xml_declaration=True)


def pre_process_docx(
    input_docx: BinaryIO,
    page_break_sentinel: Optional[str] = None,
    budget: Optional[ArchiveBudget] = None,
) -> BinaryIO:
    """
    Pre-processes a DOCX file with provided steps.

    The process works by unzipping the DOCX file in memory, transforming specific XML files
    (such as converting OMML elements to LaTeX), and then zipping everything back into a
    DOCX file without writing to disk.

    Args:
        input_docx (BinaryIO): A binary input stream representing the DOCX file.
        page_break_sentinel (Optional[str]): When given, page breaks in the body
            are marked with this token (see ``page_breaks.mark_page_breaks``).
        budget (Optional[ArchiveBudget]): Archive limits of the conversion. The
            caller is expected to have run ``check_archive`` with it already;
            without one, default limits are applied and checked here.

    Only the parts that are transformed are read into memory; every other
    part is stream-copied in chunks. A part over the member limits raises
    ``ResourceLimitException``.

    Returns:
        BinaryIO: A binary output stream representing the processed DOCX file.
    """
    # Fix ZIP filename casing mismatch before any processing
    input_docx = _fix_zip_filename_casing(input_docx)

    output_docx = BytesIO()
    # The pre-processing steps to apply to each file in the .docx
    document_steps: Tuple[Callable[[bytes], bytes], ...] = (
        _pre_process_strike,
        _pre_process_math,
    )
    if page_break_sentinel:
        document_steps = (
            partial(mark_page_breaks, sentinel=page_break_sentinel),
        ) + document_steps
    pre_process_enable_files: Dict[str, Tuple[Callable[[bytes], bytes], ...]] = {
        "word/document.xml": document_steps,
        "word/footnotes.xml": (_pre_process_strike, _pre_process_math),
        "word/endnotes.xml": (_pre_process_strike, _pre_process_math),
        "word/styles.xml": (_pre_process_styles,),
    }
    with zipfile.ZipFile(input_docx, mode="r") as zip_input:
        if budget is None:
            budget = ArchiveBudget()
            check_archive(zip_input, budget)
        with zipfile.ZipFile(output_docx, mode="w") as zip_output:
            zip_output.comment = zip_input.comment
            # A duplicated name is written once, with its last entry's content
            # (what ZipFile.read(name) returns), at its first position.
            for name in dict.fromkeys(zip_input.namelist()):
                info = zip_input.getinfo(name)
                budget.check_member(info)
                steps = pre_process_enable_files.get(name)
                if not steps:
                    copy_member(
                        zip_input,
                        info,
                        zip_output,
                        budget,
                        compress_type=zipfile.ZIP_STORED,
                    )
                    continue
                updated_content = read_member(zip_input, info, budget)
                # Each step is applied independently, so one failing step does
                # not discard the results of the others.
                for pre_process_step in steps:
                    try:
                        updated_content = pre_process_step(updated_content)
                    except Exception:
                        pass
                zip_output.writestr(name, updated_content)
    output_docx.seek(0)
    return output_docx
