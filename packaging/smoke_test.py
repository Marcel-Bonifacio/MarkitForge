# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke-test a packaged MarkitForge app (or any ``markitforge-gui`` command).

Starts the app with ``--no-browser --port PORT`` (and ``--no-history`` when the
app has it, so nothing is written to the user's data folder), waits until it
answers,
checks the page and a static asset, converts a few test files through the
same HTTP API the page uses (client header plus the per-launch session token
that the page carries in ``<meta name="mf-token">``), then stops the app. Standard library only, so it
runs with any Python 3.8+.

Usage:
    python packaging/smoke_test.py dist/MarkitForge.app/Contents/MacOS/MarkitForge
    python packaging/smoke_test.py dist\\MarkitForge\\MarkitForge.exe --port 8991
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
TEST_FILES = ROOT / "packages" / "markitforge" / "tests" / "test_files"

# (file, upload name, text the Markdown must contain). The extension-less
# upload makes the server identify the file by content (magika's model).
CASES: List[Tuple[str, str, str]] = [
    ("test.docx", "test.docx", "314b0a30-5b04-470b-b9f7-eed2c2bec74a"),
    ("test.pdf", "test.pdf", "contemporaneous exploration of multi-agent"),
    ("test.docx", "no-extension", "314b0a30-5b04-470b-b9f7-eed2c2bec74a"),
]


def _request(
    url: str,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 120,
) -> Tuple[int, bytes, Dict[str, str]]:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _multipart(filename: str, payload: bytes) -> Tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + payload
        + (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="options"\r\n\r\n'
            "{}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _await_job(
    base: str, job: Dict, headers: Dict[str, str], timeout: float
) -> Tuple[int, Dict]:
    """Follow a queued conversion (HTTP 202 + job id) until it finishes."""
    deadline = time.monotonic() + timeout
    while job.get("status") not in ("done", "error", "cancelled"):
        if time.monotonic() > deadline:
            return 504, {"error": "the conversion did not finish in time"}
        time.sleep(0.2)
        status, body, _ = _request(f"{base}/api/jobs/{job['job_id']}", headers=headers)
        if status != 200:
            return status, {"error": body[:300].decode("utf-8", "replace")}
        job = json.loads(body)
    if job["status"] != "done":
        return int(job.get("http_status") or 500), job.get("error") or job
    return 200, job.get("result") or {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", help="the app executable to start")
    parser.add_argument("--port", type=int, default=8991)
    parser.add_argument("--startup-timeout", type=float, default=120)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    command = [args.command, "--no-browser", "--port", str(args.port)]
    usage = subprocess.run(
        [args.command, "--help"], capture_output=True, text=True, timeout=120
    ).stdout
    if "--no-history" in usage:
        command.append("--no-history")
    log_dir = tempfile.TemporaryDirectory()
    log_path = Path(log_dir.name) / "app.log"
    log = open(log_path, "w", encoding="utf-8")

    def output() -> str:
        return log_path.read_text("utf-8", errors="replace")

    proc = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    failures: List[str] = []
    started = time.monotonic()
    try:
        while True:
            if proc.poll() is not None:
                print(output())
                print(f"FAIL: the app exited early with code {proc.returncode}")
                return 1
            # The app moves to a free port when the requested one is busy.
            found = re.search(r"running at (http://[^/\s]+)", output())
            if found:
                base = found.group(1)
            try:
                status, _, _ = _request(base + "/", timeout=2)
                if status == 200:
                    break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() - started > args.startup_timeout:
                print("FAIL: the app did not answer in time")
                return 1
            time.sleep(0.5)
        print(f"ok   started in {time.monotonic() - started:.1f}s at {base}")

        status, body, _ = _request(base + "/")
        if status == 200 and b"MarkitForge" in body:
            print(f"ok   GET / -> {status} ({len(body)} bytes)")
        else:
            failures.append(f"GET / -> {status}")
        found = re.search(rb'<meta name="mf-token" content="([^"]*)"', body)
        api: Dict[str, str] = {"X-MarkitForge-Client": "1"}
        if found and found.group(1):
            api["X-MarkitForge-Token"] = found.group(1).decode()
            print("ok   page carries a session token")

        status, body, _ = _request(base + "/static/app.js")
        if status == 200 and body:
            print(f"ok   GET /static/app.js -> {status}")
        else:
            failures.append(f"GET /static/app.js -> {status}")

        status, body, _ = _request(base + "/api/info", headers=api)
        if status == 200:
            print(f"ok   /api/info {json.loads(body)}")
        else:
            failures.append(f"GET /api/info -> {status}")

        for source, upload_name, expected in CASES:
            payload = (TEST_FILES / source).read_bytes()
            body, ctype = _multipart(upload_name, payload)
            t0 = time.monotonic()
            status, resp, _ = _request(
                base + "/api/convert",
                data=body,
                headers={"Content-Type": ctype, **api},
            )
            label = f"convert {source} as {upload_name!r}"
            if status == 202:  # queued: newer GUIs convert in the background
                status, data = _await_job(base, json.loads(resp), api, 120)
            elif status == 200:
                data = json.loads(resp)
            else:
                data = {"error": resp[:300].decode("utf-8", "replace")}
            took = time.monotonic() - t0
            if status != 200:
                failures.append(f"{label} -> {status}: {data}")
                continue
            markdown = data.get("markdown") or ""
            if expected not in markdown:
                failures.append(
                    f"{label}: expected text missing; got {markdown[:200]!r}"
                )
                continue
            print(f"ok   {label} -> {len(markdown)} chars of Markdown in {took:.1f}s")

        # Requests without the client header or the token must be refused.
        body, ctype = _multipart("test.pdf", b"%PDF-1.4")
        for missing in api:
            headers = {k: v for k, v in api.items() if k != missing}
            status, _, _ = _request(
                base + "/api/convert",
                data=body,
                headers={"Content-Type": ctype, **headers},
            )
            if status == 403:
                print(f"ok   request without {missing} -> 403")
            else:
                failures.append(f"request without {missing} -> {status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()

    if failures:
        print("---- app output ----")
        print(output())
        print("--------------------")
    for failure in failures:
        print(f"FAIL {failure}")
    print("PASSED" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
