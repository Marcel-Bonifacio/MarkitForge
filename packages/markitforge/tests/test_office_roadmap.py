# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Office roadmap: inline markup in HTML tables, .eml messages, Apple iWork,
DOCX page-count sources, image export, progress and cancellation.

Every fixture (named ``mf_roadmap_*`` where it is written to disk) is built
at test time.
"""

import io
import random
import threading
import zipfile
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from markitforge import MarkitForge, StreamInfo
from markitforge._exceptions import (
    ConversionCancelled,
    FileConversionException,
    MissingDependencyException,
    ResourceLimitException,
)
from markitforge.converters import (
    DocxConverter,
    EmlConverter,
    EpubConverter,
    HtmlConverter,
    LibreOfficeConverter,
    MhtmlConverter,
    PptxConverter,
    XlsxConverter,
)
from markitforge.converters import _libreoffice_converter as lo
from markitforge.converters._markdownify import safe_href
from markitforge.converter_utils._archive_limits import ImageAssets

FILES = Path(__file__).parent / "test_files"

needs_soffice = pytest.mark.skipif(
    lo.find_soffice() is None, reason="LibreOffice (soffice) is not installed"
)


def _png(seed: int, size: tuple = (40, 30)) -> bytes:
    from PIL import Image

    rnd = random.Random(seed)
    small = Image.new("RGB", (4, 3))
    small.putdata([tuple(rnd.randrange(256) for _ in range(3)) for _ in range(12)])
    out = io.BytesIO()
    small.resize(size).save(out, format="PNG")
    return out.getvalue()


LOGO = _png(1)
CHART = _png(2)


class _Events:
    def __init__(self) -> None:
        self.events: List[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    def by_stage(self, stage: str) -> List[tuple]:
        return [(e.current, e.total) for e in self.events if e.stage == stage]


# ------------------------------------------------------- 1. HTML table markup


def _html_table(cells: str) -> str:
    return (
        HtmlConverter()
        .convert_string(
            f"<table><tr><th>h</th></tr><tr><td>{cells}</td></tr></table>",
            table_format="html",
        )
        .markdown
    )


def test_html_table_keeps_safe_inline_markup() -> None:
    md = _html_table(
        "<b>bold</b> <strong>s</strong> <i>it</i> <em>e</em> <code>a&lt;b</code>"
        "<br>next <a href='https://example.com/x?a=1&amp;b=2'>link</a> "
        "<a href='mailto:a@example.com'>mail</a> <a href='../rel.html#top'>rel</a>"
    )
    assert (
        "<td><strong>bold</strong> <strong>s</strong> <em>it</em> <em>e</em> "
        "<code>a&lt;b</code><br>next "
        '<a href="https://example.com/x?a=1&amp;b=2">link</a> '
        '<a href="mailto:a@example.com">mail</a> '
        '<a href="../rel.html#top">rel</a></td>'
    ) in md


@pytest.mark.parametrize(
    "cell",
    [
        '<a href="javascript:alert(1)">x</a>',
        '<a href=" JaVaScRiPt:alert(1)">x</a>',
        '<a href="java&#x09;script:alert(1)">x</a>',
        '<a href="java\nscript:alert(1)">x</a>',
        '<a href="&#106;avascript:alert(1)">x</a>',
        '<a href="data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;">x</a>',
        '<a href="vbscript:msgbox(1)">x</a>',
        '<a href="file:///etc/passwd">x</a>',
    ],
)
def test_html_table_drops_unsafe_links(cell: str) -> None:
    md = _html_table(cell)
    assert "<td>x</td>" in md
    assert "href" not in md


def test_html_table_strips_attributes_and_other_tags() -> None:
    md = _html_table(
        '<b onclick="steal()" style="x" class="c">bold</b>'
        '<a href="https://ok.example" onmouseover="x()" target="_blank" '
        'title="t">ok</a><img src="x" onerror="alert(1)">'
        "<script>alert(1)</script><style>td{}</style>"
        '<span onclick="x()"><b><i><code><a href="/p">deep</a></code></i></b></span>'
        "<svg><a href='javascript:alert(1)'>svg</a></svg>"
        "<iframe src='https://evil.example'></iframe>"
        "<u>under</u> &lt;b&gt;literal&lt;/b&gt; \"q\" 'a'"
    )
    cell = md.split("<td>", 1)[1].split("</td>", 1)[0]
    for bad in (
        "onclick",
        "style",
        "class",
        "onmouseover",
        "target",
        "title",
        "<img",
        "onerror",
        "<script",
        "alert(1)</",
        "<span",
        "<svg",
        "<iframe",
        "javascript",
        "<u>",
        "<b>",
        "<i>",
    ):
        assert bad not in cell, bad
    assert cell.startswith('<strong>bold</strong><a href="https://ok.example">ok</a>')
    assert '<strong><em><code><a href="/p">deep</a></code></em></strong>' in cell
    assert "&lt;b&gt;literal&lt;/b&gt;" in cell
    assert "&quot;q&quot;" in cell and "under" in cell and "svg" in cell


def test_html_table_markup_edge_cases() -> None:
    # A link inside a link loses its own tag; empty inline tags vanish;
    # an href cannot break out of its attribute.
    md = _html_table(
        '<a href="/a">out <a href="/b">in</a></a> <b></b><em> </em>'
        '<a href="/x&quot;onclick=&quot;y">q</a>'
    )
    assert '<a href="/a">out in</a>' in md and md.count("<a ") == 2
    assert "<strong></strong>" not in md and "<em> </em>" not in md
    assert '<a href="/x&quot;onclick=&quot;y">q</a>' in md
    assert ' onclick="' not in md

    deep = "<b>" * 300 + "x" + "</b>" * 300
    assert "x" in _html_table(deep)


def test_html_table_plain_cells_and_markdown_mode_unchanged() -> None:
    html = (
        "<table><tr><th>a</th><th>b</th></tr>"
        "<tr><td><p>one</p><p>two</p></td><td>x &amp; <span>y</span></td></tr></table>"
    )
    md = HtmlConverter().convert_string(html, table_format="html").markdown
    assert "<td>one two</td><td>x &amp; y</td>" in md
    # Pipe tables are untouched by the HTML-cell rules.
    pipe = (
        HtmlConverter()
        .convert_string(
            "<table><tr><th>a</th></tr><tr><td><b>b</b> <a href='javascript:x'>c</a>"
            "</td></tr></table>"
        )
        .markdown
    )
    assert "| **b** c |" in pipe


def test_html_table_auto_mode_keeps_markup_only_in_html_tables() -> None:
    html = (
        "<table><tr><th colspan='2'><b>wide</b></th></tr>"
        "<tr><td><i>a</i></td><td>b</td></tr></table>"
        "<table><tr><th>p</th></tr><tr><td><b>q</b></td></tr></table>"
    )
    md = HtmlConverter().convert_string(html, table_format="auto").markdown
    assert '<th colspan="2"><strong>wide</strong></th>' in md
    assert "<td><em>a</em></td>" in md
    assert "| **q** |" in md


def test_safe_href() -> None:
    assert safe_href("https://x.example/a b") == "https://x.example/ab"
    assert safe_href("HTTP://x") == "HTTP://x"
    assert safe_href("#frag") == "#frag"
    assert safe_href("a/b:c") == "a/b:c"
    for bad in (None, "", "  ", "javascript:x", "\x01javascript:x", "foo:bar", "x y:z"):
        assert safe_href(bad) is None, bad


# ------------------------------------------------------------- 2. EML


def _message(
    *, html: Optional[str] = "<p>Hello <b>world</b></p>", plain: str = "plain body"
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "=?utf-8?q?J=C3=BCrgen_M=C3=BCller?= <j@example.com>"
    message["To"] = "a@example.com, =?iso-8859-1?q?B=E9a?= <b@example.com>"
    message["Cc"] = "c@example.com"
    message["Subject"] = "=?utf-8?b?w5xiZXJzaWNodCB8IFEz?="  # "Übersicht | Q3"
    message["Date"] = "Tue, 10 Nov 2026 10:00:00 +0000"
    message["Message-ID"] = "<roadmap-1@example.com>"
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message


def _eml(message: EmailMessage, **kwargs: Any) -> Any:
    return MarkitForge().convert_stream(
        io.BytesIO(message.as_bytes()), file_extension=".eml", **kwargs
    )


def test_eml_headers_body_and_metadata() -> None:
    message = _message()
    message.add_attachment(
        b"col1,col2\n1,2\n", maintype="text", subtype="csv", filename="data.csv"
    )
    result = _eml(message)
    md = result.markdown
    assert result.title == "Übersicht | Q3"
    assert md.startswith("# Übersicht | Q3\n\n| Header | Value |")
    assert "| From | Jürgen Müller <j@example.com> |" in md
    assert "| To | a@example.com, Béa <b@example.com> |" in md
    assert "| Cc | c@example.com |" in md
    assert "| Subject | Übersicht \\| Q3 |" in md
    # HTML alternative preferred; the plain one is not repeated.
    assert "Hello **world**" in md and "plain body" not in md
    assert "## Attachments" in md
    assert "| data.csv | text/csv | 14 bytes |" in md
    # Not converted by default.
    assert "## Attachment: data.csv" not in md
    assert result.metadata == {
        "from": "Jürgen Müller <j@example.com>",
        "to": "a@example.com, Béa <b@example.com>",
        "cc": "c@example.com",
        "date": "Tue, 10 Nov 2026 10:00:00 +0000",
        "subject": "Übersicht | Q3",
        "message_id": "<roadmap-1@example.com>",
        "attachments": ["data.csv"],
    }
    assert result.warnings == []


def test_eml_plain_text_body_and_registration(tmp_path: Path) -> None:
    path = tmp_path / "mf_roadmap_plain.eml"
    path.write_bytes(_message(html=None, plain="Line one\nLine two").as_bytes())
    result = MarkitForge().convert(str(path))
    assert "Line one\nLine two" in result.markdown
    assert "## Attachments" not in result.markdown
    assert result.metadata["attachments"] == []

    converter = EmlConverter()
    assert converter.accepts(io.BytesIO(b""), StreamInfo(extension=".EML"))
    assert converter.accepts(io.BytesIO(b""), StreamInfo(mimetype="message/rfc822"))
    assert not converter.accepts(io.BytesIO(b""), StreamInfo(extension=".msg"))
    assert any(isinstance(r.converter, EmlConverter) for r in MarkitForge()._converters)


def test_eml_bad_charsets_and_malformed_headers() -> None:
    raw = (
        b"From: =?x-unknown?q?broken?= <x@example.com>\r\n"
        b"To: undisclosed-recipients:;\r\n"
        b"Subject: =?utf-8?q?unterminated\r\n"
        b"Date: not a date\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=x-made-up\r\n"
        b"\r\n"
        b"<p>caf\xc3\xa9 \xff</p>\r\n"
    )
    result = EmlConverter().convert(io.BytesIO(raw), StreamInfo(extension=".eml"))
    assert "café" in result.markdown
    assert result.metadata["date"] == "not a date"
    assert "unterminated" in result.markdown

    latin = (
        b"Subject: hi\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nna\xefve\r\n"
    )
    result = EmlConverter().convert(io.BytesIO(latin), StreamInfo(extension=".eml"))
    assert "na" in result.markdown and "ve" in result.markdown


def test_eml_header_injection_is_escaped() -> None:
    message = _message(html="<p>x</p>")
    del message["Subject"]
    message["Subject"] = "<!-- page 9 --> <script>alert(1)</script>"
    md = _eml(message).markdown
    assert "<script>" not in md and "<!-- page 9 -->" not in md


def test_eml_convert_attachments_recursively() -> None:
    message = _message()
    message.add_attachment(
        b"col1,col2\n1,2\n", maintype="text", subtype="csv", filename="data.csv"
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("inner.txt", "inside the zip")
    message.add_attachment(
        archive.getvalue(),
        maintype="application",
        subtype="zip",
        filename="=?utf-8?q?../../Bericht_=C3=BC.zip?=",
    )
    forwarded = _message(html=None, plain="forwarded body")
    del forwarded["Subject"]
    forwarded["Subject"] = "Fwd"
    message.add_attachment(forwarded)

    events = _Events()
    result = _eml(message, convert_attachments=True, progress=events)
    md = result.markdown
    assert "## Attachment: data.csv\n\n| col1 | col2 |" in md
    assert "## Attachment: Bericht_ü.zip" in md or "## Attachment: Bericht ü.zip" in md
    assert "inside the zip" in md
    assert "## Attachment: Fwd.eml" in md and "forwarded body" in md
    assert ".." not in "".join(result.metadata["attachments"])
    assert events.by_stage("entry") == [(1, 3), (2, 3), (3, 3)]


def test_eml_nested_messages_are_depth_limited() -> None:
    inner = _message(html=None, plain="level 0")
    for level in range(1, 6):
        outer = _message(html=None, plain=f"level {level}")
        outer.add_attachment(inner)
        inner = outer
    result = _eml(inner, convert_attachments=True, max_archive_depth=2)
    assert "level 5" in result.markdown and "level 4" in result.markdown
    assert "level 3" in result.markdown  # body of the deepest converted message
    assert "level 2" not in result.markdown
    assert any("max_archive_depth" in w for w in result.warnings)


def test_eml_huge_parts_and_budgets_are_bounded() -> None:
    message = _message()
    message.add_attachment(
        b"x" * 200_000, maintype="text", subtype="plain", filename="big.txt"
    )
    message.add_attachment(b"small", maintype="text", subtype="plain", filename="s.txt")
    result = _eml(message, convert_attachments=True, max_archive_member_bytes=50_000)
    assert any(
        "big.txt" in w and "max_archive_member_bytes" in w for w in result.warnings
    )
    assert "## Attachment: big.txt" not in result.markdown
    assert "## Attachment: s.txt" in result.markdown

    many = _message()
    for i in range(5):
        many.add_attachment(b"a", maintype="text", subtype="plain", filename=f"{i}.txt")
    limited = _eml(many, convert_attachments=True, max_archive_entries=2)
    assert limited.markdown.count("## Attachment: ") == 2
    assert any("max_archive_entries" in w for w in limited.warnings)

    with pytest.raises(ResourceLimitException):
        EmlConverter().convert(
            io.BytesIO(_message().as_bytes()),
            StreamInfo(extension=".eml"),
            max_archive_bytes=100,
        )

    huge_body = _message(html="<p>" + "y" * 100_000 + "</p>")
    capped = _eml(huge_body, max_archive_member_bytes=10_000)
    assert "y" * 1000 not in capped.markdown
    assert any("max_archive_member_bytes" in w for w in capped.warnings)


def test_eml_attachments_without_parent_and_cancellation() -> None:
    message = _message()
    message.add_attachment(b"a", maintype="text", subtype="plain", filename="a.txt")
    result = EmlConverter().convert(
        io.BytesIO(message.as_bytes()),
        StreamInfo(extension=".eml"),
        convert_attachments=True,
    )
    assert any("convert_attachments" in w for w in result.warnings)

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ConversionCancelled):
        _eml(message, convert_attachments=True, cancel_event=cancel)


# ----------------------------------------------------------- 3. Apple iWork


def _iwork_package(previews: Dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Index/Document.iwa", b"\x00not a real iwa stream")
        for name, data in previews.items():
            z.writestr(name, data)
    return out.getvalue()


def test_iwork_filters_mimetypes_and_zip_requirement() -> None:
    assert lo._INPUT_FILTERS[".pages"] == "Apple Pages"
    assert lo._INPUT_FILTERS[".numbers"] == "Apple Numbers"
    assert lo._INPUT_FILTERS[".key"] == "Apple Keynote"
    assert lo._family(".pages") == "text"
    assert lo._family(".numbers") == "spreadsheet"
    assert lo._family(".key") == "presentation"
    assert lo._MIME_TO_EXTENSION["application/vnd.apple.keynote"] == ".key"

    converter = LibreOfficeConverter()
    package = io.BytesIO(_iwork_package({}))
    assert converter.accepts(package, StreamInfo(extension=".pages"))
    assert package.tell() == 0
    assert converter.accepts(
        package, StreamInfo(mimetype="application/vnd.apple.numbers")
    )
    # A PEM private key named .key is not an iWork package.
    pem = io.BytesIO(b"-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n")
    assert not converter.accepts(pem, StreamInfo(extension=".key"))
    assert pem.tell() == 0


def test_iwork_uses_explicit_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[List[str]] = []

    def fake_run(command: List[str], cwd: str, timeout: float, extension: str):
        seen.append(command)
        return 1, b"", b"import failed"

    monkeypatch.setattr(lo, "find_soffice", lambda: "/usr/bin/true")
    monkeypatch.setattr(lo, "_run", fake_run)
    monkeypatch.setattr(lo, "_sandbox_breaks", lambda *a: False)
    with pytest.raises(FileConversionException, match="could not convert"):
        LibreOfficeConverter().convert(
            io.BytesIO(_iwork_package({})), StreamInfo(extension=".numbers")
        )
    assert seen and "--infilter=Apple Numbers" in seen[0]


def test_iwork_preview_pdf_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lo, "find_soffice", lambda: None)
    pdf = (FILES / "test.pdf").read_bytes()
    package = _iwork_package(
        {"QuickLook/Preview.pdf": pdf, "QuickLook/Thumbnail.jpg": b"jpg"}
    )
    result = MarkitForge().convert_stream(io.BytesIO(package), file_extension=".pages")
    assert "While there is contemporaneous exploration" in result.markdown
    assert result.metadata["converted_via"] == "preview"
    assert result.metadata["source_format"] == "pages"
    assert result.metadata["preview_part"] == "QuickLook/Preview.pdf"
    assert "preview" in result.warnings[0] and "not installed" in result.warnings[0]

    # Used on its own, the converter falls back to the built-in PDF converter.
    alone = LibreOfficeConverter().convert(
        io.BytesIO(package), StreamInfo(extension=".pages")
    )
    assert "contemporaneous exploration" in alone.markdown


def test_iwork_preview_jpeg_and_no_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    monkeypatch.setattr(lo, "find_soffice", lambda: None)
    jpeg = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(jpeg, format="JPEG")
    package = _iwork_package({"preview.jpg": jpeg.getvalue()})
    result = LibreOfficeConverter().convert(
        io.BytesIO(package), StreamInfo(extension=".key")
    )
    assert result.metadata["preview_part"] == "preview.jpg"
    assert "preview" in result.warnings[0]

    with pytest.raises(MissingDependencyException):
        LibreOfficeConverter().convert(
            io.BytesIO(_iwork_package({})), StreamInfo(extension=".key")
        )


def test_iwork_package_is_checked_against_archive_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lo, "find_soffice", lambda: None)
    package = _iwork_package({"QuickLook/Preview.pdf": b"%PDF" + b"0" * 5000})
    with pytest.raises(ResourceLimitException):
        LibreOfficeConverter().convert(
            io.BytesIO(package),
            StreamInfo(extension=".pages"),
            max_archive_member_bytes=1000,
        )


_APPLE_SAMPLES = {
    ".pages": "/Applications/Pages Creator Studio.app/Contents/SharedSupport/"
    "Templates/08_Journal_Newsletter/Traditional.template",
    ".numbers": "/Applications/Numbers Creator Studio.app/Contents/SharedSupport/"
    "Templates/02_Invoice/Traditional.nmbtemplate",
}


@needs_soffice
@pytest.mark.parametrize("extension", sorted(_APPLE_SAMPLES))
def test_iwork_real_template_through_libreoffice(extension: str) -> None:
    # Apple's iWork apps ship their templates as ordinary iWork packages;
    # they are read in place (never copied into the repository).
    sample = Path(_APPLE_SAMPLES[extension])
    if not sample.is_file():
        pytest.skip("Apple iWork templates are not installed")
    result = MarkitForge().convert_stream(
        io.BytesIO(sample.read_bytes()), file_extension=extension
    )
    assert result.metadata["converted_via"] == "libreoffice"
    assert result.metadata["source_format"] == extension.lstrip(".")
    assert result.markdown.strip()


# ------------------------------------------------------ 4. DOCX page count


def _docx_bytes(pages: int, app_xml: Optional[str] = None) -> bytes:
    from docx import Document
    from docx.enum.text import WD_BREAK

    document = Document()
    for index in range(pages):
        paragraph = document.add_paragraph(f"Page {index + 1} text")
        if index < pages - 1:
            paragraph.add_run().add_break(WD_BREAK.PAGE)
    out = io.BytesIO()
    document.save(out)
    if app_xml is None:
        return out.getvalue()
    rebuilt = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(out.getvalue())) as src,
        zipfile.ZipFile(rebuilt, "w") as dst,
    ):
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "docProps/app.xml":
                data = app_xml.encode("utf-8")
            dst.writestr(info, data)
    return rebuilt.getvalue()


def _app_xml(pages: int, words: int = 40) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'extended-properties"><Pages>'
        f"{pages}</Pages><Words>{words}</Words><Characters>{words * 5}</Characters>"
        "</Properties>"
    )


def _docx(data: bytes, **kwargs: Any) -> Any:
    return DocxConverter().convert(
        io.BytesIO(data), StreamInfo(extension=".docx"), **kwargs
    )


def test_page_count_from_breaks_agreeing_with_app_xml() -> None:
    result = _docx(_docx_bytes(3, _app_xml(3)), docx_page_count="stored")
    assert result.metadata["page_count"] == 3 == len(result.pages)
    assert result.metadata["page_count_source"] == "breaks"
    assert result.warnings == []


def test_page_count_warns_when_app_xml_disagrees_with_breaks() -> None:
    result = _docx(_docx_bytes(3, _app_xml(5)), docx_page_count="stored")
    assert result.metadata["page_count"] == 3
    assert result.metadata["page_count_source"] == "breaks"
    assert len(result.warnings) == 1
    assert "app.xml records 5" in result.warnings[0]


def test_page_count_from_app_xml_when_no_breaks() -> None:
    result = _docx(_docx_bytes(1, _app_xml(4)), docx_page_count="stored")
    assert result.metadata["page_count"] == 4
    assert result.metadata["page_count_source"] == "app.xml"
    assert len(result.pages) == 1 and result.warnings == []


def test_page_count_ignores_template_statistics() -> None:
    # python-docx copies a template app.xml claiming 1 page and 0 words.
    result = _docx(_docx_bytes(3), docx_page_count="stored")
    assert result.metadata["page_count_source"] == "breaks"
    assert result.warnings == []


def test_page_count_libreoffice_missing_falls_back_to_app_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lo, "find_soffice", lambda: None)
    result = _docx(_docx_bytes(1, _app_xml(6)), docx_page_count="libreoffice")
    assert result.metadata["page_count"] == 6
    assert result.metadata["page_count_source"] == "app.xml"
    assert len(result.warnings) == 1 and "app.xml" in result.warnings[0]
    assert "page_count" not in _docx(_docx_bytes(1, _app_xml(6))).metadata


# ------------------------------------------------------ 5. image export


def _docx_with_images(images: List[bytes]) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("Before")
    for data in images:
        document.add_paragraph().add_run().add_picture(io.BytesIO(data))
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def test_docx_extract_images() -> None:
    data = _docx_with_images([LOGO, CHART, LOGO])
    plain = _docx(data)
    assert plain.assets == [] and "data:image/png;base64..." in plain.markdown
    assert _docx(data, extract_images=False).markdown == plain.markdown

    result = _docx(data, extract_images=True)
    assert [a.name for a in result.assets] == ["image1.png", "image2.png"]
    assert result.assets[0].data == LOGO and result.assets[1].data == CHART
    assert result.assets[0].mime_type == "image/png"
    assert result.markdown.count("](image1.png)") == 2
    assert result.markdown.count("](image2.png)") == 1
    assert "base64" not in result.markdown
    # extract_images wins over keep_data_uris.
    both = _docx(data, extract_images=True, keep_data_uris=True)
    assert "base64" not in both.markdown and len(both.assets) == 2


def _pptx_with_images(per_slide: List[List[bytes]]) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    for images in per_slide:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        for i, data in enumerate(images):
            picture = slide.shapes.add_picture(
                io.BytesIO(data), Inches(1 + i * 3), Inches(1)
            )
            picture._element._nvXxPr.cNvPr.set("descr", f"Alt [{i}]")
    out = io.BytesIO()
    presentation.save(out)
    return out.getvalue()


def _pptx(data: bytes, **kwargs: Any) -> Any:
    return PptxConverter().convert(
        io.BytesIO(data), StreamInfo(extension=".pptx"), **kwargs
    )


def test_pptx_extract_images() -> None:
    data = _pptx_with_images([[LOGO], [CHART, LOGO], [_png(3)]])
    plain = _pptx(data)
    assert plain.assets == []
    result = _pptx(data, extract_images=True)
    names = [a.name for a in result.assets]
    assert names == ["slide1_img1.png", "slide2_img1.png", "slide3_img1.png"]
    assert [a.page for a in result.assets] == [1, 2, 3]
    assert result.markdown.count("](slide1_img1.png)") == 2
    assert "![Alt 0](slide2_img1.png)" in result.markdown
    assert result.assets[0].description == "Alt 0"
    # Without the option the output is what it always was.
    assert plain.markdown == _pptx(data, extract_images=False).markdown
    assert "](Picture" in plain.markdown


def test_xlsx_extract_images() -> None:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as SheetImage

    workbook = Workbook()
    first = workbook.active
    first.title = "Data"
    first.append(["Item", "Count"])
    first.append(["Bolt", 3])
    first.add_image(SheetImage(io.BytesIO(LOGO)), "D2")
    first.add_image(SheetImage(io.BytesIO(LOGO)), "D20")
    second = workbook.create_sheet("More")
    second.append(["x"])
    second.add_image(SheetImage(io.BytesIO(CHART)), "C3")
    out = io.BytesIO()
    workbook.save(out)

    plain = XlsxConverter().convert(
        io.BytesIO(out.getvalue()), StreamInfo(extension=".xlsx")
    )
    result = XlsxConverter().convert(
        io.BytesIO(out.getvalue()), StreamInfo(extension=".xlsx"), extract_images=True
    )
    assert plain.assets == [] and "Images in this sheet" not in plain.markdown
    assert [(a.name, a.page) for a in result.assets] == [
        ("sheet1_img1.png", 1),
        ("sheet2_img1.png", 2),
    ]
    assert result.pages[0].markdown.endswith(
        "### Images in this sheet:\n\n![](sheet1_img1.png)"
    )
    assert "![](sheet2_img1.png)" in result.pages[1].markdown


def _epub(chapters: Dict[str, str], resources: Dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    manifest = "".join(
        f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>'
        for i, name in enumerate(chapters)
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">'
            "<metadata><dc:title>Roadmap Book</dc:title></metadata>"
            f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>",
        )
        for name, body in chapters.items():
            z.writestr(f"OEBPS/{name}", f"<html><body>{body}</body></html>")
        for name, data in resources.items():
            z.writestr(name, data)
    return out.getvalue()


def test_epub_extract_images_and_progress() -> None:
    data = _epub(
        {
            "text/one.xhtml": '<h1>One</h1><img src="../images/cover%20art.png" alt="Cover [x]"/>',
            "text/two.xhtml": '<p>Two</p><img src="../images/again.png"/>'
            '<img src="https://example.com/remote.png"/>'
            '<svg><image xlink:href="../images/chart.png"/></svg>',
        },
        {
            "OEBPS/images/cover art.png": LOGO,
            "OEBPS/images/again.png": LOGO,
            "OEBPS/images/chart.png": CHART,
        },
    )
    converter = EpubConverter()
    plain = converter.convert(io.BytesIO(data), StreamInfo(extension=".epub"))
    assert plain.assets == [] and "cover%20art.png" in plain.markdown

    events = _Events()
    result = converter.convert(
        io.BytesIO(data),
        StreamInfo(extension=".epub"),
        extract_images=True,
        progress=events,
    )
    assert [(a.name, a.page) for a in result.assets] == [
        ("cover_art.png", 1),
        ("chart.png", 2),
    ]
    assert "![Cover x](cover_art.png)" in result.markdown
    assert result.markdown.count("](cover_art.png)") == 2  # again.png: same bytes
    assert "![](chart.png)" in result.markdown
    assert "https://example.com/remote.png" in result.markdown
    assert events.by_stage("page") == [(1, 2), (2, 2)]


def _mhtml(images: List[tuple]) -> bytes:
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    root = MIMEMultipart("related")
    root["Subject"] = "Saved page"
    tags = "".join(f'<img src="{src}" alt="{alt}">' for src, _, alt, _ in images)
    page = MIMEText(f"<html><body><p>Page</p>{tags}</body></html>", "html")
    page["Content-Location"] = "https://example.com/page.html"
    root.attach(page)
    for src, location, _, data in images:
        part = MIMEImage(data, "png")
        if src.startswith("cid:"):
            part["Content-ID"] = f"<{src[4:]}>"
        if location:
            part["Content-Location"] = location
        root.attach(part)
    return root.as_bytes()


def test_mhtml_extract_images() -> None:
    data = _mhtml(
        [
            ("a/logo.png", "https://example.com/a/logo.png", "Logo", LOGO),
            ("cid:img2", None, "Copy", LOGO),
            ("b/logo.png", "https://example.com/b/logo.png", "Other", CHART),
        ]
    )
    plain = MhtmlConverter().convert(io.BytesIO(data), StreamInfo(extension=".mht"))
    assert len(plain.assets) == 3  # unchanged default: one asset per image part
    result = MhtmlConverter().convert(
        io.BytesIO(data), StreamInfo(extension=".mht"), extract_images=True
    )
    assert [a.name for a in result.assets] == ["logo.png", "logo_2.png"]
    assert "![Logo](logo.png)" in result.markdown
    assert "![Copy](logo.png)" in result.markdown
    assert "![Other](logo_2.png)" in result.markdown


def test_image_assets_names_are_safe_and_unique() -> None:
    assets = ImageAssets()
    assert assets.add(b"1", "image/png", name="../../etc/pass wd.png") == "pass_wd.png"
    assert assets.add(b"2", "image/png", name="pass wd.png") == "pass_wd_2.png"
    assert assets.add(b"1", "image/png", name="other.png") == "pass_wd.png"
    assert assets.add(b"3", "image/jpeg") == "image1.jpeg"
    assert assets.add(b"4", None, name="...") == "image.bin"
    assert len(assets) == 4


# ------------------------------------------------- 6. progress and cancel


def test_progress_per_unit() -> None:
    events = _Events()
    _docx(_docx_bytes(2), progress=events)
    assert events.by_stage("page") == [(0, 1), (1, 1)]

    events = _Events()
    _pptx(_pptx_with_images([[LOGO], [CHART], []]), progress=events)
    assert events.by_stage("slide") == [(1, 3), (2, 3), (3, 3)]

    events = _Events()
    XlsxConverter().convert(
        open(FILES / "mf_sheets.xlsx", "rb"),
        StreamInfo(extension=".xlsx"),
        progress=events,
    )
    sheets = events.by_stage("sheet")
    assert sheets and sheets[-1][0] == sheets[-1][1] == len(sheets)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.txt", "a")
        z.writestr("b.txt", "b")
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as nested:
            nested.writestr("c.txt", "c")
        z.writestr("n.zip", inner.getvalue())
    events = _Events()
    MarkitForge().convert_stream(archive, file_extension=".zip", progress=events)
    # Nested archives do not add events of their own.
    assert events.by_stage("entry") == [(1, 3), (2, 3), (3, 3)]


def test_cancellation_propagates_through_zip() -> None:
    cancel = threading.Event()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.docx", _docx_bytes(1))
        z.writestr("b.txt", "b")

    def stop(event: Any) -> None:
        cancel.set()

    with pytest.raises(ConversionCancelled):
        MarkitForge().convert_stream(
            archive, file_extension=".zip", progress=stop, cancel_event=cancel
        )

    # Cancelled inside a nested member: still raised, not a warning.
    cancel = threading.Event()
    cancel.set()
    for convert in (
        lambda: _docx(_docx_bytes(1), cancel_event=cancel),
        lambda: _pptx(_pptx_with_images([[LOGO]]), cancel_event=cancel),
    ):
        with pytest.raises(ConversionCancelled):
            convert()
