# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for MarkitForge CLI additions: batch mode, --json, --list-formats, core limits."""

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from markitforge import MarkitForge, ResourceLimitException
from markitforge._batch import collect_items

FILES = Path(__file__).parent / "test_files"


def run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "markitforge", *map(str, args)],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_list_formats_text_and_json():
    r = run("--list-formats")
    assert r.returncode == 0 and "PDF" in r.stdout
    rows = json.loads(run("--list-formats", "--json").stdout)
    assert {"name", "extensions", "available", "missing"} <= set(rows[0])


def test_json_output_has_structure():
    r = run(FILES / "test.docx", "--json")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["markdown"].strip()
    assert {"metadata", "pages", "assets", "warnings", "title"} <= set(data)


def test_batch_folder_with_glob_and_recursion(tmp_path):
    src = tmp_path / "in"
    (src / "sub").mkdir(parents=True)
    shutil.copy(FILES / "test.docx", src / "a.docx")
    shutil.copy(FILES / "test.pptx", src / "sub" / "b.pptx")
    shutil.copy(FILES / "test.xlsx", src / "c.xlsx")
    out = tmp_path / "out"

    r = run(src, "-r", "--glob", "*.*x", "-o", out, "-j", "2")
    assert r.returncode == 0, r.stderr
    assert (out / "a.md").exists()
    assert (out / "sub" / "b.md").exists()
    assert (out / "c.md").exists()
    assert "Converted 3 of 3" in r.stderr

    # Second run skips existing outputs unless --overwrite
    r2 = run(src, "-r", "-o", out)
    assert "skip" in r2.stderr


def test_batch_reports_failures_and_keeps_going(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    shutil.copy(FILES / "test.docx", src / "good.docx")
    shutil.copy(FILES / "random.bin", src / "bad.bin")
    r = run(src, "-o", tmp_path / "out", "-j", "1")
    assert r.returncode == 1
    assert "FAIL" in r.stderr and "good.docx" in r.stderr
    assert (tmp_path / "out" / "good.md").exists()


def test_batch_multiple_files_json(tmp_path):
    r = run(FILES / "test.docx", FILES / "test.pptx", "--json", "-o", tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "test.docx.json").read_text())["markdown"]
    assert json.loads((tmp_path / "test.pptx.json").read_text())["markdown"]


def test_collect_items_avoids_target_collisions(tmp_path):
    (tmp_path / "x.pdf").write_bytes(b"%PDF")
    (tmp_path / "x.docx").write_bytes(b"PK")
    items = collect_items([str(tmp_path)], output_dir=str(tmp_path / "o"))
    targets = [i.target.name for i in items]
    assert len(set(targets)) == 2


def test_missing_input_is_an_error(tmp_path):
    r = run(tmp_path / "nope.pdf", tmp_path / "nope2.pdf")
    assert r.returncode == 2 and "No such file" in r.stderr


def test_llm_flags_need_model():
    r = run(FILES / "test.docx", "--describe-images")
    assert r.returncode == 1 and "--llm-model" in r.stderr


def test_no_magika_still_converts():
    md = MarkitForge(use_magika=False)
    assert md._magika is None
    assert "AutoGen" in md.convert(FILES / "test.docx").markdown
    text = md.convert_stream(
        io.BytesIO(
            "Schöne Grüße aus München, wir freuen uns über Ihre Nachricht.".encode(
                "latin-1"
            )
        ),
        file_extension=".txt",
    ).markdown
    assert "Grüße aus München" in text


class _FakeResponse:
    def __init__(self, body: bytes, length: bool):
        self._body = body
        self.headers = {"content-type": "text/plain"}
        if length:
            self.headers["content-length"] = str(len(body))
        self.url = "https://example.com/a.txt"

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), 10):
            yield self._body[i : i + 10]

    def close(self):
        pass


@pytest.mark.parametrize("declared", [True, False])
def test_download_size_limit(declared):
    md = MarkitForge(max_download_bytes=50)
    with pytest.raises(ResourceLimitException):
        md.convert_response(_FakeResponse(b"x" * 200, declared))
    assert md.convert_response(_FakeResponse(b"ok", declared)).markdown == "ok"


def test_fetch_uses_timeout():
    seen = {}

    class Session:
        headers: dict = {}

        def get(self, url, **kw):
            seen.update(kw)
            r = _FakeResponse(b"hi", True)
            r.raise_for_status = lambda: None
            return r

    md = MarkitForge(requests_session=Session(), fetch_timeout=(1, 2))
    md.convert("https://example.com/a.txt")
    assert seen["timeout"] == (1, 2)


# ---- hardening regression tests ----------------------------------------------


def test_page_range_is_clamped_and_bounded():
    import time

    from markitforge import parse_page_range

    start = time.monotonic()
    assert parse_page_range("1-999999999999", 3) == [1, 2, 3]
    assert time.monotonic() - start < 0.5
    with pytest.raises(ValueError):
        parse_page_range(",".join(["1"] * 500), 3)
    with pytest.raises(ValueError):
        parse_page_range("x-y", 3)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://10.1.2.3/",
        "http://0.0.0.0/",
        "ftp://example.com/",
    ],
)
def test_ssrf_guard_blocks_internal(url):
    from markitforge import BlockedURLException, check_public_url

    with pytest.raises(BlockedURLException):
        check_public_url(url)


def test_ssrf_guard_blocks_mixed_records():
    from markitforge import BlockedURLException, check_public_url

    with pytest.raises(BlockedURLException):
        check_public_url(
            "http://x.example/", resolver=lambda h, p: ["93.184.216.34", "10.0.0.5"]
        )
    check_public_url("http://x.example/", resolver=lambda h, p: ["93.184.216.34"])


class _RedirectSession:
    """Fake session: first hop redirects to an internal address."""

    headers: dict = {}

    def __init__(self, location):
        self.location = location
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if len(self.calls) == 1:
            r = _FakeResponse(b"", True)
            r.is_redirect = True
            r.headers["location"] = self.location
            r.raise_for_status = lambda: None
            return r
        r = _FakeResponse(b"secret", True)
        r.url = url
        r.raise_for_status = lambda: None
        return r


def test_redirects_are_rechecked_against_ssrf_guard(monkeypatch):
    from markitforge import BlockedURLException
    import markitforge._markitforge as core

    seen = []

    def fake_check(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise BlockedURLException("internal")

    monkeypatch.setattr(core, "check_public_url", fake_check)
    sess = _RedirectSession("http://127.0.0.1:9/admin")
    md = MarkitForge(requests_session=sess, block_private_networks=True)
    with pytest.raises(BlockedURLException):
        md.convert("https://public.example/a.txt")
    assert seen == ["https://public.example/a.txt", "http://127.0.0.1:9/admin"]
    assert len(sess.calls) == 1  # never fetched the internal hop
    assert sess.calls[0][1]["allow_redirects"] is False


def test_redirect_to_file_scheme_refused():
    sess = _RedirectSession("file:///etc/passwd")
    with pytest.raises(ValueError):
        MarkitForge(requests_session=sess).convert("https://public.example/a.txt")


def test_download_deadline():
    class Slow(_FakeResponse):
        def iter_content(self, chunk_size):
            import time

            for _ in range(5):
                time.sleep(0.3)
                yield b"x"

    md = MarkitForge(fetch_deadline=0.5)
    with pytest.raises(ResourceLimitException):
        md.convert_response(Slow(b"", False))
