# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Resource-limit regressions for ZIP-based inputs, data-URI inlining and LLM calls.

Every fixture is built small in ``tmp_path`` (or memory) at test time; the
limits are lowered through the conversion kwargs so each bomb stays
proportionate while still exceeding them.
"""

import base64
import io
import os
import random
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Tuple

import pytest

from markitforge import MarkitForge, StreamInfo
from markitforge._exceptions import FileConversionException, ResourceLimitException
from markitforge.converter_utils._archive_limits import (
    ArchiveBudget,
    ArchiveLimits,
    CaptionCache,
    data_uri_length,
    read_member,
)
from markitforge.converter_utils.docx import page_breaks
from markitforge.converter_utils.docx.pre_process import pre_process_docx
from markitforge.converters import (
    DocxConverter,
    EpubConverter,
    PptxConverter,
    XlsxConverter,
    _epub_converter,
)
from markitforge.converters._mhtml_converter import MhtmlConverter

TEST_FILES_DIR = Path(__file__).parent / "test_files"
MIB = 1024 * 1024


# ----------------------------------------------------------------- helpers


def _zip(entries: Iterable[Tuple[str, bytes]], level: int = 9) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def _add_zero_member(package: bytes, name: str, size: int) -> bytes:
    """Copy of a ZIP package with ``size`` zero bytes added as ``name``."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package)) as source:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for info in source.infolist():
                z.writestr(info, source.read(info))
            with z.open(name, "w") as member:
                chunk = b"\0" * MIB
                for _ in range(size // MIB):
                    member.write(chunk)
    return out.getvalue()


def _convert(data: bytes, extension: str, **kwargs: Any):
    return MarkitForge().convert_stream(
        io.BytesIO(data), stream_info=StreamInfo(extension=extension), **kwargs
    )


def _limit_error(excinfo: pytest.ExceptionInfo) -> BaseException:
    """The ResourceLimitException MarkitForge raised (directly, or wrapped)."""
    exc = excinfo.value
    if isinstance(exc, ResourceLimitException):
        return exc
    assert isinstance(exc, FileConversionException)
    for attempt in exc.attempts or []:
        if attempt.exc_info and isinstance(attempt.exc_info[1], ResourceLimitException):
            return attempt.exc_info[1]
    raise AssertionError(f"no ResourceLimitException in {exc}")


def _png(width: int, height: int, seed: int) -> bytes:
    from PIL import Image

    rnd = random.Random(seed)
    image = Image.frombytes(
        "RGB",
        (width, height),
        bytes(rnd.getrandbits(8) for _ in range(width * height * 3)),
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class _FakeLLM:
    def __init__(self, caption: str = "a caption") -> None:
        self.calls = 0
        self._caption = caption
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls += 1
        message = SimpleNamespace(content=self._caption)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


# ------------------------------------------------------------- limit plumbing


def test_limits_defaults_and_validation() -> None:
    limits = ArchiveLimits.from_kwargs({})
    assert limits == ArchiveLimits(
        max_archive_bytes=500 * MIB,
        max_archive_member_bytes=200 * MIB,
        max_archive_entries=2000,
        max_archive_depth=3,
        max_compression_ratio=200,
        max_inline_bytes=50 * MIB,
        max_llm_calls=50,
    )
    assert ArchiveLimits.from_kwargs({"max_archive_depth": None}) == limits
    assert ArchiveLimits.from_kwargs({"max_llm_calls": 3}).max_llm_calls == 3
    for bad in (-1, "10", True):
        with pytest.raises(ValueError):
            ArchiveLimits.from_kwargs({"max_archive_bytes": bad})


def test_read_member_stops_streaming_at_the_member_cap() -> None:
    # The declared size passes the check that ran with a larger cap, but the
    # streaming read is cut off at the (now smaller) cap after ~1 MiB.
    data = os.urandom(3 * MIB)
    archive = zipfile.ZipFile(io.BytesIO(_zip([("blob.bin", data)], level=1)))
    budget = ArchiveBudget(ArchiveLimits(max_archive_member_bytes=MIB))
    with pytest.raises(ResourceLimitException, match="inflates past"):
        read_member(archive, archive.getinfo("blob.bin"), budget)
    assert read_member(archive, archive.getinfo("blob.bin"), ArchiveBudget()) == data


def test_caption_cache_dedupes_and_caps() -> None:
    cache = CaptionCache(2)
    produced: List[bytes] = []

    def make(data: bytes) -> Callable[[], str]:
        def produce() -> str:
            produced.append(data)
            return f"caption {len(produced)}"

        return produce

    assert cache.caption(b"a", make(b"a")) == "caption 1"
    assert cache.caption(b"a", make(b"a")) == "caption 1"
    assert cache.caption(b"b", make(b"b")) == "caption 2"
    assert cache.caption(b"c", make(b"c")) is None
    assert cache.caption(b"d", make(b"d")) is None
    assert produced == [b"a", b"b"]
    assert len(cache.warnings) == 1 and "max_llm_calls" in cache.warnings[0]


# ------------------------------------------------------------------ ZIP bombs


def test_zip_member_over_member_cap_is_skipped_without_reading() -> None:
    bomb = _add_zero_member(_zip([("a.txt", b"hello")]), "big.txt", 50 * MIB)
    start = time.monotonic()
    result = _convert(bomb, ".zip", max_archive_member_bytes=10 * MIB)
    assert time.monotonic() - start < 2
    assert "hello" in result.markdown
    assert "## File: big.txt" not in result.markdown
    assert any(
        "big.txt" in w and "max_archive_member_bytes" in w for w in result.warnings
    )


def test_zip_member_over_compression_ratio_is_skipped_by_default() -> None:
    bomb = _add_zero_member(_zip([("a.txt", b"hello")]), "zeros.txt", 8 * MIB)
    result = _convert(bomb, ".zip")
    assert "## File: zeros.txt" not in result.markdown
    assert any("max_compression_ratio" in w for w in result.warnings)


def test_zip_total_bytes_budget_stops_conversion() -> None:
    members = [(f"{i}.bin", os.urandom(2 * MIB)) for i in range(5)]
    result = _convert(_zip(members, level=1), ".zip", max_archive_bytes=5 * MIB)
    warnings = [w for w in result.warnings if "max_archive_bytes" in w]
    assert len(warnings) == 1 and "2.bin" in warnings[0]
    # After the budget is spent nothing else is even looked at.
    assert not any("3.bin" in w or "4.bin" in w for w in result.warnings)


def _fan_out(width: int, depth: int) -> bytes:
    current = _zip([("a.txt", b"leaf")])
    for _ in range(depth):
        current = _zip([(f"{i}.zip", current) for i in range(width)])
    return current


def test_zip_fan_out_is_capped_by_shared_entry_budget() -> None:
    # 8 + 64 + 512 + 4096 entries across the nesting; the budget is shared.
    start = time.monotonic()
    result = _convert(
        _fan_out(8, 3), ".zip", max_archive_entries=150, max_archive_depth=10
    )
    assert time.monotonic() - start < 5
    assert 0 < result.markdown.count("## File: a.txt") < 150
    assert result.markdown.count("## File: ") <= 150
    assert any("max_archive_entries" in w for w in result.warnings)


def test_zip_nesting_depth_is_capped() -> None:
    # Five archive levels: the default max_archive_depth=3 unpacks three.
    result = _convert(_fan_out(2, 4), ".zip")
    assert "## File: a.txt" not in result.markdown
    # Levels 1-3 are unpacked (1 + 2 + 4 archives); the 8 archives on level 4
    # are listed but refused, reported once plus a count of the rest.
    assert result.markdown.count("Content from the zip file") == 1 + 2 + 4 + 8
    assert len(result.warnings) == 2
    assert all("max_archive_depth" in w for w in result.warnings)
    assert result.warnings[1].startswith("Skipped 7 more archives")


def test_deep_zip_chain_does_not_hit_recursion_limit() -> None:
    current = _zip([("a.txt", b"deep")])
    for _ in range(60):
        current = _zip([("n.zip", current)])
    result = _convert(current, ".zip", max_archive_depth=5)
    assert "deep" not in result.markdown
    assert result.markdown.count("Content from the zip file") == 5 + 1  # + refused
    assert len(result.warnings) == 1
    assert "max_archive_depth" in result.warnings[0]
    assert result.warnings[0].startswith("n.zip: ")


def test_zip_reports_nested_document_limit_hits() -> None:
    docx = _add_zero_member(
        (TEST_FILES_DIR / "test.docx").read_bytes(), "word/media/pad.bin", 8 * MIB
    )
    result = _convert(_zip([("doc.docx", docx), ("b.txt", b"after")], level=1), ".zip")
    assert "after" in result.markdown
    assert any(
        "doc.docx" in w and "max_compression_ratio" in w for w in result.warnings
    )


# ------------------------------------------------------------- OOXML / EPUB


@pytest.mark.parametrize(
    "fixture,extension,member",
    [
        ("test.docx", ".docx", "word/media/pad.bin"),
        ("test.xlsx", ".xlsx", "xl/media/pad.bin"),
        ("test.pptx", ".pptx", "ppt/media/pad.bin"),
        ("test.epub", ".epub", "OEBPS/pad.bin"),
    ],
)
def test_package_bomb_is_rejected_before_reading(
    fixture: str, extension: str, member: str
) -> None:
    bomb = _add_zero_member((TEST_FILES_DIR / fixture).read_bytes(), member, 50 * MIB)
    start = time.monotonic()
    with pytest.raises(ResourceLimitException) as excinfo:
        _convert(bomb, extension, max_archive_member_bytes=10 * MIB)
    assert "max_archive_member_bytes" in str(_limit_error(excinfo))
    # Default limits catch it too, through the compression ratio.
    with pytest.raises(ResourceLimitException) as excinfo:
        _convert(bomb, extension)
    assert "max_compression_ratio" in str(_limit_error(excinfo))
    assert time.monotonic() - start < 3


def test_docx_pre_process_stream_copies_untouched_parts() -> None:
    payload = os.urandom(3 * MIB)
    with zipfile.ZipFile(TEST_FILES_DIR / "test.docx") as source:
        parts = [(i.filename, source.read(i)) for i in source.infolist()]
    package = _zip(parts + [("word/media/blob.bin", payload)], level=1)
    out = pre_process_docx(io.BytesIO(package))
    with zipfile.ZipFile(out) as archive:
        assert archive.read("word/media/blob.bin") == payload
    budget = ArchiveBudget(ArchiveLimits(max_archive_member_bytes=MIB))
    with pytest.raises(ResourceLimitException):
        pre_process_docx(io.BytesIO(package), budget=budget)


def test_xlsx_show_zeroes_repair_stream_copies_within_limits() -> None:
    from markitforge.converters import _xlsx_converter

    source = (TEST_FILES_DIR / "test.xlsx").read_bytes()
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        parts = [(i.filename, archive.read(i)) for i in archive.infolist()]
    payload = os.urandom(2 * MIB)
    package = _zip(parts + [("xl/media/blob.bin", payload)], level=1)
    repaired = _xlsx_converter._repair_sheetview_show_zeroes(io.BytesIO(package))
    with zipfile.ZipFile(repaired) as archive:
        assert archive.read("xl/media/blob.bin") == payload
    budget = ArchiveBudget(ArchiveLimits(max_archive_member_bytes=MIB))
    with pytest.raises(ResourceLimitException):
        _xlsx_converter._repair_sheetview_show_zeroes(io.BytesIO(package), 0, budget)


def _epub(chapters: Dict[str, str], spine: List[str]) -> bytes:
    manifest = "".join(
        f'<item id="{cid}" href="{cid}.xhtml" media-type="application/xhtml+xml"/>'
        for cid in chapters
    )
    itemrefs = "".join(f'<itemref idref="{cid}"/>' for cid in spine)
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">'
        "<metadata><dc:title>t</dc:title></metadata>"
        f"<manifest>{manifest}</manifest><spine>{itemrefs}</spine></package>"
    )
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    files = [
        ("mimetype", b"application/epub+zip"),
        ("META-INF/container.xml", container.encode()),
        ("content.opf", opf.encode()),
    ]
    for cid, text in chapters.items():
        body = f"<html><body><p>{text}</p></body></html>"
        files.append((f"{cid}.xhtml", body.encode()))
    return _zip(files)


def test_epub_duplicate_spine_entries_are_converted_once() -> None:
    data = _epub({"c": "chaptermarker " + "lorem ipsum " * 500}, ["c"] * 3000)
    start = time.monotonic()
    result = _convert(data, ".epub")
    assert time.monotonic() - start < 3
    assert result.markdown.count("chaptermarker") == 1
    assert len(result.markdown) < 10_000


def test_epub_spine_length_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_epub_converter, "MAX_SPINE_ITEMS", 3)
    chapters = {f"c{i}": f"chapter-{i}" for i in range(5)}
    result = EpubConverter().convert(
        io.BytesIO(_epub(chapters, list(chapters))), StreamInfo(extension=".epub")
    )
    assert "chapter-2" in result.markdown and "chapter-3" not in result.markdown
    assert any("only the first 3" in w for w in result.warnings)


# ------------------------------------------------------------------ MHTML


def _mhtml(refs: int, image: bytes) -> bytes:
    html = "<html><body>" + '<img src="cid:i1">' * refs + "</body></html>"
    return (
        'MIME-Version: 1.0\nContent-Type: multipart/related; boundary="BND"\n\n'
        f"--BND\nContent-Type: text/html; charset=utf-8\n\n{html}\n"
        "--BND\nContent-Type: image/png\nContent-ID: <i1>\n"
        "Content-Transfer-Encoding: base64\n\n"
        + base64.encodebytes(image).decode("ascii")
        + "\n--BND--\n"
    ).encode()


def test_mhtml_inlining_is_capped_and_deduplicated() -> None:
    image = os.urandom(30_000)
    data = _mhtml(200, image)
    uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    result = MhtmlConverter().convert(
        io.BytesIO(data),
        StreamInfo(extension=".mhtml"),
        keep_data_uris=True,
        max_inline_bytes=5 * len(uri) + 10,
    )
    assert result.markdown.count(uri) == 5
    assert result.markdown.count("](cid:i1)") == 195
    assert len(result.warnings) == 1 and "max_inline_bytes" in result.warnings[0]


def test_mhtml_inlines_everything_within_budget() -> None:
    image = os.urandom(3_000)
    result = MhtmlConverter().convert(
        io.BytesIO(_mhtml(10, image)),
        StreamInfo(extension=".mhtml"),
        keep_data_uris=True,
    )
    assert result.markdown.count("data:image/png;base64,") == 10
    assert result.warnings == []


# --------------------------------------------------- DOCX/PPTX images and LLM


def _docx_with_images(tmp_path: Path, images: List[bytes]) -> bytes:
    from docx import Document

    document = Document()
    for index, image in enumerate(images):
        path = tmp_path / f"img{index % 7}_{len(image)}.png"
        if not path.exists():
            path.write_bytes(image)
        document.add_picture(str(path))
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


@pytest.fixture(scope="module")
def repeated_image_docx(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    image = _png(150, 150, seed=1)  # ~68 KB, incompressible
    return _docx_with_images(tmp_path_factory.mktemp("docx"), [image] * 400)


def test_docx_repeated_image_default_output_skips_encoding(
    repeated_image_docx: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mammoth.images

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("images must not be base64-encoded by default")

    monkeypatch.setattr(mammoth.images.base64, "b64encode", fail)
    with zipfile.ZipFile(io.BytesIO(repeated_image_docx)) as archive:
        media = [n for n in archive.namelist() if n.startswith("word/media/")]
    assert len(media) == 1
    start = time.monotonic()
    result = DocxConverter().convert(
        io.BytesIO(repeated_image_docx), StreamInfo(extension=".docx")
    )
    assert time.monotonic() - start < 5
    assert result.markdown.count("](data:image/png;base64...)") == 400
    assert result.warnings == []


def test_docx_repeated_image_keep_data_uris_is_capped(
    repeated_image_docx: bytes,
) -> None:
    result = DocxConverter().convert(
        io.BytesIO(repeated_image_docx),
        StreamInfo(extension=".docx"),
        keep_data_uris=True,
        max_inline_bytes=1 * MIB,
    )
    inlined = result.markdown.count("](data:image/png;base64,")
    assert 1 <= inlined <= 12
    assert result.markdown.count("](image1.png)") == 400 - inlined
    assert len(result.markdown) < 2 * MIB
    assert any("max_inline_bytes" in w for w in result.warnings)


def test_docx_repeated_image_is_captioned_once(repeated_image_docx: bytes) -> None:
    client = _FakeLLM("a caption")
    result = DocxConverter().convert(
        io.BytesIO(repeated_image_docx),
        StreamInfo(extension=".docx"),
        describe_images=True,
        llm_client=client,
        llm_model="m",
    )
    assert client.calls == 1
    assert len(result.assets) == 1
    assert result.markdown.count("![a caption](data:image/png;base64...)") == 400


def test_docx_llm_calls_are_capped(tmp_path: Path) -> None:
    images = [_png(8, 8, seed=i) for i in range(5)]
    client = _FakeLLM("a caption")
    result = DocxConverter().convert(
        io.BytesIO(_docx_with_images(tmp_path, images)),
        StreamInfo(extension=".docx"),
        describe_images=True,
        llm_client=client,
        llm_model="m",
        max_llm_calls=2,
    )
    assert client.calls == 2
    assert result.markdown.count("![a caption]") == 2
    assert sum("max_llm_calls" in w for w in result.warnings) == 1


def test_docx_caption_cannot_escape_alt_text(tmp_path: Path) -> None:
    client = _FakeLLM("evil <b>bold</b> [x] \\")
    result = DocxConverter().convert(
        io.BytesIO(_docx_with_images(tmp_path, [_png(8, 8, seed=9)])),
        StreamInfo(extension=".docx"),
        describe_images=True,
        llm_client=client,
        llm_model="m",
    )
    assert "![evil b bold /b x](data:image/png;base64...)" in result.markdown
    assert "\\" not in result.markdown


def _pptx_with_images(images: List[bytes]) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    for image in images:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(image), Inches(1), Inches(1))
    out = io.BytesIO()
    presentation.save(out)
    return out.getvalue()


def test_pptx_identical_images_captioned_once_and_calls_capped() -> None:
    same = _png(8, 8, seed=1)
    distinct = [_png(8, 8, seed=i) for i in range(2, 6)]
    client = _FakeLLM("a caption")
    result = PptxConverter().convert(
        io.BytesIO(_pptx_with_images([same] * 20 + distinct)),
        StreamInfo(extension=".pptx"),
        llm_client=client,
        llm_model="m",
        max_llm_calls=3,
    )
    assert client.calls == 3
    assert result.markdown.count("![a caption") == 20 + 2
    assert sum("max_llm_calls" in w for w in result.warnings) == 1


def test_pptx_caption_cannot_escape_alt_text() -> None:
    client = _FakeLLM("evil [x] caption \\")
    result = PptxConverter().convert(
        io.BytesIO(_pptx_with_images([_png(8, 8, seed=3)])),
        StreamInfo(extension=".pptx"),
        llm_client=client,
        llm_model="m",
    )
    assert "\\" not in result.markdown
    # The caption is followed by the picture's own description ("image.png").
    assert "![evil x caption image.png](Picture1.jpg)" in result.markdown


def test_pptx_keep_data_uris_is_capped() -> None:
    image = _png(60, 60, seed=4)
    result = PptxConverter().convert(
        io.BytesIO(_pptx_with_images([image] * 30)),
        StreamInfo(extension=".pptx"),
        keep_data_uris=True,
        max_inline_bytes=5 * data_uri_length("image/png", len(image)) + 10,
    )
    assert result.markdown.count("](data:image/png;base64,") == 5
    assert any("max_inline_bytes" in w for w in result.warnings)


# --------------------------------------------------------------- page breaks


def _deep_document(depth: int, texts: int) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    runs = "<w:r><w:t>word</w:t></w:r>" * texts
    body = (
        "<w:customXml>" * depth
        + f'<w:p>{runs}<w:r><w:br w:type="page"/></w:r>{runs}</w:p>'
        + "</w:customXml>" * depth
    )
    return (
        f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}'
        "</w:body></w:document>"
    ).encode()


def test_page_breaks_are_linear_in_deep_documents() -> None:
    content = _deep_document(depth=240, texts=10_000)
    start = time.monotonic()
    marked = page_breaks.mark_page_breaks(content, "SENTINEL")
    assert time.monotonic() - start < 2
    assert marked.count(b"SENTINEL") == 1
    assert b"word</w:t>" in marked and b"wordSENTINEL</w:t>" in marked


def test_page_breaks_refuse_huge_tree_depth() -> None:
    from lxml import etree

    content = _deep_document(depth=2000, texts=10)
    start = time.monotonic()
    with pytest.raises(etree.XMLSyntaxError):
        page_breaks.mark_page_breaks(content, "SENTINEL")
    assert time.monotonic() - start < 1


def test_byte_budget_is_shared_with_documents_inside_a_zip() -> None:
    # The archive member itself fits the budget, the DOCX parts it holds do not.
    docx = (TEST_FILES_DIR / "test.docx").read_bytes()
    archive = _zip([("doc.docx", docx), ("z.txt", b"tail")], level=0)
    ok = _convert(archive, ".zip")
    assert "## File: doc.docx" in ok.markdown and ok.warnings == []
    limited = _convert(archive, ".zip", max_archive_bytes=len(docx) + 10)
    assert "## File: doc.docx" not in limited.markdown
    assert "## File: z.txt" not in limited.markdown  # the budget is spent
    assert len(limited.warnings) == 1
    assert "doc.docx" in limited.warnings[0]
    assert "max_archive_bytes" in limited.warnings[0]
