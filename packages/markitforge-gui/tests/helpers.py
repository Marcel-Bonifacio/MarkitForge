# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared test helpers: a token-aware client and job polling."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.testclient import TestClient

from markitforge_gui._app import CLIENT_HEADER, TOKEN_HEADER

TEST_FILES = (
    Path(__file__).resolve().parents[2] / "markitforge" / "tests" / "test_files"
)
HEADERS = {CLIENT_HEADER: "1"}


def TC(app: Any, base_url: str = "http://127.0.0.1", **kwargs: Any) -> TestClient:
    """A TestClient that sends this app's session token on every request."""
    headers = {TOKEN_HEADER: app.state.token, **kwargs.pop("headers", {})}
    return TestClient(app, base_url=base_url, headers=headers, **kwargs)


class JobResult:
    """Looks like a response: what the old synchronous endpoint returned."""

    def __init__(self, status_code: int, body: Dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> Dict[str, Any]:
        return self._body


def wait_job(client: TestClient, res: Any, timeout: float = 90) -> Any:
    """Follow a 202 job response until it finishes."""
    if res.status_code != 202:
        return res
    job_id = res.json()["job_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] == "done":
            return JobResult(200, data["result"])
        if data["status"] == "error":
            return JobResult(data["http_status"], data["error"])
        if data["status"] == "cancelled":
            return JobResult(409, {"error": "cancelled", "code": "cancelled"})
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def upload(
    client: TestClient,
    name: str,
    data: Optional[bytes] = None,
    options: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    rel_path: Optional[str] = None,
    wait: bool = True,
) -> Any:
    if data is None:
        data = (TEST_FILES / name).read_bytes()
    form: Dict[str, str] = {"options": json.dumps(options or {})}
    if rel_path is not None:
        form["rel_path"] = rel_path
    res = client.post(
        "/api/convert",
        files={"file": (name, data, "application/octet-stream")},
        data=form,
        headers=HEADERS if headers is None else headers,
    )
    return wait_job(client, res) if wait else res
