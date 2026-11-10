# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Write THIRD_PARTY_LICENSES.txt for the packaged desktop app.

Run it with the build environment's Python, before PyInstaller:

    python packaging/collect_licenses.py --out build/THIRD_PARTY_LICENSES.txt

The file starts with MarkitForge's own LICENSE, LICENSE-APACHE and NOTICE and a
summary (copyleft licenses, packages without a license file), followed by one
section per distribution with its name, version, declared license and the
full text of every license file it ships.

Which distributions are listed: the dependency closure of the packages the
app is built from (``markitforge-gui[window]``, ``markitforge[all]`` and the
runtime extras in ``requirements-build.txt``), following only requirements that
are installed in this environment and whose extras were requested. Build-only
tools (PyInstaller itself, pip, setuptools) are not part of the bundle and are
left out, except PyInstaller's bootloader, which is the app's executable.
The CPython license is included too, since the interpreter is bundled.

Standard library only (``importlib.metadata``).
"""

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

PACKAGING = Path(__file__).resolve().parent
ROOT = PACKAGING.parent
ROOTS = ["markitforge-gui[window]", "markitforge[all]"]
REQUIREMENTS = PACKAGING / "requirements-build.txt"
# In the build environment but not in the bundle (see the spec's excludes).
BUILD_ONLY = {
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "altgraph",
    "macholib",
    "pefile",
    "pywin32-ctypes",
    "pip",
    "setuptools",
    "wheel",
    "pytest",
}
LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|copyright)", re.IGNORECASE)
COPYLEFT = re.compile(
    r"\b(A?GPL|LGPL|GNU (Lesser |Library |Affero )?General Public)", re.I
)
WEAK_COPYLEFT = re.compile(r"\b(MPL|Mozilla Public|EPL|Eclipse Public|CDDL)\b", re.I)
RULE = "=" * 79


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _split_requirement(text: str) -> Tuple[str, Set[str], str]:
    """``name[extra1,extra2] (>=1) ; marker`` -> (name, extras, marker)."""
    req, _, marker = text.partition(";")
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[([^\]]*)\])?", req)
    if not m:
        raise ValueError(f"Cannot parse requirement {text!r}")
    extras = {_norm(e) for e in (m.group(3) or "").split(",") if e.strip()}
    return m.group(1), extras, marker.strip()


def _marker_extras(marker: str) -> Set[str]:
    return {_norm(e) for e in re.findall(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)}


def _installed(name: str) -> Optional[metadata.Distribution]:
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None


def _roots() -> List[str]:
    roots = list(ROOTS)
    if REQUIREMENTS.exists():
        for line in REQUIREMENTS.read_text("utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and _norm(_split_requirement(line)[0]) not in BUILD_ONLY:
                roots.append(line)
    return roots


def closure(roots: Iterable[str]) -> Dict[str, metadata.Distribution]:
    """Installed distributions needed by ``roots`` (with requested extras)."""
    found: Dict[str, metadata.Distribution] = {}
    seen: Set[Tuple[str, frozenset]] = set()
    todo: List[Tuple[str, Set[str]]] = []
    for root in roots:
        name, extras, _ = _split_requirement(root)
        todo.append((name, extras))
    while todo:
        name, extras = todo.pop()
        key = (_norm(name), frozenset(extras))
        if key in seen or _norm(name) in BUILD_ONLY:
            continue
        seen.add(key)
        dist = _installed(name)
        if dist is None:
            continue
        found[_norm(dist.metadata["Name"])] = dist
        for requirement in dist.requires or []:
            req_name, req_extras, marker = _split_requirement(requirement)
            needed = _marker_extras(marker)
            if needed and not (needed & extras):
                continue  # belongs to an extra that was not requested
            # Platform markers are not evaluated: a requirement that does not
            # apply here is simply not installed, and skipped above.
            todo.append((req_name, req_extras))
    return found


def license_files(dist: metadata.Distribution) -> List[Tuple[str, str]]:
    """(relative path, text) of every license-like file the distribution ships."""
    out: List[Tuple[str, str]] = []
    for item in dist.files or []:
        parts = item.parts
        in_dist_info = parts and parts[0].endswith(".dist-info")
        in_license_dir = (
            in_dist_info
            and len(parts) > 2
            and parts[1].lower()
            in (
                "licenses",
                "license_files",
            )
        )
        if not (in_license_dir or LICENSE_NAME.match(parts[-1])):
            continue
        path = Path(str(item.locate()))
        if not path.is_file():
            continue
        try:
            text = path.read_text("utf-8")
        except UnicodeDecodeError:
            text = path.read_text("latin-1")
        out.append(("/".join(parts), text.strip()))
    return sorted(out)


def declared_license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    field = (meta.get("License") or "").strip()
    if field and "\n" not in field and len(field) <= 100:
        classifiers.insert(0, field)
    elif field:
        classifiers.insert(0, field.splitlines()[0][:100] + " (see text)")
    return "; ".join(dict.fromkeys(classifiers)) or "UNKNOWN"


def _python_license() -> Tuple[str, str]:
    candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "LICENSE.txt",
    ]
    for path in candidates:
        if path.is_file():
            return str(path.name), path.read_text("utf-8").strip()
    return "", ""


def _section(title: str, body: str) -> str:
    return f"{RULE}\n{title}\n{RULE}\n\n{body.rstrip()}\n\n"


def build() -> Tuple[str, Dict[str, List[str]]]:
    dists = closure(_roots())
    own = {_norm("markitforge"), _norm("markitforge-gui")}
    entries = []
    copyleft: List[str] = []
    weak: List[str] = []
    text_mentions: List[str] = []
    missing: List[str] = []
    for key in sorted(dists):
        dist = dists[key]
        name, version = dist.metadata["Name"], dist.version
        declared = declared_license(dist)
        files = license_files(dist)
        label = f"{name} {version} ({declared})"
        if COPYLEFT.search(declared):
            copyleft.append(label)
        elif WEAK_COPYLEFT.search(declared):
            weak.append(label)
        elif any(COPYLEFT.search(text) for _, text in files):
            text_mentions.append(label)
        if not files:
            missing.append(label)
        entries.append((name, version, declared, files, key in own))

    # The app's executable is PyInstaller's bootloader.
    pyi = _installed("pyinstaller")
    bootloader = None
    if pyi is not None:
        texts = [t for p, t in license_files(pyi) if "COPYING" in p.upper()]
        bootloader = (pyi.version, texts[0] if texts else "")
        copyleft.append(
            f"PyInstaller bootloader {pyi.version} (GPL-2.0-or-later WITH "
            "Bootloader-exception: may be distributed with any program)"
        )

    py_file, py_text = _python_license()
    out: List[str] = []
    out.append(
        "MarkitForge desktop app: licenses\n\n"
        "This file lists MarkitForge's own license and the licenses of the\n"
        "third-party software bundled with the app.\n\n"
    )
    for name in ("LICENSE", "LICENSE-APACHE", "NOTICE"):
        path = ROOT / name
        if path.exists():
            out.append(_section(f"MarkitForge: {name}", path.read_text("utf-8")))

    summary = [
        f"Bundled distributions: {len(entries)} (plus CPython and the bootloader)",
        "",
    ]
    summary.append("GPL / LGPL / AGPL (declared license):")
    summary += [f"  - {x}" for x in copyleft] or ["  (none)"]
    summary.append("")
    summary.append("Other weak copyleft (MPL, EPL, CDDL; declared license):")
    summary += [f"  - {x}" for x in weak] or ["  (none)"]
    summary.append("")
    summary.append(
        "License text mentions GPL/LGPL but the declared license does not\n"
        "(usually a bundled component or a dual-license option; check the text):"
    )
    summary += [f"  - {x}" for x in text_mentions] or ["  (none)"]
    summary.append("")
    summary.append("No license file found in the distribution:")
    summary += [f"  - {x}" for x in missing] or ["  (none)"]
    out.append(_section("Summary", "\n".join(summary)))

    if py_text:
        out.append(
            _section(f"Python {sys.version.split()[0]} (PSF-2.0) - {py_file}", py_text)
        )
    if bootloader is not None:
        version, text = bootloader
        out.append(
            _section(
                f"PyInstaller bootloader {version} "
                "(GPL-2.0-or-later WITH Bootloader-exception)",
                text or "(COPYING.txt not found)",
            )
        )
    for name, version, declared, files, is_own in entries:
        if is_own:
            body = f"License: {declared}\n\n(MarkitForge itself; see the top of this file.)"
            out.append(_section(f"{name} {version}", body))
            continue
        parts = [f"License: {declared}"]
        if not files:
            parts.append("\nNo license file was found in this distribution.")
        for path, text in files:
            parts.append(f"\n----- {path} -----\n\n{text}")
        out.append(_section(f"{name} {version}", "\n".join(parts)))

    report = {
        "copyleft": copyleft,
        "weak": weak,
        "text_mentions": text_mentions,
        "missing": missing,
        "count": [str(len(entries))],
    }
    return "".join(out), report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=PACKAGING / "build" / "THIRD_PARTY_LICENSES.txt",
    )
    args = parser.parse_args()
    text, report = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"{args.out}: {report['count'][0]} distributions")
    for key, title in (
        ("copyleft", "GPL/LGPL/AGPL"),
        ("weak", "weak copyleft"),
        ("text_mentions", "GPL/LGPL mentioned in text"),
        ("missing", "no license file"),
    ):
        for item in report[key]:
            print(f"  {title}: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
