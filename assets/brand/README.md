# MarkitForge brand assets

| File | Use |
|---|---|
| `markitforge-logo-light.svg` | Horizontal logo for light backgrounds |
| `markitforge-logo-dark.svg` | Horizontal logo for dark backgrounds |
| `markitforge-mark.svg` | The mark alone (transparent) |
| `markitforge-icon.svg` | App icon / favicon (mark on a dark rounded tile) |

Brand copper: `#B87333`. Ink: `#1F2328` (light), `#F0F3F6` (dark).

The mark is an **M** over an **anvil** with a **down arrow** cut into it: "M↓" is
the Markdown symbol, and the anvil is the forge. Regenerate everything with:

```bash
uv run --with fonttools --with reportlab python scripts/build_logo.py
```

The wordmark uses Bitstream Vera Sans (Copyright © 2003 Bitstream, Inc.,
[Bitstream Vera license](https://www.gnome.org/fonts/)), converted to outlines.
The logo files are Apache-2.0, like the rest of MarkitForge's own code.
