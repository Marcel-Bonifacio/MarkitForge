# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest
from starlette.testclient import TestClient

from markitforge import DocumentConverterResult, DocumentSection, MarkitForge
from markitforge_gui import create_app, render_markdown
from markitforge_gui._app import (
    CLIENT_HEADER,
    content_disposition,
    parse_options,
    OptionsError,
)

TEST_FILES = (
    Path(__file__).resolve().parents[2] / "markitforge" / "tests" / "test_files"
)
HEADERS = {CLIENT_HEADER: "1"}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


def upload(
    client: TestClient,
    name: str,
    data: Optional[bytes] = None,
    options: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    if data is None:
        data = (TEST_FILES / name).read_bytes()
    return client.post(
        "/api/convert",
        files={"file": (name, data, "application/octet-stream")},
        data={"options": json.dumps(options or {})},
        headers=HEADERS if headers is None else headers,
    )


# ---------------------------------------------------------------- pages


def test_index_served_with_csp(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "MarkitForge" in res.text
    csp = res.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    for asset in ("/static/app.js", "/static/app.css", "/static/favicon.svg"):
        assert client.get(asset).status_code == 200


def test_info(client: TestClient) -> None:
    data = client.get("/api/info").json()
    assert data["max_upload_mb"] == 200
    assert data["max_upload_bytes"] == 200 * 1024 * 1024


def test_foreign_host_rejected() -> None:
    with TestClient(create_app(), base_url="http://evil.example:8765") as c:
        assert c.get("/").status_code == 400


# ---------------------------------------------------------------- convert


@pytest.mark.parametrize("name", ["test.docx", "test.xlsx", "test.pdf"])
def test_convert_upload_shape(client: TestClient, name: str) -> None:
    res = upload(client, name)
    assert res.status_code == 200, res.text
    data = res.json()
    for key in (
        "id",
        "filename",
        "markdown",
        "html",
        "title",
        "metadata",
        "pages",
        "warnings",
        "assets",
    ):
        assert key in data
    assert data["filename"] == name
    assert data["md_filename"] == Path(name).stem + ".md"
    assert isinstance(data["markdown"], str) and data["markdown"].strip()
    assert isinstance(data["html"], str) and data["html"].strip()
    assert isinstance(data["metadata"], dict)
    assert isinstance(data["warnings"], list)
    for page in data["pages"]:
        assert set(page) == {"index", "kind", "title"}
    for asset in data["assets"]:
        assert set(asset) == {"name", "mime_type", "size", "description"}

    dl = client.get(f"/api/download/{data['id']}")
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("text/markdown")
    assert "attachment" in dl.headers["content-disposition"]
    assert Path(name).stem + ".md" in dl.headers["content-disposition"]
    assert dl.text == data["markdown"]


def test_xlsx_content_and_options(client: TestClient) -> None:
    res = upload(client, "test.xlsx", options={"page_markers": True})
    assert res.status_code == 200
    data = res.json()
    assert "<table>" in data["html"]
    assert data["pages"], "xlsx should report one section per sheet"
    assert data["pages"][0]["kind"] == "sheet"


def test_temp_file_removed(client: TestClient) -> None:
    before = set(Path(tempfile.gettempdir()).glob("markitforge-gui-*"))
    assert upload(client, "test.docx").status_code == 200
    after = set(Path(tempfile.gettempdir()).glob("markitforge-gui-*"))
    assert after - before == set()


def test_unsupported_file_friendly_error(client: TestClient) -> None:
    res = upload(client, "mystery.zzzq", data=bytes(range(256)) * 64)
    assert res.status_code == 415, res.text
    body = res.json()
    assert body["error"] == "This file type is not supported yet."


def test_missing_file_field(client: TestClient) -> None:
    res = client.post(
        "/api/convert", data={"options": "{}"}, headers=HEADERS, files={"x": ("a", b"")}
    )
    assert res.status_code == 400
    assert "No file" in res.json()["error"]


def test_bad_options_rejected(client: TestClient) -> None:
    res = upload(client, "test.docx", options={"table_format": "fancy"})
    assert res.status_code == 400
    assert "table_format" in res.json()["error"]


def test_upload_limit_enforced() -> None:
    app = create_app(max_upload_mb=0.01)  # ~10 KB (+1 MB framing allowance)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        big = b"a" * (2 * 1024 * 1024)
        res = upload(c, "big.txt", data=big)
        assert res.status_code == 413
        assert "too large" in res.json()["error"]


def test_upload_limit_enforced_without_content_length() -> None:
    app = create_app(max_upload_mb=0.01)
    with TestClient(app, base_url="http://127.0.0.1") as c:

        def chunks() -> Iterator[bytes]:
            for _ in range(40):
                yield b"x" * 65536

        res = c.post(
            "/api/render",
            content=chunks(),
            headers={**HEADERS, "content-type": "application/json"},
        )
        assert res.status_code == 413


def test_missing_dependency_shows_hint(client: TestClient) -> None:
    from markitforge import MissingDependencyException

    class Broken(MarkitForge):
        def convert_local(self, *args: Any, **kwargs: Any) -> DocumentConverterResult:
            raise MissingDependencyException("pip install 'markitforge[pdf]'")

    app = create_app(converter_factory=Broken)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        res = upload(c, "test.pdf")
    assert res.status_code == 422
    body = res.json()
    assert "extra component" in body["error"]
    assert "pip install" in body["hint"]


# ---------------------------------------------------------------- CSRF-ish


def test_post_requires_client_header(client: TestClient) -> None:
    res = upload(client, "test.docx", headers={})
    assert res.status_code == 403


def test_post_rejects_foreign_origin(client: TestClient) -> None:
    res = client.post(
        "/api/render",
        json={"markdown": "hi"},
        headers={**HEADERS, "Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    ok = client.post(
        "/api/render",
        json={"markdown": "hi"},
        headers={**HEADERS, "Origin": "http://127.0.0.1:8765"},
    )
    assert ok.status_code == 200


# ---------------------------------------------------------------- render


def test_render_endpoint(client: TestClient) -> None:
    res = client.post(
        "/api/render",
        json={"markdown": "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"},
        headers=HEADERS,
    )
    assert res.status_code == 200
    html = res.json()["html"]
    assert "<h1>Title</h1>" in html
    assert "<table>" in html and "<td>1</td>" in html


def test_render_page_selection(client: TestClient) -> None:
    data = upload(client, "test.xlsx").json()
    assert len(data["pages"]) >= 2
    res = client.post(
        "/api/render", json={"id": data["id"], "pages": "2"}, headers=HEADERS
    )
    assert res.status_code == 200, res.text
    selected = res.json()["markdown"]
    assert "sheet 2" in selected
    assert "sheet 1 -->" not in selected

    bad = client.post(
        "/api/render", json={"id": data["id"], "pages": "abc"}, headers=HEADERS
    )
    assert bad.status_code == 400
    assert "page range" in bad.json()["error"]

    dl = client.get(f"/api/download/{data['id']}?pages=2")
    assert dl.status_code == 200
    assert dl.text == selected


def test_render_unknown_id(client: TestClient) -> None:
    res = client.post("/api/render", json={"id": "nope"}, headers=HEADERS)
    assert res.status_code == 404


# ---------------------------------------------------------------- sanitizer


@pytest.mark.parametrize(
    "markdown, forbidden",
    [
        ("hello\n\n<script>alert(1)</script>", ["<script", "alert(1)"]),
        ('<img src="x.png" onerror="alert(1)">', ["onerror", "alert"]),
        ('<a href="javascript:alert(1)">x</a>', ["javascript:"]),
        ('<a href="JaVaScRiPt:alert(1)">x</a>', ["javascript", "JaVaScRiPt"]),
        ("[x](javascript:alert(1))", ['href="javascript']),
        ('<a href="data:text/html,<script>">x</a>', ["data:text"]),
        ('<img src="https://tracker.example/p.gif">', ["tracker.example"]),
        ('<img src="//tracker.example/p.gif">', ["tracker.example"]),
        ('<svg onload="alert(1)"></svg>', ["onload", "<svg"]),
        ('<iframe src="https://x"></iframe>', ["<iframe"]),
        ("<style>body{display:none}</style>", ["<style", "display:none"]),
        ('<p style="position:fixed">x</p>', ["style="]),
        ('<form action="/x"><input name="a"></form>', ["<form", "<input"]),
    ],
)
def test_sanitizer_strips(markdown: str, forbidden: list) -> None:
    html = render_markdown(markdown)
    for bad in forbidden:
        assert bad not in html


def test_sanitizer_keeps_safe_content() -> None:
    html = render_markdown(
        '<table><tr><td colspan="2" rowspan="3">a</td></tr></table>\n\n'
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="pic">\n\n'
        '<img src="media/a.png">\n\n'
        "[site](https://example.com) and `code`\n\n"
        "```\nprint(1)\n```\n\n"
        "<!-- page 2 -->\nNext"
    )
    assert 'colspan="2"' in html and 'rowspan="3"' in html
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in html
    # Relative image paths are dropped: they could make the page request /api/...
    assert "media/a.png" not in html
    assert 'href="https://example.com"' in html
    assert "noopener" in html
    assert "<pre><code>print(1)" in html
    assert 'class="mf-page-marker"' in html and "Page 2" in html


def test_render_endpoint_sanitizes(client: TestClient) -> None:
    res = client.post(
        "/api/render",
        json={"markdown": "<script>alert(1)</script><img src=x onerror=alert(1)>"},
        headers=HEADERS,
    )
    html = res.json()["html"]
    assert "<script" not in html and "onerror" not in html


def test_converted_html_is_sanitized() -> None:
    class Evil(MarkitForge):
        def convert_local(self, *args: Any, **kwargs: Any) -> DocumentConverterResult:
            return DocumentConverterResult(
                markdown='# Doc\n\n<script>alert(1)</script>\n\n<a href="javascript:x">l</a>',
                title="Doc",
                pages=[DocumentSection(index=1, markdown="# Doc")],
                warnings=["Something odd"],
            )

    with TestClient(
        create_app(converter_factory=Evil), base_url="http://127.0.0.1"
    ) as c:
        data = upload(c, "test.docx").json()
    assert "<script" not in data["html"]
    assert "javascript:" not in data["html"]
    assert data["warnings"] == ["Something odd"]
    assert data["pages"] == [{"index": 1, "kind": "page", "title": None}]


# ---------------------------------------------------------------- zip / store


def test_zip_download(client: TestClient) -> None:
    a = upload(client, "test.docx").json()
    b = upload(client, "test.docx").json()
    res = client.get("/api/zip")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = zf.namelist()
        assert names == ["test.md", "test (2).md"]
        assert zf.read("test.md").decode() == a["markdown"]

    only = client.get(f"/api/zip?ids={b['id']}")
    with zipfile.ZipFile(io.BytesIO(only.content)) as zf:
        assert zf.namelist() == ["test.md"]


def test_zip_empty(client: TestClient) -> None:
    res = client.get("/api/zip")
    assert res.status_code == 404
    assert "nothing" in res.json()["error"].lower()


def test_store_is_capped() -> None:
    class Tiny(MarkitForge):
        def convert_local(self, *args: Any, **kwargs: Any) -> DocumentConverterResult:
            return DocumentConverterResult(markdown="x")

    app = create_app(converter_factory=Tiny, store_capacity=3)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        ids = [upload(c, f"f{i}.txt", data=b"x").json()["id"] for i in range(5)]
        assert c.get(f"/api/download/{ids[0]}").status_code == 404
        assert c.get(f"/api/download/{ids[-1]}").status_code == 200
    assert len(app.state.store) == 3


def test_download_unknown(client: TestClient) -> None:
    res = client.get("/api/download/does-not-exist")
    assert res.status_code == 404


def test_download_filename_is_safe() -> None:
    class Tiny(MarkitForge):
        def convert_local(self, *args: Any, **kwargs: Any) -> DocumentConverterResult:
            return DocumentConverterResult(markdown="x")

    with TestClient(
        create_app(converter_factory=Tiny), base_url="http://127.0.0.1"
    ) as c:
        data = upload(c, '../../etc/"evil"\r\nX: y.pdf', data=b"x").json()
        md_name = data["md_filename"]
        assert md_name.endswith(".md")
        for bad in ("/", "\\", '"', "\r", "\n", ".."):
            assert bad not in md_name
        cd = c.get(f"/api/download/{data['id']}").headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd
    assert cd.count('"') == 2  # only the quotes around the ASCII filename

    from markitforge_gui._app import _safe_stem

    assert _safe_stem('../../etc/"evil"\r\nX: y.pdf') == "evil_X_ y"
    assert _safe_stem("...") == "document"
    assert _safe_stem("C:\\Users\\me\\report.final.docx") == "report.final"


# ---------------------------------------------------------------- url


def test_convert_url_rejects_non_http(client: TestClient) -> None:
    for bad in ("file:///etc/passwd", "data:text/plain,hi", "ftp://x.y/z", ""):
        res = client.post("/api/convert-url", json={"url": bad}, headers=HEADERS)
        assert res.status_code == 400, bad


def test_convert_url_uses_converter() -> None:
    seen: Dict[str, Any] = {}

    class Fake(MarkitForge):
        def convert_uri(self, uri: str, **kwargs: Any) -> DocumentConverterResult:
            seen["uri"] = uri
            seen["kwargs"] = kwargs
            return DocumentConverterResult(markdown="# Page", title="My Page")

    with TestClient(
        create_app(converter_factory=Fake), base_url="http://127.0.0.1"
    ) as c:
        res = c.post(
            "/api/convert-url",
            json={"url": "example.com/a", "options": {"table_format": "html"}},
            headers=HEADERS,
        )
    assert res.status_code == 200, res.text
    assert seen["uri"] == "https://example.com/a"
    assert seen["kwargs"] == {
        "table_format": "html",
        "max_download_bytes": 200 * 1024 * 1024,
    }
    assert res.json()["md_filename"] == "My Page.md"


# ---------------------------------------------------------------- helpers


def test_parse_options() -> None:
    assert parse_options(None) == {}
    assert parse_options('{"page_markers": true, "unknown": 1}') == {
        "page_markers": True
    }
    with pytest.raises(OptionsError):
        parse_options('{"pdf_backend": "rm -rf"}')
    with pytest.raises(OptionsError):
        parse_options('{"keep_data_uris": "yes"}')
    with pytest.raises(OptionsError):
        parse_options("[1]")


def test_content_disposition_unicode() -> None:
    cd = content_disposition("Résumé 2024.md")
    assert 'filename="Resume 2024.md"' in cd
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9%202024.md" in cd


def test_cli_parser_and_socket() -> None:
    from markitforge_gui._cli import bind_socket, build_parser, is_loopback

    args = build_parser().parse_args(["--no-browser", "--port", "0"])
    assert args.no_browser and args.port == 0 and args.host == "127.0.0.1"
    assert is_loopback("127.0.0.1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0")

    first = bind_socket("127.0.0.1", 0)
    try:
        taken = first.getsockname()[1]
        second = bind_socket("127.0.0.1", taken)  # busy -> falls back
        try:
            assert second.getsockname()[1] != taken
        finally:
            second.close()
    finally:
        first.close()


def test_module_entry_point() -> None:
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "markitforge_gui", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},
    )
    assert out.returncode == 0
    assert "--no-browser" in out.stdout and "--max-upload-mb" in out.stdout


# ---- hardening regression tests ----------------------------------------------


@pytest.mark.parametrize(
    "src",
    ["\\/evil.com/p.png", "/\t/evil.com/p.png", "//evil.com/p.png", "/api/zip"],
)
def test_sanitizer_drops_remote_and_same_origin_image_paths(src):
    from markitforge_gui._render import sanitize_html

    assert "src=" not in sanitize_html(f'<img src="{src}">')


def test_sanitizer_drops_q_cite():
    from markitforge_gui._render import sanitize_html

    assert "javascript" not in sanitize_html('<q cite="javascript:alert(1)">x</q>')


def test_wildcard_hosts_are_rejected():
    from markitforge_gui import create_app

    with pytest.raises(ValueError):
        create_app(allowed_hosts=["*"])


def test_cli_wildcard_bind_does_not_allow_any_host():
    from markitforge_gui._cli import _allowed_hosts

    hosts = _allowed_hosts("0.0.0.0")
    assert "*" not in hosts and "localhost" in hosts


def test_rebinding_origin_not_on_allow_list_is_refused():
    from markitforge_gui import create_app

    app = create_app(allowed_hosts=["127.0.0.1", "localhost", "evil.example"])
    # evil.example passes the Host check here only because we put it in the
    # list for the test; the Origin check must still match the list.
    client = TestClient(app, base_url="http://evil.example")
    r = client.post(
        "/api/render",
        json={"markdown": "x"},
        headers={"X-MarkitForge-Client": "1", "Origin": "http://evil.example"},
    )
    assert r.status_code == 200  # on the list: allowed
    app2 = create_app()
    client2 = TestClient(app2, base_url="http://127.0.0.1")
    r2 = client2.post(
        "/api/render",
        json={"markdown": "x"},
        headers={
            "X-MarkitForge-Client": "1",
            "Origin": "http://attacker.example",
            "Host": "127.0.0.1",
        },
    )
    assert r2.status_code == 403


def test_huge_page_range_is_fast(client):
    import time

    rid = upload(client, "test.pdf").json()["id"]
    start = time.monotonic()
    r = client.get(f"/api/download/{rid}?pages=1-999999999999")
    assert r.status_code == 200
    assert time.monotonic() - start < 2


def test_zip_bomb_upload_gets_friendly_413(client):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", b"\0" * (20 * 1024 * 1024))
    r = upload(client, "bomb.docx", data=buf.getvalue())
    assert r.status_code in (413, 415, 422)
    assert "Traceback" not in r.text
