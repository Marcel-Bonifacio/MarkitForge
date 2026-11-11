# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Config file, progress/cancel hooks, RAG output helpers and their CLI flags."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from markitforge import (
    ConversionCancelled,
    DocumentAsset,
    DocumentConverter,
    DocumentConverterResult,
    DocumentSection,
    MarkitForge,
    StreamInfo,
    check_cancelled,
    report_progress,
)
from markitforge._config import Config, load_config
from markitforge._rag import chunk_result, estimate_tokens, rewrite_asset_links

FILES = Path(__file__).parent / "test_files"
PNG = b"\x89PNG\r\n\x1a\n"

# An empty config file, so the CLI never reads the user's own.
EMPTY_CONFIG = Path(tempfile.mkdtemp(prefix="mf-config-")) / "config.toml"
EMPTY_CONFIG.write_text("")


def run(*args, env=None):
    e = dict(os.environ)
    e["MARKITFORGE_CONFIG"] = str(EMPTY_CONFIG)
    e.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "markitforge", *map(str, args)],
        capture_output=True,
        text=True,
        env=e,
    )


# ---------------------------------------------------------------- config ---


def test_config_sections_and_unknown_keys(tmp_path, caplog):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        "[defaults]\npage_markers = true\nnope = 1\n"
        "[limits]\nmax_pdf_pages = 3\nblock_private_networks = true\n"
        '[llm]\nmodel = "m"\nbase_url = "http://localhost:11434/v1"\n'
    )
    c = load_config(cfg)
    assert c.defaults == {"page_markers": True}
    assert c.limits == {"max_pdf_pages": 3}
    assert c.instance == {"block_private_networks": True}
    assert c.conversion_kwargs() == {"page_markers": True, "max_pdf_pages": 3}
    assert "nope" in caplog.text


def test_missing_config_is_empty(tmp_path, monkeypatch):
    # Only the default location may be missing.
    monkeypatch.delenv("MARKITFORGE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    c = load_config()
    assert c.path is None and c.conversion_kwargs() == {}


def test_bad_config_raises(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[defaults\n")
    with pytest.raises(ValueError):
        load_config(cfg)


def test_config_never_reads_key_from_file(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[llm]\nmodel = "m"\napi_key = "sk-secret"\n')
    c = load_config(cfg)
    assert "api_key" not in c.llm


def test_cli_uses_config_defaults(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[defaults]\npage_markers = true\n")
    r = run(FILES / "test.pptx", env={"MARKITFORGE_CONFIG": str(cfg)})
    assert r.returncode == 0, r.stderr
    assert "<!-- slide 1 -->" in r.stdout
    r2 = run(FILES / "test.pptx", "--no-config", env={"MARKITFORGE_CONFIG": str(cfg)})
    assert "<!-- slide 1 -->" not in r2.stdout


def test_cli_config_limit_applies(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[limits]\nmax_pdf_pages = 0\n")  # 0 = unlimited, must still work
    assert run(FILES / "test.pdf", "--config", cfg).returncode == 0


# ------------------------------------------------------- progress/cancel ---


def test_progress_callback_errors_are_ignored():
    def boom(_event):
        raise RuntimeError("ui broke")

    report_progress({"progress": boom}, "page", 1, 2)  # must not raise


class _SlowConverter(DocumentConverter):
    def accepts(self, file_stream, stream_info, **kwargs):
        return (stream_info.extension or "") == ".slow"

    def convert(self, file_stream, stream_info, **kwargs):
        for i in range(1, 6):
            check_cancelled(kwargs)
            report_progress(kwargs, "page", i, 5)
            if i == 2 and kwargs.get("cancel_event") is not None:
                kwargs["cancel_event"].set()
        return DocumentConverterResult("done")


def test_cancel_propagates_through_markitforge():
    md = MarkitForge()
    md.register_converter(_SlowConverter())
    events = []
    with pytest.raises(ConversionCancelled):
        md.convert_stream(
            io.BytesIO(b"x"),
            stream_info=StreamInfo(extension=".slow"),
            progress=events.append,
            cancel_event=threading.Event(),
        )
    assert [e.current for e in events] == [1, 2]
    assert events[0].total == 5 and events[0].fraction == pytest.approx(0.2)


# ------------------------------------------------------------------- RAG ---


def _doc():
    return DocumentConverterResult(
        "x",
        title='Report: "Q1"',
        metadata={"author": "no", "tags": ["a", "b"], "nested": {"k": 1}},
        pages=[
            DocumentSection(
                1, "# Intro\n\nHello world.\n\n## Detail\n\n" + "word " * 500
            ),
            DocumentSection(2, "## End\n\n```\ncode\n\n# not a heading\n```\n\nBye"),
        ],
    )


def test_front_matter_is_quoted_yaml():
    fm = _doc().front_matter()
    assert fm.startswith("---\n") and fm.endswith("---\n")
    assert 'title: "Report: \\"Q1\\""' in fm
    assert 'author: "no"' in fm  # stays a string, not YAML false
    assert 'tags:\n  - "a"' in fm
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(fm.strip("-\n"))
    assert data["title"] == 'Report: "Q1"' and data["author"] == "no"


def test_chunks_follow_headings_pages_and_size():
    chunks = _doc().chunks(max_tokens=200)
    assert all(c.tokens <= 205 for c in chunks)
    assert chunks[0].heading_path == ["Intro"] and chunks[0].page_start == 1
    assert not any(
        c.text.strip().startswith("## Detail") and c.tokens < 10 for c in chunks
    )
    last = chunks[-1]
    assert last.heading_path == ["Intro", "End"] and last.page_start == 2
    assert "# not a heading" in last.text  # fenced code is kept whole


def test_chunk_overlap_and_token_mode():
    doc = DocumentConverterResult(
        "\n\n".join(f"Paragraph {i} " + "x " * 40 for i in range(30))
    )
    chunks = chunk_result(doc, by="tokens", max_tokens=100, overlap=30)
    assert len(chunks) > 3
    assert chunks[1].text.split("\n\n")[0].strip() in chunks[0].text  # repeated tail


def test_chunk_validation():
    with pytest.raises(ValueError):
        _doc().chunks(by="nope")
    with pytest.raises(ValueError):
        _doc().chunks(max_tokens=2)


def test_estimate_tokens_monotone():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


def test_save_assets_safe_names_and_links(tmp_path):
    r = DocumentConverterResult(
        "![a](img.png) ![b](../../evil.png) ![c](other.png)",
        pages=[DocumentSection(1, "![a](img.png)")],
        assets=[
            DocumentAsset("img.png", "image/png", PNG + b"1"),
            DocumentAsset("../../evil.png", "image/png", PNG + b"2"),
            DocumentAsset("nodata.png", "image/png", None),
        ],
    )
    mapping = r.save_assets(tmp_path / "out", link_prefix="doc_assets")
    assert sorted(os.listdir(tmp_path / "out")) == ["evil.png", "img.png"]
    assert not (tmp_path / "evil.png").exists()
    assert "![a](doc_assets/img.png)" in r.markdown
    assert "![b](doc_assets/evil.png)" in r.markdown
    assert "![c](other.png)" in r.markdown  # unknown names untouched
    assert r.pages[0].markdown == "![a](doc_assets/img.png)"
    assert mapping["img.png"] == "doc_assets/img.png"


def test_save_assets_refuses_symlink_target(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "img.png").symlink_to(tmp_path / "victim")
    r = DocumentConverterResult(
        "x", assets=[DocumentAsset("img.png", "image/png", PNG + b"1")]
    )
    with pytest.raises(ValueError):
        r.save_assets(out)
    assert not (tmp_path / "victim").exists()


def test_rewrite_asset_links_keeps_titles():
    out = rewrite_asset_links('![x](a.png "T")', {"a.png": "d/a.png"})
    assert out == '![x](d/a.png "T")'


def test_cli_chunks_jsonl_and_front_matter():
    r = run(FILES / "test.pdf", "--chunks", "heading", "--chunk-size", "200")
    assert r.returncode == 0, r.stderr
    rows = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
    assert rows and {"text", "tokens", "heading_path", "page_start", "source"} <= set(
        rows[0]
    )
    r2 = run(FILES / "test.docx", "--front-matter")
    assert r2.stdout.startswith("---\n")


def test_cli_batch_chunks_suffix(tmp_path):
    r = run(
        FILES / "test.docx", FILES / "test.pptx", "--chunks", "tokens", "-o", tmp_path
    )
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "test.docx.jsonl",
        "test.pptx.jsonl",
    ]


def test_cli_rejects_tiny_chunk_size():
    r = run(FILES / "test.docx", "--chunks", "tokens", "--chunk-size", "4")
    assert r.returncode == 1 and "chunk-size" in r.stderr


def test_cli_limit_flags_reach_converters(tmp_path):
    r = run(FILES / "test.pdf", "--max-pages", "0")
    assert r.returncode == 0, r.stderr
    import zipfile

    bomb = tmp_path / "b.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.txt", "a" * (3 * 1024 * 1024))
    r = run(bomb, "--max-archive-mb", "1")
    assert "a.txt" in (r.stdout + r.stderr)  # skipped with a warning, not converted
    assert "aaaaaaaa" not in r.stdout


def test_cli_block_private_urls():
    r = run("http://127.0.0.1:9/x.txt", "--block-private-urls")
    assert r.returncode != 0
    assert "private" in (r.stderr + r.stdout).lower()


def test_config_object_default():
    assert Config().llm_client() is None
