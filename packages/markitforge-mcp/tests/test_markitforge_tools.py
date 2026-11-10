# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from markitforge_mcp import __main__ as server

FILES = Path(__file__).parents[2] / "markitforge" / "tests" / "test_files"


def call(tool: str, **args):
    result = asyncio.run(server.mcp.call_tool(tool, args))
    # SDK 2.x returns a CallToolResult; older shapes were (blocks, structured)
    blocks = getattr(result, "content", None)
    if blocks is None:
        blocks = result[0] if isinstance(result, tuple) else result
    return blocks[0].text


@pytest.fixture(autouse=True)
def reset_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKITFORGE_MCP_OUTPUT_DIR", str(tmp_path / "out"))
    server.configure_file_access(enabled=True, roots=None)
    yield
    server.configure_file_access(enabled=True, roots=None)


def test_tools_are_listed():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {
        "convert_to_markdown",
        "convert_to_file",
        "get_document_info",
        "list_supported_formats",
    } <= names


def test_max_chars_cuts_output():
    text = call(
        "convert_to_markdown", uri=(FILES / "test.docx").as_uri(), max_chars=100
    )
    assert text.startswith(text[:100])
    assert "more characters cut" in text


def test_convert_to_file_returns_summary_not_content(tmp_path):
    summary = json.loads(call("convert_to_file", uri=(FILES / "test.docx").as_uri()))
    path = Path(summary["path"])
    assert path.exists() and path.parent == tmp_path / "out"
    assert path.read_text(encoding="utf-8").strip()
    assert summary["characters"] == len(path.read_text(encoding="utf-8"))
    assert len(summary["preview"]) <= 500


def test_convert_to_file_sanitizes_filename(tmp_path):
    summary = json.loads(
        call(
            "convert_to_file",
            uri=(FILES / "test.docx").as_uri(),
            filename="../../etc/evil name",
        )
    )
    path = Path(summary["path"])
    assert path.parent == tmp_path / "out"
    assert path.name == "evil_name.md"


def test_get_document_info_has_no_body():
    info = json.loads(call("get_document_info", uri=(FILES / "test.pptx").as_uri()))
    assert "markdown" not in info
    assert info["characters"] > 0


def test_list_supported_formats():
    rows = json.loads(call("list_supported_formats"))
    assert any(r["name"] == "PDF" for r in rows)


def test_file_uris_can_be_disabled():
    server.configure_file_access(enabled=False, roots=None)
    with pytest.raises(ToolError, match="disabled"):
        call("convert_to_markdown", uri=(FILES / "test.docx").as_uri())


def test_file_roots_allow_inside_and_block_outside(tmp_path):
    server.configure_file_access(enabled=True, roots=[str(FILES)])
    assert call("convert_to_markdown", uri=(FILES / "test.docx").as_uri())
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with pytest.raises(ToolError, match="outside"):
        call("convert_to_markdown", uri=outside.as_uri())


def test_file_roots_block_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (root / "link.txt").symlink_to(secret)
    server.configure_file_access(enabled=True, roots=[str(root)])
    with pytest.raises(ToolError, match="outside"):
        call("convert_to_markdown", uri=(root / "link.txt").as_uri())


def test_data_uris_work_when_files_disabled():
    server.configure_file_access(enabled=False, roots=None)
    assert "hello" in call("convert_to_markdown", uri="data:text/plain,hello")


# ---- hardening regression tests ----------------------------------------------


def test_convert_to_file_does_not_follow_planted_symlink(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim.txt"
    (out / "test.md").symlink_to(victim)  # dangling link where we'd write
    summary = json.loads(call("convert_to_file", uri=(FILES / "test.docx").as_uri()))
    assert not victim.exists()
    assert Path(summary["path"]).name == "test-2.md"
    assert not Path(summary["path"]).is_symlink()


def test_convert_to_file_numbering_keeps_stem(tmp_path):
    names = [
        Path(
            json.loads(
                call(
                    "convert_to_file",
                    uri=(FILES / "test.docx").as_uri(),
                    filename="my-report.md",
                )
            )["path"]
        ).name
        for _ in range(3)
    ]
    assert names == ["my-report.md", "my-report-2.md", "my-report-3.md"]


def test_convert_to_file_is_private(tmp_path):
    import os
    import stat

    p = Path(
        json.loads(call("convert_to_file", uri=(FILES / "test.docx").as_uri()))["path"]
    )
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_huge_pages_spec_is_rejected_or_fast():
    import time

    start = time.monotonic()
    call(
        "convert_to_markdown",
        uri=(FILES / "test.pptx").as_uri(),
        pages="1-999999999999",
    )
    assert time.monotonic() - start < 10


def test_private_urls_blocked_when_enabled(monkeypatch):
    monkeypatch.setattr(server, "_block_private_urls", True)
    with pytest.raises(ToolError):
        call("convert_to_markdown", uri="http://127.0.0.1:9/")
