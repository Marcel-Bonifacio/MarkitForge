# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rasterize assets/brand/markitforge-icon.svg into app icons.

Writes, into the output directory (default: packaging/build/icons):

* ``markitforge-1024.png``  master image
* ``markitforge.ico``       Windows icon (16-256 px)
* ``markitforge.icns``      macOS icon (via ``iconutil`` when available,
                            otherwise Pillow's ICNS writer)

Only Pillow is needed (it is already a dependency of markitforge[all]). The
SVG is not rendered by a general SVG engine: this script understands exactly
what the icon uses (a rounded ``rect`` and a ``g`` with ``translate``/``scale``
holding filled paths made of M/L/H/V/Q/Z commands) and draws it with
supersampling. If the icon gains other SVG features, extend ``_parse_path``.

Usage: python packaging/make_icons.py [--out DIR]
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "assets" / "brand" / "markitforge-icon.svg"
NS = "{http://www.w3.org/2000/svg}"
SIZE = 1024
SUPERSAMPLE = 4

Point = Tuple[float, float]


def _parse_path(d: str) -> List[List[Point]]:
    """Return the closed sub-paths of an SVG path as point lists."""
    tokens = re.findall(r"[MmLlHhVvQqZz]|-?\d*\.?\d+(?:e-?\d+)?", d)
    subpaths: List[List[Point]] = []
    current: List[Point] = []
    x = y = 0.0
    start = (0.0, 0.0)
    cmd = ""
    i = 0

    def num() -> float:
        nonlocal i
        value = float(tokens[i])
        i += 1
        return value

    while i < len(tokens):
        tok = tokens[i]
        if re.fullmatch(r"[A-Za-z]", tok):
            cmd = tok
            i += 1
            if cmd in "Zz":
                if current:
                    subpaths.append(current)
                current = []
                x, y = start
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            if current:
                subpaths.append(current)
            current = [(x, y)]
            start = (x, y)
            cmd = "l" if rel else "L"  # implicit lineto after moveto
        elif c == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            current.append((x, y))
        elif c == "H":
            nx = num()
            x = x + nx if rel else nx
            current.append((x, y))
        elif c == "V":
            ny = num()
            y = y + ny if rel else ny
            current.append((x, y))
        elif c == "Q":
            cx, cy, ex, ey = num(), num(), num(), num()
            if rel:
                cx, cy, ex, ey = x + cx, y + cy, x + ex, y + ey
            x0, y0 = x, y
            for step in range(1, 17):
                t = step / 16
                mt = 1 - t
                current.append(
                    (
                        mt * mt * x0 + 2 * mt * t * cx + t * t * ex,
                        mt * mt * y0 + 2 * mt * t * cy + t * t * ey,
                    )
                )
            x, y = ex, ey
        else:  # pragma: no cover - guarded by the tokenizer
            raise ValueError(f"Unsupported path command {cmd!r}")
    if current:
        subpaths.append(current)
    return subpaths


def _parse_transform(value: str) -> Tuple[float, float, float]:
    dx = dy = 0.0
    scale = 1.0
    m = re.search(r"translate\(\s*([-\d.]+)[\s,]+([-\d.]+)\s*\)", value or "")
    if m:
        dx, dy = float(m.group(1)), float(m.group(2))
    m = re.search(r"scale\(\s*([-\d.]+)\s*\)", value or "")
    if m:
        scale = float(m.group(1))
    return dx, dy, scale


def render(size: int = SIZE) -> Image.Image:
    root = ET.parse(SVG).getroot()
    vb = [float(v) for v in root.get("viewBox", "0 0 128 128").split()]
    big = size * SUPERSAMPLE
    k = big / vb[2]
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    rect = root.find(f"{NS}rect")
    background = "#000000"
    if rect is not None:
        background = rect.get("fill", background)
        radius = float(rect.get("rx", "0")) * k
        draw.rounded_rectangle(
            (0, 0, float(rect.get("width")) * k - 1, float(rect.get("height")) * k - 1),
            radius=radius,
            fill=background,
        )

    for group in root.iter(f"{NS}g"):
        dx, dy, s = _parse_transform(group.get("transform", ""))
        color = group.get("fill", "#000000")
        for path in group.iter(f"{NS}path"):
            fill = path.get("fill", color)
            subpaths = _parse_path(path.get("d", ""))
            for n, sub in enumerate(subpaths):
                pts = [((dx + px * s) * k, (dy + py * s) * k) for px, py in sub]
                # The icon's even-odd paths are an outline plus holes cut
                # out of it: paint holes with the background colour.
                hole = n > 0 and path.get("fill-rule") == "evenodd"
                draw.polygon(pts, fill=background if hole else fill)

    return img.resize((size, size), Image.LANCZOS)


def write_icns(master: Image.Image, target: Path) -> None:
    # macOS icons leave a transparent margin around the shape (the 824/1024
    # grid), otherwise the icon looks oversized in the Dock and Finder.
    inner = round(SIZE * 824 / 1024)
    padded = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    offset = (SIZE - inner) // 2
    padded.paste(master.resize((inner, inner), Image.LANCZOS), (offset, offset))
    master = padded
    iconutil = shutil.which("iconutil")
    if iconutil and sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "markitforge.iconset"
            iconset.mkdir()
            for base in (16, 32, 128, 256, 512):
                master.resize((base, base), Image.LANCZOS).save(
                    iconset / f"icon_{base}x{base}.png"
                )
                master.resize((base * 2, base * 2), Image.LANCZOS).save(
                    iconset / f"icon_{base}x{base}@2x.png"
                )
            subprocess.run(
                [iconutil, "-c", "icns", str(iconset), "-o", str(target)], check=True
            )
            return
    master.save(target, format="ICNS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).resolve().parent / "build" / "icons"
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    master = render()
    png = args.out / "markitforge-1024.png"
    master.save(png)
    master.save(
        args.out / "markitforge.ico",
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )
    write_icns(master, args.out / "markitforge.icns")
    for name in ("markitforge-1024.png", "markitforge.ico", "markitforge.icns"):
        print(args.out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
