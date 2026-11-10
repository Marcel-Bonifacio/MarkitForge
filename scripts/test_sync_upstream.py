# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/sync_upstream.py, using throwaway git repositories."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "sync_upstream", Path(__file__).with_name("sync_upstream.py")
)
sync_upstream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_upstream)  # type: ignore[union-attr]


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_rename_text_keeps_upstream_urls():
    src = "from markitdown import MarkItDown  # see https://github.com/microsoft/markitdown"
    out = sync_upstream.rename_text(src)
    assert out == (
        "from markitforge import MarkitForge  # see https://github.com/microsoft/markitdown"
    )
    assert sync_upstream.rename_path(
        "packages/markitdown/src/markitdown/_markitdown.py"
    ) == ("packages/markitforge/src/markitforge/_markitforge.py")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A fork repo whose `upstream` history uses markitdown names."""
    r = tmp_path / "fork"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    up = r / "packages/markitdown/src/markitdown"
    up.mkdir(parents=True)
    (up / "a.py").write_text("import markitdown\nx = 1\ny = 2\nz = 3\n")
    (up / "gone.py").write_text("old\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "upstream base")
    base = git(r, "rev-parse", "HEAD")
    # upstream moves on
    (up / "a.py").write_text("import markitdown\nx = 1\ny = 20\nz = 3\n")
    (up / "new.py").write_text("from markitdown import MarkItDown\n")
    (up / "gone.py").unlink()
    git(r, "add", "-A")
    git(r, "commit", "-qm", "upstream change")
    ref = git(r, "rev-parse", "HEAD")
    # the fork: renamed, with its own change to a.py
    git(r, "checkout", "-q", base)
    git(r, "checkout", "-q", "-b", "fork")
    subprocess.run(["rm", "-rf", "packages/markitdown"], cwd=r, check=True)
    ours = r / "packages/markitforge/src/markitforge"
    ours.mkdir(parents=True)
    (ours / "a.py").write_text(
        "# fork header\nimport markitforge\nx = 1\ny = 2\nz = 3\n"
    )
    (ours / "gone.py").write_text("old\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "fork")
    monkeypatch.setattr(sync_upstream, "REPO", r)
    monkeypatch.setattr(sync_upstream, "STATE", r / ".upstream-sync")
    return r, base, ref


def test_sync_merges_adds_and_deletes(repo):
    r, base, ref = repo
    code = sync_upstream.main(["--base", base, "--ref", ref])
    assert code == 0
    ours = r / "packages/markitforge/src/markitforge"
    assert (
        ours / "a.py"
    ).read_text() == "# fork header\nimport markitforge\nx = 1\ny = 20\nz = 3\n"
    assert (ours / "new.py").read_text() == "from markitforge import MarkitForge\n"
    assert not (ours / "gone.py").exists()
    assert (r / ".upstream-sync").read_text().strip() == ref


def test_sync_reports_conflicts(repo):
    r, base, ref = repo
    a = r / "packages/markitforge/src/markitforge/a.py"
    a.write_text("# fork header\nimport markitforge\nx = 1\ny = 99\nz = 3\n")
    git(r, "commit", "-qam", "fork edits y")
    code = sync_upstream.main(["--base", base, "--ref", ref])
    assert code == 1
    assert "<<<<<<< markitforge" in a.read_text()
    assert not (r / ".upstream-sync").exists()


def test_dry_run_changes_nothing(repo):
    r, base, ref = repo
    before = git(r, "status", "--porcelain")
    assert sync_upstream.main(["--base", base, "--ref", ref, "--dry-run"]) == 0
    assert git(r, "status", "--porcelain") == before
