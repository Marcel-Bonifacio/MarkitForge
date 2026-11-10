# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Keep untrusted text (document content, LLM answers) from changing the
structure of the Markdown MarkitForge emits.

Document text and model output are data: a caption must stay inside its
image alt text, a LaTeX answer must stay inside its ``$$`` block, and page
text must not be able to forge the ``<!-- page N -->`` markers that tools
use to split the output.
"""

import re
import unicodedata
from typing import Optional

MAX_ALT_TEXT = 300
MAX_LATEX = 2000

# --------------------------------------------------------------- alt text

_ALT_DROP_RE = re.compile(r"[\\\[\]<>`]")


def safe_alt_text(text: Optional[str], limit: int = MAX_ALT_TEXT) -> str:
    """Make text safe for ``![...](...)``: one line, no brackets, backslashes,
    angle brackets or backticks, at most ``limit`` characters."""
    if not text:
        return ""
    # Control and format characters (newlines, zero-width joiners, bidi
    # overrides, ...) become spaces.
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in text)
    text = _ALT_DROP_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip()
    return text


# --------------------------------------------------------------- LaTeX

# Substrings that could close the math block or start Markdown/HTML.
_LATEX_FORBIDDEN = ("$", "<", ">", "`", "](")
_LATEX_ASCII_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " \t\n"
    "\\{}[]()^_+-*/=.,;:!?|'\"&~@%#"
)
# MathJax extensions that can create links, load code or inject HTML/CSS,
# and TeX primitives that only make sense for side effects.
_LATEX_COMMAND_DENY_RE = re.compile(
    r"\\(?:href|url|includegraphics|input|include|require|class|cssId|style"
    r"|html[A-Za-z]*|unicode|write|immediate|openin|openout|catcode|def|let"
    r"|newcommand|renewcommand)(?![A-Za-z])"
)


def _latex_char_ok(ch: str) -> bool:
    if ch in _LATEX_ASCII_OK:
        return True
    if ord(ch) < 128:
        return False
    # Non-ASCII letters, digits and math symbols (Greek, ≤, ∑, ...).
    category = unicodedata.category(ch)
    return category[0] in ("L", "N") or category == "Sm"


def safe_latex(latex: str, limit: int = MAX_LATEX) -> Optional[str]:
    """Validate an LLM's LaTeX answer for a ``$$`` block.

    Returns the formula on a single line, ``""`` for an empty answer, or
    ``None`` when the answer could leave the math block (``$``, HTML, links,
    code spans, headings, blank lines), is too long, or uses characters or
    commands a formula has no need for.
    """
    text = latex.strip()
    if not text:
        return ""
    if limit > 0 and len(text) > limit:
        return None
    if any(bad in text for bad in _LATEX_FORBIDDEN):
        return None
    lines = text.split("\n")
    if any(not line.strip() for line in lines):
        return None  # a blank line ends the block in Markdown
    if any(line.lstrip().startswith("#") for line in lines):
        return None
    if not all(_latex_char_ok(ch) for ch in text):
        return None
    if _LATEX_COMMAND_DENY_RE.search(text):
        return None
    return " ".join(text.split())


# --------------------------------------------------------------- page markers

# Lines that look like the markers join_sections() writes:
# <!-- page 3 -->, <!-- slide 2 -->, <!-- sheet 1 -->, <!-- chapter 4 -->.
_MARKER_LINE_RE = re.compile(
    r"^([ \t]*)<!--(?=[ \t]*(?:page|slide|sheet|chapter)[ \t]*\d)",
    re.IGNORECASE | re.MULTILINE,
)


def neutralize_page_markers(text: str) -> str:
    """Escape document lines that would read as MarkitForge page markers
    (``<!-- page 7 -->`` becomes ``&lt;!-- page 7 -->``)."""
    if "<!--" not in text:
        return text
    return _MARKER_LINE_RE.sub(r"\1&lt;!--", text)
