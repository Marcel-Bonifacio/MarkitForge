# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regenerate docs/FORMATS.md from markitforge/_formats.py.

Usage: python scripts/gen_formats_doc.py [--check]
--check exits 1 when the file is out of date (for CI).
"""

import sys
from pathlib import Path

from markitforge._formats import format_markdown

target = Path(__file__).resolve().parents[1] / "docs" / "FORMATS.md"
text = format_markdown()
if "--check" in sys.argv:
    if not target.exists() or target.read_text(encoding="utf-8") != text:
        print(f"{target} is out of date. Run: python scripts/gen_formats_doc.py")
        sys.exit(1)
    print("docs/FORMATS.md is up to date.")
else:
    target.write_text(text, encoding="utf-8")
    print(f"Wrote {target}")
