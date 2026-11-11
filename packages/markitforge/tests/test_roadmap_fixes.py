# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Image export safety, e-mail bodies, front matter, chunking and config."""

import base64
import io
import os
import shutil
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest
import yaml

from markitforge import MarkitForge, StreamInfo
from markitforge import _rag
from markitforge._base_converter import DocumentAsset, DocumentConverterResult
from markitforge._config import load_config
from markitforge._rag import chunk_result, front_matter
from markitforge.converter_utils import _archive_limits
from markitforge.converter_utils.pdf._visual import VisualBudget
from markitforge.converters import MhtmlConverter

FILES = Path(__file__).parent / "test_files"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PHP = b"<?php system($_GET['c']); ?>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _mhtml_with(parts) -> bytes:
    """An MHTML page with image parts: (location, content type, bytes)."""
    html = "<html><body><p>Page</p>" + "".join(
        f'<img src="{location}" alt="pic">' for location, _, _ in parts
    )
    out = [
        "MIME-Version: 1.0",
        'Content-Type: multipart/related; boundary="BND"; type="text/html"',
        "",
        "--BND",
        "Content-Type: text/html; charset=utf-8",
        "Content-Location: http://x/index.html",
        "",
        html + "</body></html>",
    ]
    for location, content_type, data in parts:
        out += [
            "--BND",
            f"Content-Type: {content_type}",
            "Content-Transfer-Encoding: base64",
            f"Content-Location: {location}",
            "",
            base64.b64encode(data).decode("ascii"),
        ]
    return ("\n".join(out) + "\n--BND--\n").encode()


def test_exported_images_take_their_extension_from_checked_content(tmp_path):
    data = _mhtml_with(
        [
            ("http://x/shell.php", "image/png", PNG),
            ("http://x/fake.png", "image/png", PHP),
            ("http://x/drawing.svg", "image/svg+xml", SVG),
        ]
    )
    result = MhtmlConverter().convert(
        io.BytesIO(data), StreamInfo(extension=".mht"), extract_images=True
    )
    assert [a.name for a in result.assets] == ["shell.png"]
    assert "![pic](shell.png)" in result.markdown
    assert len(result.warnings) == 2
    assert any("fake" in w and "image/png" in w for w in result.warnings)
    assert any("SVG" in w for w in result.warnings)
    result.save_assets(tmp_path / "out")
    assert os.listdir(tmp_path / "out") == ["shell.png"]

    # Without extract_images the assets keep the document's names; saving
    # them applies the same checks.
    plain = MhtmlConverter().convert(io.BytesIO(data), StreamInfo(extension=".mht"))
    plain.save_assets(tmp_path / "plain")
    assert os.listdir(tmp_path / "plain") == ["shell.png"]
    assert len(plain.warnings) == 2


def test_mhtml_extension_is_not_converted_as_email(tmp_path):
    source = FILES / "mf_page.mht"
    expected = MarkitForge().convert(str(source)).markdown
    assert expected.strip()
    copy = tmp_path / "page.mhtml"
    shutil.copyfile(source, copy)
    forge = MarkitForge()
    assert forge.convert(str(copy)).markdown == expected
    by_stream = forge.convert_stream(
        io.BytesIO(copy.read_bytes()), file_extension=".mhtml"
    )
    assert by_stream.markdown == expected


def test_eml_body_keeps_every_inline_text_part():
    message = EmailMessage()
    message["From"] = "a@example.com"
    message["Subject"] = "Mixed"
    message.set_content("The real message text.")
    message.add_attachment("<p>Legal <b>disclaimer</b></p>", subtype="html")
    message.add_attachment("Mailing list footer", subtype="plain")
    for part in list(message.iter_attachments()):
        del part["Content-Disposition"]
    message.add_attachment("kept as a file", subtype="plain", filename="notes.txt")
    result = MarkitForge().convert_stream(
        io.BytesIO(message.as_bytes()), file_extension=".eml"
    )
    md = result.markdown
    assert (
        md.index("The real message text.")
        < md.index("Legal **disclaimer**")
        < md.index("Mailing list footer")
    )
    assert result.metadata["attachments"] == ["notes.txt"]

    alternative = EmailMessage()
    alternative["Subject"] = "Alt"
    alternative.set_content("plain version")
    alternative.add_alternative("<p>html <i>version</i></p>", subtype="html")
    md = (
        MarkitForge()
        .convert_stream(io.BytesIO(alternative.as_bytes()), file_extension=".eml")
        .markdown
    )
    assert "html *version*" in md and "plain version" not in md


def test_front_matter_escapes_characters_yaml_rejects():
    title = "it\x92s here next \x7f￾"
    result = DocumentConverterResult("body", title=title, metadata={"k": ["a\x85b"]})
    block = front_matter(result)
    assert "\x92" not in block and " " not in block
    loaded = yaml.safe_load(block.strip().strip("-"))
    assert loaded["title"] == title
    assert loaded["k"] == ["a\x85b"]


def _four_chars_per_token(monkeypatch):
    monkeypatch.setattr(_rag, "estimate_tokens", lambda text: -(-len(text) // 4))


def test_chunk_overlap_never_exceeds_max_tokens(monkeypatch):
    _four_chars_per_token(monkeypatch)
    paragraphs = ["a" * 80, "b" * 80, "c" * 380]  # 20, 20 and 95 tokens
    result = DocumentConverterResult("\n\n".join(paragraphs))
    chunks = chunk_result(result, by="tokens", max_tokens=100, overlap=50)
    assert "".join(c.text for c in chunks).count("c" * 380) == 1
    assert all(c.tokens <= 100 for c in chunks)
    for previous, following in zip(chunks, chunks[1:]):
        assert previous.text not in following.text


def test_explicitly_named_missing_config_is_an_error(tmp_path, monkeypatch):
    missing = tmp_path / "typo.toml"
    with pytest.raises(ValueError, match="Config file not found"):
        load_config(missing)
    monkeypatch.setenv("MARKITFORGE_CONFIG", str(missing))
    with pytest.raises(ValueError, match="Config file not found"):
        load_config()
    run = subprocess.run(
        [sys.executable, "-m", "markitforge", str(FILES / "test.json")],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1 and run.stdout == ""
    assert run.stderr.strip() == f"Config file not found: {missing}"


def test_pdf_image_export_stops_at_max_export_bytes():
    budget = VisualBudget(max_export_bytes=100)
    warnings: list = []
    assert budget.can_keep_export(60, warnings)
    assert not budget.can_keep_export(60, warnings)
    assert not budget.can_export(warnings)
    assert len(warnings) == 1 and "max_export_bytes=100" in warnings[0]
    assert VisualBudget(max_export_bytes=0).can_keep_export(10**12, [])

    pdf = str(FILES / "mf_image.pdf")
    assert MarkitForge().convert(pdf, extract_images=True).assets
    capped = MarkitForge().convert(pdf, extract_images=True, max_export_bytes=1)
    assert capped.assets == []
    assert any("max_export_bytes" in w for w in capped.warnings)
    with pytest.raises(Exception, match="max_export_bytes"):
        MarkitForge().convert(pdf, extract_images=True, max_export_bytes=-1)


@pytest.mark.parametrize("name", ["limits", "defaults", "llm"])
def test_config_section_must_be_a_table(tmp_path, name):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"{name} = 5\n")
    with pytest.raises(ValueError, match=rf"\[{name}\] must be a table"):
        load_config(cfg)


def test_eml_attachment_size_needs_no_decoding():
    message = EmailMessage()
    message["Subject"] = "Sizes"
    message.set_content("body")
    for size in (1000, 1001, 1002):
        message.add_attachment(
            os.urandom(size),
            maintype="application",
            subtype="octet-stream",
            filename=f"blob{size}.bin",
        )
    md = (
        MarkitForge()
        .convert_stream(io.BytesIO(message.as_bytes()), file_extension=".eml")
        .markdown
    )
    for size in (1000, 1001, 1002):
        assert f"| blob{size}.bin | application/octet-stream | {size} bytes |" in md


def test_hard_split_respects_max_tokens_for_dense_scripts(monkeypatch):
    # One token per CJK character, four characters per token otherwise.
    def estimate(text: str) -> int:
        cjk = sum(1 for ch in text if ch >= "　")
        return cjk + -(-(len(text) - cjk) // 4)

    monkeypatch.setattr(_rag, "estimate_tokens", estimate)
    text = "漢" * 1000
    chunks = chunk_result(DocumentConverterResult(text), by="tokens", max_tokens=64)
    assert "".join(c.text for c in chunks) == text
    assert all(0 < c.tokens <= 64 for c in chunks)


def test_one_asset_name_helper_with_a_length_cap(tmp_path):
    assert _rag.safe_asset_name is _archive_limits.safe_asset_name
    result = DocumentConverterResult(
        "x",
        assets=[
            DocumentAsset("../" + "n" * 300 + ".png", "image/png", PNG),
            DocumentAsset("...", "image/png", PNG + b"2"),
        ],
    )
    mapping = result.save_assets(tmp_path / "out")
    names = sorted(os.listdir(tmp_path / "out"))
    assert names == ["asset.png", "n" * 100 + ".png"]
    assert sorted(mapping.values()) == names
