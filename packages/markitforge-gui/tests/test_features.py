# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Session token, jobs, history, original view, editing, folders, AI options."""

import io
import json
import os
import stat
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
from starlette.testclient import TestClient

from markitforge import (
    ConversionCancelled,
    DocumentAsset,
    DocumentConverterResult,
    DocumentSection,
    MarkitForge,
    ProgressEvent,
)
from markitforge._config import Config
from markitforge_gui import create_app
from markitforge_gui._app import (
    MAX_EDIT_BYTES,
    TOKEN_HEADER,
    safe_rel_dir,
)
from markitforge_gui._history import HistoryStore, default_data_dir

from .helpers import HEADERS, TC, TEST_FILES, upload, wait_job

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
    b"\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)


class Tiny(MarkitForge):
    def convert_local(self, *args: Any, **kwargs: Any) -> DocumentConverterResult:
        return DocumentConverterResult(
            markdown="# Tiny\n\nbody",
            title="Tiny",
            pages=[DocumentSection(index=1, markdown="# Tiny\n\nbody")],
            warnings=["w1"],
            metadata={"author": "me"},
        )


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TC(create_app(converter_factory=Tiny)) as c:
        yield c


# ---------------------------------------------------------------- token


def _endpoints(item_id: str, job_id: str) -> List[Any]:
    return [
        ("GET", "/api/info", None),
        ("GET", "/api/history", None),
        ("GET", f"/api/download/{item_id}", None),
        ("GET", "/api/zip", None),
        ("GET", f"/api/results/{item_id}", None),
        ("GET", f"/api/original/{item_id}", None),
        ("GET", f"/api/original/{item_id}/info", None),
        ("GET", f"/api/jobs/{job_id}", None),
        ("GET", "/api/llm", None),
        ("POST", "/api/render", {"markdown": "x"}),
        ("POST", "/api/convert-url", {"url": "https://example.com"}),
        ("POST", f"/api/jobs/{job_id}/cancel", {}),
        ("PUT", f"/api/results/{item_id}/markdown", {"markdown": "x"}),
        ("PUT", "/api/llm", {"model": "m"}),
        ("POST", "/api/llm/test", {}),
        ("DELETE", "/api/history", None),
    ]


def test_token_required_on_every_endpoint() -> None:
    app = create_app(converter_factory=Tiny)
    with TC(app) as good:
        job = upload(good, "a.txt", data=b"x", wait=False).json()["job_id"]
        item = wait_job(good, good.get("/api/jobs/x")).json()  # 404 -> response
        assert "error" in item
        rid = upload(good, "a.txt", data=b"x").json()["id"]
    with TestClient(app, base_url="http://127.0.0.1") as bare:
        for token in (None, "wrong", app.state.token[:-1] + "A"):
            headers = dict(HEADERS)
            if token is not None:
                headers[TOKEN_HEADER] = token
            for method, path, body in _endpoints(rid, job):
                res = bare.request(method, path, json=body, headers=headers)
                assert res.status_code == 403, (method, path, token)
                assert res.json()["code"] == "token"
            res = bare.post(
                "/api/convert",
                files={"file": ("a.txt", b"x")},
                headers=headers,
            )
            assert res.status_code == 403


def test_token_in_query_for_get_links_only() -> None:
    app = create_app(converter_factory=Tiny)
    with TC(app) as good:
        rid = upload(good, "a.txt", data=b"x").json()["id"]
    t = app.state.token
    with TestClient(app, base_url="http://127.0.0.1") as bare:
        assert bare.get(f"/api/download/{rid}?t={t}").status_code == 200
        assert bare.get(f"/api/zip?ids={rid}&t={t}").status_code == 200
        assert bare.get(f"/api/download/{rid}?t=nope").status_code == 403
        # A POST cannot use the query string.
        res = bare.post(f"/api/render?t={t}", json={"markdown": "x"}, headers=HEADERS)
        assert res.status_code == 403


def test_token_served_in_page_meta() -> None:
    app = create_app(token="known-token-value-123")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        res = c.get("/")
    assert '<meta name="mf-token" content="known-token-value-123">' in res.text
    assert res.headers["cache-control"] == "no-store"
    assert "unsafe-inline" not in res.headers["content-security-policy"]
    assert "<script>" not in res.text  # no inline scripts
    # Each app (launch) gets its own random token.
    assert create_app().state.token != create_app().state.token


def test_client_header_still_required_with_token(client: TestClient) -> None:
    res = client.post("/api/render", json={"markdown": "x"})
    assert res.status_code == 403
    res = client.put("/api/llm", json={"model": "m"})
    assert res.status_code == 403
    res = client.delete("/api/history")
    assert res.status_code == 403


# ---------------------------------------------------------------- jobs


class Slow(MarkitForge):
    """Reports progress per 'page' and honours cancel_event."""

    started = threading.Event()
    proceed = threading.Event()
    seen: Dict[str, Any] = {}

    def convert_local(self, *args: Any, **kwargs: Any) -> DocumentConverterResult:
        progress = kwargs["progress"]
        cancel = kwargs["cancel_event"]
        Slow.seen = kwargs
        progress(ProgressEvent("page", 1, 4, "Page 1"))
        Slow.started.set()
        Slow.proceed.wait(10)
        for i in range(2, 5):
            if cancel.is_set():
                raise ConversionCancelled("stop")
            progress(ProgressEvent("page", i, 4))
            time.sleep(0.01)
        return DocumentConverterResult(markdown="done")


def _poll(
    c: TestClient, job_id: str, until: Any, timeout: float = 10
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = c.get(f"/api/jobs/{job_id}").json()
        if until(data):
            return data
        time.sleep(0.01)
    raise AssertionError(data)


def test_job_progress_then_done() -> None:
    Slow.started.clear()
    Slow.proceed.clear()
    with TC(create_app(converter_factory=Slow)) as c:
        res = upload(c, "a.pdf", data=b"%PDF-1.4", wait=False)
        assert res.status_code == 202
        body = res.json()
        assert body["status"] in ("queued", "running")
        job_id = body["job_id"]
        assert Slow.started.wait(10)
        data = _poll(c, job_id, lambda d: (d["progress"] or {}).get("current") == 1)
        assert data["status"] == "running"
        assert data["progress"]["total"] == 4
        assert data["progress"]["fraction"] == 0.25
        assert data["progress"]["stage"] == "page"
        assert data["progress"]["message"] == "Page 1"
        assert "result" not in data
        Slow.proceed.set()
        data = _poll(c, job_id, lambda d: d["status"] == "done")
        assert data["result"]["markdown"] == "done"
        assert data["progress"]["fraction"] == 1.0
        assert c.get(f"/api/download/{data['result']['id']}").text == "done"


def test_job_cancel_running() -> None:
    Slow.started.clear()
    Slow.proceed.clear()
    with TC(create_app(converter_factory=Slow)) as c:
        job_id = upload(c, "a.pdf", data=b"%PDF-1.4", wait=False).json()["job_id"]
        assert Slow.started.wait(10)
        res = c.post(f"/api/jobs/{job_id}/cancel", headers=HEADERS)
        assert res.status_code == 200
        assert res.json()["cancel_requested"] is True
        assert Slow.seen["cancel_event"].is_set()
        Slow.proceed.set()
        data = _poll(c, job_id, lambda d: d["status"] in ("cancelled", "done", "error"))
        assert data["status"] == "cancelled"
        assert "result" not in data


def test_job_cancel_queued_cleans_temp_file(tmp_path: Path) -> None:
    import tempfile

    Slow.started.clear()
    Slow.proceed.clear()
    before = set(Path(tempfile.gettempdir()).glob("markitforge-gui-*"))
    with TC(create_app(converter_factory=Slow, max_workers=1)) as c:
        first = upload(c, "a.pdf", data=b"%PDF-1.4", wait=False).json()["job_id"]
        assert Slow.started.wait(10)
        second = upload(c, "b.pdf", data=b"%PDF-1.4", wait=False).json()["job_id"]
        res = c.post(f"/api/jobs/{second}/cancel", headers=HEADERS).json()
        assert res["status"] == "cancelled"
        Slow.proceed.set()
        _poll(c, first, lambda d: d["status"] == "done")
        _poll(c, second, lambda d: d["status"] == "cancelled")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            after = set(Path(tempfile.gettempdir()).glob("markitforge-gui-*"))
            if not after - before:
                break
            time.sleep(0.02)
        assert after - before == set()


def test_job_error_payload(client: TestClient) -> None:
    class Bad(MarkitForge):
        def convert_local(self, *a: Any, **k: Any) -> DocumentConverterResult:
            from markitforge import UnsupportedFormatException

            raise UnsupportedFormatException("nope")

    with TC(create_app(converter_factory=Bad)) as c:
        job_id = upload(c, "a.bin", data=b"x", wait=False).json()["job_id"]
        data = _poll(c, job_id, lambda d: d["status"] == "error")
    assert data["http_status"] == 415
    assert data["error"]["error"] == "This file type is not supported yet."


def test_unknown_job_and_ttl() -> None:
    app = create_app(converter_factory=Tiny, job_ttl=0)
    with TC(app) as c:
        assert c.get("/api/jobs/nope").status_code == 404
        res = upload(c, "a.txt", data=b"x", wait=False)
        job_id = res.json()["job_id"]
        deadline = time.monotonic() + 5
        while app.state.jobs._jobs[job_id].status != "done":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.01)
        assert c.get(f"/api/jobs/{job_id}").status_code == 404  # pruned
        assert len(app.state.jobs) == 0


# ---------------------------------------------------------------- history


def test_history_persists_across_instances(tmp_path: Path) -> None:
    hist = tmp_path / "data" / "history"
    with TC(create_app(converter_factory=Tiny, history_dir=hist)) as c:
        data = upload(
            c, "report.pdf", data=b"%PDF-1.4", rel_path="a/b/report.pdf"
        ).json()
        rid = data["id"]
        res = c.put(
            f"/api/results/{rid}/markdown",
            json={"markdown": "# Edited"},
            headers=HEADERS,
        )
        assert res.status_code == 200
    with TC(create_app(converter_factory=Tiny, history_dir=hist)) as c2:
        listing = c2.get("/api/history").json()
        assert listing["enabled"] is True
        assert [i["id"] for i in listing["items"]] == [rid]
        assert listing["items"][0]["filename"] == "report.pdf"
        assert listing["items"][0]["rel_dir"] == "a/b"
        assert listing["items"][0]["edited"] is True
        again = c2.get(f"/api/results/{rid}").json()
        assert again["markdown"] == "# Edited"
        assert again["title"] == "Tiny"
        assert again["warnings"] == ["w1"]
        assert again["metadata"] == {"author": "me"}
        assert again["pages"] == [{"index": 1, "kind": "page", "title": None}]
        assert again["edited"] is True
        assert again["has_original"] is False  # originals stay in memory only
        assert c2.get(f"/api/download/{rid}").text == "# Edited"
        info = c2.get(f"/api/original/{rid}/info").json()
        assert info["kind"] == "none"


def test_history_file_modes_and_atomic(tmp_path: Path) -> None:
    hist = tmp_path / "data" / "history"
    with TC(create_app(converter_factory=Tiny, history_dir=hist)) as c:
        rid = upload(c, "a.txt", data=b"x").json()["id"]
    files = list(hist.iterdir())
    assert [p.name for p in files] == [f"{rid}.json"]  # no temp files left
    if os.name == "posix":
        assert stat.S_IMODE(hist.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    saved = json.loads(files[0].read_text())
    assert saved["markdown"] == "# Tiny\n\nbody"
    assert "options" not in saved and "llm" not in json.dumps(saved).lower()


def test_history_caps(tmp_path: Path) -> None:
    hist = tmp_path / "h"
    with TC(
        create_app(converter_factory=Tiny, history_dir=hist, history_max_entries=3)
    ) as c:
        ids = [upload(c, f"f{i}.txt", data=b"x").json()["id"] for i in range(5)]
        listed = [i["id"] for i in c.get("/api/history").json()["items"]]
    assert listed == list(reversed(ids[2:]))
    assert sorted(p.stem for p in hist.glob("*.json")) == sorted(ids[2:])

    store = HistoryStore(tmp_path / "h2", max_entries=100, max_bytes=1)
    for _ in range(3):
        entry = create_app(converter_factory=Tiny).state.store.add(
            "x", "x.md", DocumentConverterResult(markdown="y" * 100)
        )
        store.save(entry)
    assert len(store) == 1  # byte cap keeps only the newest


def test_clear_history(tmp_path: Path) -> None:
    hist = tmp_path / "h"
    with TC(create_app(converter_factory=Tiny, history_dir=hist)) as c:
        rid = upload(c, "a.txt", data=b"x").json()["id"]
        (hist / "unrelated.txt").write_text("keep me")
        res = c.delete("/api/history", headers=HEADERS)
        assert res.status_code == 200 and res.json()["cleared"] == 1
        assert c.get("/api/history").json()["items"] == []
        assert c.get(f"/api/results/{rid}").status_code == 404
    assert sorted(p.name for p in hist.iterdir()) == ["unrelated.txt"]


def test_history_ignores_bad_files_and_ids(tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.mkdir(parents=True)
    (hist / "not-an-id.json").write_text("{}")
    (hist / ("A" * 22 + ".json")).write_text("not json")
    store = HistoryStore(hist)
    assert len(store) == 0
    assert store.load("../../etc/passwd") is None
    with pytest.raises(ValueError):
        store._json_path("../x")


def test_no_history_by_default(client: TestClient, tmp_path: Path) -> None:
    upload(client, "a.txt", data=b"x")
    assert client.get("/api/history").json() == {"enabled": False, "items": []}
    assert not (tmp_path / "data").exists()


def test_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MARKITFORGE_GUI_DATA_DIR", str(tmp_path / "x"))
    assert default_data_dir() == tmp_path / "x"
    monkeypatch.delenv("MARKITFORGE_GUI_DATA_DIR")
    if sys.platform == "darwin":
        assert default_data_dir().parts[-3:] == (
            "Library",
            "Application Support",
            "MarkitForge",
        )
    elif os.name != "nt":
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert default_data_dir() == tmp_path / "xdg" / "markitforge"


def test_cli_history_flags(tmp_path: Path) -> None:
    from markitforge_gui._cli import build_parser, history_dir_for

    args = build_parser().parse_args(["--data-dir", str(tmp_path)])
    assert history_dir_for(args) == tmp_path / "history"
    assert args.allow_private_urls is False
    args = build_parser().parse_args(["--no-history", "--allow-private-urls"])
    assert history_dir_for(args) is None and args.allow_private_urls is True


# ---------------------------------------------------------------- original


def test_original_pdf_headers(tmp_path: Path) -> None:
    pdf = (TEST_FILES / "test.pdf").read_bytes()
    with TC(create_app()) as c:
        data = upload(c, "test.pdf", data=pdf).json()
        assert data["has_original"] is True
        info = c.get(f"/api/original/{data['id']}/info").json()
        assert info["kind"] == "pdf"
        res = c.get(f"/api/original/{data['id']}")
    assert res.status_code == 200
    assert res.content == pdf
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith("inline;")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "SAMEORIGIN"
    csp = res.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'self'" in csp
    assert "script-src" not in csp
    assert "plugin-types" not in csp  # breaks Chromium's viewer in dynamic frames


def test_original_image_and_text(client: TestClient) -> None:
    rid = upload(client, "pic.png", data=PNG).json()["id"]
    assert client.get(f"/api/original/{rid}/info").json()["kind"] == "image"
    res = client.get(f"/api/original/{rid}")
    assert res.headers["content-type"] == "image/png"
    assert "sandbox" in res.headers["content-security-policy"]

    html_doc = b"<script>alert(1)</script><b>hi</b>"
    rid = upload(client, "page.html", data=html_doc).json()["id"]
    info = client.get(f"/api/original/{rid}/info").json()
    assert info["kind"] == "text"
    assert info["text"] == html_doc.decode()  # the page shows it as text
    # Text files are never served raw (no HTML from the document).
    assert client.get(f"/api/original/{rid}").status_code == 415

    # A ".png" that is not a PNG is not served as an image.
    rid = upload(client, "fake.png", data=b"<svg onload=alert(1)>").json()["id"]
    assert client.get(f"/api/original/{rid}/info").json()["kind"] == "none"
    assert client.get(f"/api/original/{rid}").status_code == 415


def test_original_size_cap() -> None:
    with TC(create_app(converter_factory=Tiny, max_original_mb=0.001)) as c:
        rid = upload(c, "big.pdf", data=b"%PDF-1.4" + b"x" * 5000).json()["id"]
        info = c.get(f"/api/original/{rid}/info").json()
        assert info["kind"] == "none"
        assert "larger than" in info["message"]
        assert c.get(f"/api/original/{rid}").status_code == 404
        small = upload(c, "s.pdf", data=b"%PDF-1.4 tiny").json()
        assert small["has_original"] is True


def test_original_office_without_libreoffice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from markitforge.converters import _libreoffice_converter as lo

    monkeypatch.setattr(lo, "find_soffice", lambda: None)
    rid = upload(client, "a.docx", data=b"PK\x03\x04 not really").json()["id"]
    info = client.get(f"/api/original/{rid}/info").json()
    assert info["kind"] == "none"
    assert "Preview not available" in info["message"]


def _soffice() -> bool:
    from markitforge.converters._libreoffice_converter import find_soffice

    return find_soffice() is not None


@pytest.mark.skipif(not _soffice(), reason="LibreOffice is not installed")
def test_original_office_preview_with_libreoffice(client: TestClient) -> None:
    docx = (TEST_FILES / "test.docx").read_bytes()
    rid = upload(client, "test.docx", data=docx).json()["id"]
    info = client.get(f"/api/original/{rid}/info").json()
    assert info["kind"] == "pdf" and info["converted"] is True
    res = client.get(f"/api/original/{rid}")
    assert res.headers["content-type"] == "application/pdf"
    assert res.content.startswith(b"%PDF-")
    assert 'filename="test.pdf"' in res.headers["content-disposition"]


def test_original_for_url_result() -> None:
    class Fake(MarkitForge):
        def convert_uri(self, uri: str, **kwargs: Any) -> DocumentConverterResult:
            return DocumentConverterResult(markdown="# Page")

    with TC(create_app(converter_factory=Fake)) as c:
        res = wait_job(
            c,
            c.post(
                "/api/convert-url", json={"url": "https://x.example"}, headers=HEADERS
            ),
        )
        info = c.get(f"/api/original/{res.json()['id']}/info").json()
    assert info["kind"] == "none" and "uploaded files" in info["message"]


# ---------------------------------------------------------------- editing


def test_markdown_edit_put(client: TestClient) -> None:
    rid = upload(client, "a.txt", data=b"x").json()["id"]
    res = client.put(
        f"/api/results/{rid}/markdown", json={"markdown": "new text"}, headers=HEADERS
    )
    assert res.status_code == 200 and res.json()["edited"] is True
    assert client.get(f"/api/download/{rid}").text == "new text"
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/zip?ids={rid}").content)) as zf:
        assert zf.read("a.md").decode() == "new text"
    assert client.get(f"/api/results/{rid}").json()["edited"] is True
    # Page selection no longer matches the edited text.
    res = client.post("/api/render", json={"id": rid, "pages": "1"}, headers=HEADERS)
    assert res.status_code == 409
    assert client.get(f"/api/download/{rid}?pages=1").status_code == 409
    # Rendering the edit keeps working.
    res = client.post(
        "/api/render", json={"markdown": "# H", "for_id": rid}, headers=HEADERS
    )
    assert "<h1>H</h1>" in res.json()["html"]


def test_markdown_edit_limits(client: TestClient) -> None:
    rid = upload(client, "a.txt", data=b"x").json()["id"]
    big = "a" * (MAX_EDIT_BYTES + 1)
    res = client.put(
        f"/api/results/{rid}/markdown", json={"markdown": big}, headers=HEADERS
    )
    assert res.status_code == 413
    res = client.put(
        f"/api/results/{rid}/markdown", json={"markdown": 5}, headers=HEADERS
    )
    assert res.status_code == 400
    res = client.put(
        "/api/results/unknown/markdown", json={"markdown": "x"}, headers=HEADERS
    )
    assert res.status_code == 404
    res = client.put(
        f"/api/results/{rid}/markdown",
        json={"markdown": "x"},
        headers={**HEADERS, "Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    assert client.get(f"/api/download/{rid}").text == "# Tiny\n\nbody"


# ---------------------------------------------------------------- folders


@pytest.mark.parametrize(
    "rel, expected",
    [
        ("folder/sub/file.pdf", "folder/sub"),
        ("file.pdf", ""),
        ("../../etc/passwd", "etc"),
        ("/abs/path/x.pdf", "abs/path"),
        ("C:\\Windows\\System32\\x.dll", "C/Windows/System32"),
        ("a/./../b/.hidden/x", "a/b/hidden"),
        ("..\\..\\x", ""),
        ("a//b\\\\c/x", "a/b/c"),
    ],
)
def test_safe_rel_dir(rel: str, expected: str) -> None:
    out = safe_rel_dir(rel)
    assert out == expected
    assert not out.startswith("/") and ".." not in out.split("/")


def test_folder_zip_structure_and_zip_slip(client: TestClient) -> None:
    ids = [
        upload(client, "a.pdf", data=b"x", rel_path="proj/docs/a.pdf").json()["id"],
        upload(client, "a.pdf", data=b"x", rel_path="proj/docs/a.pdf").json()["id"],
        upload(client, "b.pdf", data=b"x", rel_path="proj/b.pdf").json()["id"],
        upload(client, "evil.pdf", data=b"x", rel_path="../../../evil.pdf").json()[
            "id"
        ],
        upload(client, "win.pdf", data=b"x", rel_path="C:\\x\\..\\win.pdf").json()[
            "id"
        ],
        upload(client, "top.pdf", data=b"x").json()["id"],
    ]
    res = client.get(f"/api/zip?ids={','.join(ids)}")
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = zf.namelist()
    assert names == [
        "proj/docs/a.md",
        "proj/docs/a (2).md",
        "proj/b.md",
        "evil.md",
        "C/x/win.md",
        "top.md",
    ]
    for name in names:
        assert not name.startswith("/") and ".." not in name.split("/")
        assert "\\" not in name


# ---------------------------------------------------------------- AI options


class Recorder(MarkitForge):
    calls: List[Dict[str, Any]] = []

    def convert_local(self, *args: Any, **kwargs: Any) -> DocumentConverterResult:
        Recorder.calls.append(kwargs)
        return DocumentConverterResult(
            markdown="![chart](img1.png)\n\nText",
            assets=[
                DocumentAsset(name="img1.png", mime_type="image/png", data=PNG),
                DocumentAsset(name="../../x.png", mime_type="image/png", data=PNG),
            ],
        )


class FakeOpenAI:
    instances: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        FakeOpenAI.instances.append(kwargs)
        outer = self

        class Models:
            def list(self) -> Any:
                class Page:
                    data = [type("M", (), {"id": "llama3.2-vision"})()]

                return Page()

        self.models = Models()
        self.kwargs = kwargs
        del outer


def test_ai_options_reach_converter(tmp_path: Path) -> None:
    Recorder.calls = []
    FakeOpenAI.instances = []
    app = create_app(converter_factory=Recorder, llm_client_factory=FakeOpenAI)
    opts = {
        "ocr_scanned_pages": True,
        "ocr_engine": "llm",
        "describe_images": True,
        "transcribe_formulas": True,
        "extract_images": True,
    }
    with TC(app) as c:
        data = upload(c, "a.pdf", data=b"%PDF", options=opts).json()
        kwargs = Recorder.calls[-1]
        for key, value in opts.items():
            assert kwargs[key] == value
        assert isinstance(kwargs["llm_client"], FakeOpenAI)
        assert kwargs["llm_model"] == "llama3.2-vision"
        inst = FakeOpenAI.instances[-1]
        assert inst["base_url"] == "http://localhost:11434/v1"
        # No AI option: no client is passed (nothing leaves the computer).
        upload(
            c,
            "b.pdf",
            data=b"%PDF",
            options={"ocr_scanned_pages": True, "ocr_engine": "tesseract"},
        )
        assert "llm_client" not in Recorder.calls[-1]
        upload(c, "c.pdf", data=b"%PDF", options={})
        assert "llm_client" not in Recorder.calls[-1]

        # Saved images: listed, inlined into the preview, put in the zip.
        assert data["saved_assets"] == ["img1.png", "x.png"]
        assert 'src="data:image/png;base64,' in data["html"]
        with zipfile.ZipFile(
            io.BytesIO(c.get(f"/api/zip?ids={data['id']}").content)
        ) as zf:
            names = zf.namelist()
            assert "a_assets/img1.png" in names and "a_assets/x.png" in names
            assert zf.read("a_assets/img1.png") == PNG
            assert "![chart](a_assets/img1.png)" in zf.read("a.md").decode()
        assert all(".." not in n for n in names)


def test_bad_ai_option_values(client: TestClient) -> None:
    res = upload(client, "a.pdf", data=b"x", options={"ocr_engine": "magic"})
    assert res.status_code == 400
    res = upload(client, "a.pdf", data=b"x", options={"extract_images": "yes"})
    assert res.status_code == 400


def test_api_key_never_stored_or_echoed(tmp_path: Path) -> None:
    secret = "sk-test-SECRET-1234567890"
    FakeOpenAI.instances = []
    Recorder.calls = []
    hist = tmp_path / "hist"
    app = create_app(
        converter_factory=Recorder, llm_client_factory=FakeOpenAI, history_dir=hist
    )
    with TC(app) as c:
        # OpenAI-compatible provider without a key: refused before any job.
        res = c.put(
            "/api/llm",
            json={
                "provider": "openai",
                "base_url": "https://api.example/v1",
                "model": "gpt-x",
            },
            headers=HEADERS,
        )
        assert res.status_code == 200 and res.json()["key_set"] is False
        res = upload(c, "a.pdf", data=b"x", options={"describe_images": True})
        assert res.status_code == 400 and "API key" in res.json()["error"]

        res = c.put("/api/llm", json={"api_key": secret}, headers=HEADERS)
        body = res.text
        assert res.status_code == 200 and secret not in body
        assert res.json()["key_set"] is True and res.json()["key_source"] == "session"
        assert secret not in c.get("/api/llm").text
        data = upload(c, "a.pdf", data=b"x", options={"describe_images": True}).json()
        assert FakeOpenAI.instances[-1]["api_key"] == secret
        assert FakeOpenAI.instances[-1]["base_url"] == "https://api.example/v1"
        assert Recorder.calls[-1]["llm_model"] == "gpt-x"
        test = c.post("/api/llm/test", json={}, headers=HEADERS)
        assert test.status_code == 200 and secret not in test.text
        for path in ("/api/info", "/api/history", f"/api/results/{data['id']}"):
            assert secret not in c.get(path).text
        # Forget it again.
        res = c.put("/api/llm", json={"clear_key": True}, headers=HEADERS)
        assert res.json()["key_set"] is False
    for path in hist.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_llm_settings_validation(client: TestClient) -> None:
    for bad in (
        {"provider": "evil"},
        {"base_url": "file:///etc/passwd"},
        {"base_url": "https://user:pw@api.example/v1"},
        {"model": "a b; rm"},
        {"api_key": "has space"},
    ):
        res = client.put("/api/llm", json=bad, headers=HEADERS)
        assert res.status_code == 400, bad
    info = client.get("/api/llm").json()
    assert info["provider"] == "local" and info["is_local"] is True


def test_llm_test_scrubs_key_from_errors() -> None:
    secret = "sk-test-LEAK-abcdefghijkl"

    class Broken:
        def __init__(self, **kwargs: Any) -> None:
            class Models:
                def list(self) -> Any:
                    raise RuntimeError(f"401 bad key {secret}")

            class Completions:
                def create(self, **kw: Any) -> Any:
                    raise RuntimeError(f"invalid api key {secret}")

            self.models = Models()
            self.chat = type("C", (), {"completions": Completions()})()

    with TC(create_app(llm_client_factory=Broken)) as c:
        c.put(
            "/api/llm",
            json={
                "provider": "openai",
                "base_url": "https://a.example/v1",
                "api_key": secret,
            },
            headers=HEADERS,
        )
        res = c.post("/api/llm/test", json={}, headers=HEADERS)
    assert res.status_code == 400
    assert secret not in res.text and "***" in res.text


def test_llm_defaults_from_config() -> None:
    cfg = Config(
        llm={"base_url": "https://llm.example/v1", "model": "vision-1"},
        defaults={"page_markers": True, "describe_images": False},
    )
    with TC(create_app(config=cfg)) as c:
        info = c.get("/api/llm").json()
        assert info["provider"] == "openai" and info["model"] == "vision-1"
        assert c.get("/api/info").json()["defaults"] == {
            "page_markers": True,
            "describe_images": False,
        }


def test_config_defaults_and_limits_applied() -> None:
    Recorder.calls = []
    cfg = Config(defaults={"page_markers": True}, limits={"max_pdf_pages": 7})
    with TC(create_app(converter_factory=Recorder, config=cfg)) as c:
        upload(c, "a.pdf", data=b"x", options={"table_format": "html"})
        upload(c, "a.pdf", data=b"x", options={"page_markers": False})
    assert Recorder.calls[0]["page_markers"] is True
    assert Recorder.calls[0]["max_pdf_pages"] == 7
    assert Recorder.calls[0]["table_format"] == "html"
    assert Recorder.calls[1]["page_markers"] is False


# ---------------------------------------------------------------- URLs


def test_url_converter_blocks_private_networks() -> None:
    made: List[Dict[str, Any]] = []

    class Fake(MarkitForge):
        def __init__(self, **kwargs: Any) -> None:
            made.append(kwargs)
            super().__init__(**kwargs)

        def convert_uri(self, uri: str, **kwargs: Any) -> DocumentConverterResult:
            return DocumentConverterResult(markdown="# Page")

    app = create_app(converter_factory=Fake, max_upload_mb=3)
    assert app.state.url_converter_kwargs["block_private_networks"] is True
    with TC(app) as c:
        res = wait_job(
            c,
            c.post(
                "/api/convert-url", json={"url": "https://a.example"}, headers=HEADERS
            ),
        )
        assert res.status_code == 200
    url_made = [k for k in made if "block_private_networks" in k]
    assert url_made and url_made[0]["block_private_networks"] is True
    assert url_made[0]["max_download_bytes"] == 3 * 1024 * 1024

    app2 = create_app(allow_private_urls=True)
    assert app2.state.url_converter_kwargs["block_private_networks"] is False


def test_private_url_is_refused_with_friendly_error() -> None:
    with TC(create_app()) as c:
        res = wait_job(
            c,
            c.post(
                "/api/convert-url",
                json={"url": "http://127.0.0.1:1/secret"},
                headers=HEADERS,
            ),
        )
    assert res.status_code == 400
    assert "private network" in res.json()["error"]
    assert "--allow-private-urls" in res.json()["hint"]


# ---------------------------------------------------------------- theme


def test_theme_switch_markup_and_script() -> None:
    with TC(create_app()) as c:
        page = c.get("/").text
        js = c.get("/static/theme.js")
        css = c.get("/static/app.css").text
    assert 'role="radiogroup"' in page and 'aria-label="Color theme"' in page
    for choice in ("system", "light", "dark"):
        assert f'data-theme-choice="{choice}"' in page
    # theme.js loads synchronously, before the stylesheet.
    head = page.split("</head>", 1)[0]
    assert head.index('src="/static/theme.js"></script>') < head.index("app.css")
    assert 'theme.js" defer' not in head
    assert js.status_code == 200 and "data-theme" in js.text
    assert "javascript" in js.headers["content-type"]
    assert ':root[data-theme="dark"]' in css
    assert ":root:not([data-theme])" in css


def test_static_assets_have_no_external_urls() -> None:
    static = Path(__file__).resolve().parents[1] / "src" / "markitforge_gui" / "static"
    for path in static.iterdir():
        text = path.read_text(encoding="utf-8")
        for bad in ("https://cdn", "http://cdn", "googleapis", "unpkg", "jsdelivr"):
            assert bad not in text, (path.name, bad)
        if path.suffix == ".html":
            assert "style=" not in text and "<script>" not in text
            assert " on" + "click=" not in text
