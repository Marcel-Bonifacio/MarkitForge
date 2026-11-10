# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build the MarkitForge logo SVGs in assets/brand/.

The mark is an M over an anvil with a down arrow cut into it ("M down-arrow" is
the Markdown symbol; the anvil is the forge). The wordmark is Bitstream Vera
Sans (from reportlab) converted to outlines, so no font is needed to view it.

Usage: uv run --with fonttools --with reportlab python scripts/build_logo.py
"""

import pathlib
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

import reportlab

FONTS = pathlib.Path(reportlab.__file__).parent / "fonts"
OUT = pathlib.Path(__file__).resolve().parents[1] / "assets" / "brand"
OUT.mkdir(parents=True, exist_ok=True)
LICENSE_NOTE = "<!-- SPDX-FileCopyrightText: 2026-present MarkitForge contributors | SPDX-License-Identifier: Apache-2.0 -->"
BRAND = "#B87333"  # muted copper
INK_LIGHT, INK_DARK = "#1F2328", "#F0F3F6"

# ---- mark (128 grid) -------------------------------------------------------
M = "M36 52V14h14l14 18 14-18h14v38H79V35L64 54 49 35v17z"
ANVIL = (
    "M8 58H112l4 4v4l-4 4q-16 2-26 10l-2 12q4 6 12 8v12H76q-12-10-24 0H32V100"
    "q8-2 12-8l-2-12q-8-6-20-10Q12 66 8 58z"
)
ARROW = "M59 63h10v11h8L64 89 51 74h8z"  # cut out of the anvil (evenodd)


def mark_group(color, dx=0, dy=0, s=1.0):
    t = f' transform="translate({dx} {dy}) scale({s})"' if (dx or dy or s != 1) else ""
    return (
        f'<g fill="{color}"{t}><path d="{M}"/>'
        f'<path fill-rule="evenodd" d="{ANVIL} {ARROW}"/></g>'
    )


# ---- wordmark --------------------------------------------------------------
def text_path(font_file, text, x0, baseline, size, tracking=0):
    f = TTFont(FONTS / font_file)
    gs = f.getGlyphSet()
    cmap = f.getBestCmap()
    upm = f["head"].unitsPerEm
    scale = size / upm
    kern = {}
    if "kern" in f:
        for sub in f["kern"].kernTables:
            kern.update(sub.kernTable)
    pen = SVGPathPen(gs)
    x = x0
    prev = None
    for ch in text:
        g = cmap[ord(ch)]
        if prev and (prev, g) in kern:
            x += kern[(prev, g)] * scale
        tp = TransformPen(pen, (scale, 0, 0, -scale, x, baseline))
        gs[g].draw(tp)
        x += gs[g].width * scale + tracking
        prev = g
    return pen.getCommands(), x


def wordmark(ink, x0, baseline, size):
    d1, x = text_path("Vera.ttf", "Markit", x0, baseline, size, tracking=-0.6)
    d2, x = text_path("VeraBd.ttf", "Forge", x + 2, baseline, size, tracking=-0.8)
    return (f'<path fill="{ink}" d="{d1}"/><path fill="{BRAND}" d="{d2}"/>'), x


def svg(w, h, body, title):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" '
        f'role="img" aria-label="{title}">{LICENSE_NOTE}<title>{title}</title>{body}</svg>\n'
    )


# mark alone
(OUT / "markitforge-mark.svg").write_text(
    svg(128, 128, mark_group(BRAND), "MarkitForge")
)

# horizontal lockups: mark 128 tall, wordmark cap height ~ 52% of mark
for name, ink in (("light", INK_LIGHT), ("dark", INK_DARK)):
    wm, xend = wordmark(ink, 150, 92, 74)
    w = xend + 8
    (OUT / f"markitforge-logo-{name}.svg").write_text(
        svg(w, 128, mark_group(BRAND) + wm, "MarkitForge")
    )

# favicon / app icon: mark on a rounded dark tile
tile = '<rect width="128" height="128" rx="28" fill="#17191E"/>' + mark_group(
    BRAND, dx=14, dy=12, s=0.79
)
(OUT / "markitforge-icon.svg").write_text(svg(128, 128, tile, "MarkitForge"))
print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.svg"))))
