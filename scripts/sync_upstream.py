# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bring upstream MarkItDown changes into MarkitForge.

Every file in MarkitForge was renamed (markitdown -> markitforge), so a plain
`git merge upstream/main` conflicts everywhere. This script instead, for each
file upstream changed since the last sync:

1. takes the upstream version at the last synced commit (base) and at the new
   commit (theirs), and applies the same rename to both (paths and text);
2. three-way merges them into our file with `git merge-file`, so our own
   changes are kept and real conflicts get the usual <<<<<<< markers.

The last synced upstream commit is stored in `.upstream-sync`.

Usage:
    python scripts/sync_upstream.py --fetch              # merge upstream/main
    python scripts/sync_upstream.py --ref upstream/main --dry-run
    python scripts/sync_upstream.py --ref <sha> --commit # commit if no conflicts

Run it on a clean working tree, on a new branch:
    git switch -c sync/upstream-$(date +%Y%m%d)
"""

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / ".upstream-sync"
UPSTREAM_URL = "https://github.com/microsoft/markitdown.git"

# Text that must keep pointing at upstream after the rename.
_PROTECT = (
    "raw.githubusercontent.com/microsoft/markitdown",
    "github.com/microsoft/markitdown",
)


def rename_text(text: str) -> str:
    """The same rename the fork applied: markitdown -> markitforge."""
    for i, p in enumerate(_PROTECT):
        text = text.replace(p, f"\0P{i}\0")
    text = (
        text.replace("MarkItDownException", "MarkitForgeException")
        .replace("MarkItDown", "MarkitForge")
        .replace("Markitdown", "MarkitForge")
        .replace("MARKITDOWN", "MARKITFORGE")
        .replace("markitdown", "markitforge")
    )
    for i, p in enumerate(_PROTECT):
        text = text.replace(f"\0P{i}\0", p)
    return text


def rename_path(path: str) -> str:
    return path.replace("markitdown", "markitforge")


def git(
    *args: str, check: bool = True, binary: bool = False
) -> "subprocess.CompletedProcess":
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=check,
        capture_output=True,
        text=not binary,
    )


def blob(ref: str, path: str) -> Optional[bytes]:
    r = git("show", f"{ref}:{path}", check=False, binary=True)
    return r.stdout if r.returncode == 0 else None


def as_text(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    if b"\0" in data[:8000]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass
class Report:
    merged: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    conflicts: List[Tuple[str, str]] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)


def merge_file(ours: str, base: str, theirs: str) -> Tuple[str, bool]:
    """Three-way merge text with git merge-file. Returns (text, had_conflicts)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = Path(tmp) / name
            p.write_text(content, encoding="utf-8")
            paths.append(str(p))
        r = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "markitforge",
                "-L",
                "upstream-base",
                "-L",
                "upstream",
                *paths,
            ],
            capture_output=True,
        )
        if r.returncode < 0 or r.returncode > 127:
            raise RuntimeError(r.stderr.decode(errors="replace"))
        return r.stdout.decode("utf-8"), r.returncode > 0


def changes(base: str, ref: str) -> List[Tuple[str, str, Optional[str]]]:
    """(status, path, new_path) for each upstream change between base and ref."""
    out = git("diff", "--name-status", "-M90", base, ref).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            rows.append(("R", parts[1], parts[2]))
        else:
            rows.append((status, parts[1], None))
    return rows


def sync(base: str, ref: str, *, dry_run: bool) -> Report:
    rep = Report()

    def write(path: str, data: bytes) -> None:
        if dry_run:
            return
        target = REPO / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def merge_one(up_old: str, up_new: str) -> None:
        ours_path = rename_path(up_new)
        old_ours_path = rename_path(up_old)
        base_raw = blob(base, up_old)
        theirs_raw = blob(ref, up_new)
        ours_raw = (
            (REPO / old_ours_path).read_bytes()
            if (REPO / old_ours_path).is_file()
            else None
        )
        base_t, theirs_t, ours_t = (
            as_text(base_raw),
            as_text(theirs_raw),
            as_text(ours_raw),
        )

        if theirs_raw is None:  # deleted upstream
            if ours_raw is None:
                rep.unchanged.append(ours_path)
                return
            expected = rename_text(base_t) if base_t is not None else base_raw
            current = ours_t if ours_t is not None else ours_raw
            if current == expected:
                rep.deleted.append(old_ours_path)
                if not dry_run:
                    (REPO / old_ours_path).unlink()
            else:
                rep.conflicts.append((old_ours_path, "changed here, deleted upstream"))
            return

        if theirs_t is None:  # binary
            if ours_raw is None or ours_raw == base_raw:
                write(ours_path, theirs_raw)
                (rep.added if ours_raw is None else rep.merged).append(ours_path)
            elif ours_raw == theirs_raw:
                rep.unchanged.append(ours_path)
            else:
                rep.conflicts.append((ours_path, "binary file changed on both sides"))
            return

        theirs_r = rename_text(theirs_t)
        if ours_raw is None:
            if base_raw is None:
                write(ours_path, theirs_r.encode("utf-8"))
                rep.added.append(ours_path)
            else:
                rep.conflicts.append((ours_path, "deleted here, changed upstream"))
            return
        if ours_t is None:
            rep.conflicts.append((ours_path, "upstream text file is binary here"))
            return
        base_r = rename_text(base_t) if base_t is not None else ""
        merged, conflicted = merge_file(ours_t, base_r, theirs_r)
        if merged == ours_t:
            rep.unchanged.append(ours_path)
            return
        write(ours_path, merged.encode("utf-8"))
        if old_ours_path != ours_path and not dry_run:
            (REPO / old_ours_path).unlink(missing_ok=True)
        if conflicted:
            rep.conflicts.append((ours_path, "conflict markers written"))
        else:
            rep.merged.append(ours_path)

    for status, path, new_path in changes(base, ref):
        merge_one(path, new_path or path)
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--ref", default="upstream/main", help="Upstream commit to sync to."
    )
    ap.add_argument(
        "--base", help="Last synced upstream commit (default: .upstream-sync)."
    )
    ap.add_argument("--fetch", action="store_true", help="git fetch upstream first.")
    ap.add_argument(
        "--dry-run", action="store_true", help="Report only; change nothing."
    )
    ap.add_argument(
        "--commit", action="store_true", help="Commit when there are no conflicts."
    )
    args = ap.parse_args(argv)

    if args.fetch:
        if git("remote", "get-url", "upstream", check=False).returncode != 0:
            git("remote", "add", "upstream", UPSTREAM_URL)
        git("fetch", "upstream")
    base = args.base or (STATE.read_text().strip() if STATE.exists() else None)
    if not base:
        print("No base commit: pass --base or create .upstream-sync.", file=sys.stderr)
        return 2
    ref = git("rev-parse", args.ref).stdout.strip()
    base = git("rev-parse", base).stdout.strip()
    if not args.dry_run and git("status", "--porcelain").stdout.strip():
        print("Working tree is not clean; commit or stash first.", file=sys.stderr)
        return 2
    if base == ref:
        print(f"Already in sync with {ref[:9]}.")
        return 0

    commits = git("log", "--oneline", f"{base}..{ref}").stdout.strip().splitlines()
    print(f"Upstream {base[:9]}..{ref[:9]}: {len(commits)} commit(s)")
    rep = sync(base, ref, dry_run=args.dry_run)
    for label, items in (
        ("merged", rep.merged),
        ("added", rep.added),
        ("deleted", rep.deleted),
    ):
        for p in items:
            print(f"  {label:8} {p}")
    for p, why in rep.conflicts:
        print(f"  CONFLICT {p}: {why}")
    print(
        f"{len(rep.merged)} merged, {len(rep.added)} added, {len(rep.deleted)} deleted, "
        f"{len(rep.conflicts)} conflict(s), {len(rep.unchanged)} already up to date."
    )
    if args.dry_run:
        return 1 if rep.conflicts else 0
    if rep.conflicts:
        print(
            "Resolve the conflicts, run the tests, then: "
            f"echo {ref} > .upstream-sync && git add -A && git commit",
            file=sys.stderr,
        )
        return 1
    STATE.write_text(ref + "\n")
    if args.commit:
        git("add", "-A")
        msg = f"Sync upstream MarkItDown {ref[:9]}\n\n" + "\n".join(
            f"- {c}" for c in commits[:50]
        )
        git("commit", "-m", msg)
        print("Committed.")
    else:
        print("Done. Review with `git diff`, run the tests, then commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
