# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Text lines, heading inference and reading-order rendering for PDF pages."""

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# A line must be at least this much larger than the body text to become a
# heading. Kept high on purpose: a false heading is worse than a missed one.
# 1.15 catches LaTeX-style section titles (12pt over a 10pt body).
HEADING_MIN_RATIO = 1.15
HEADING_MAX_CHARS = 100
HEADING_MAX_WORDS = 14
MAX_HEADING_LEVEL = 3

_BOLD_RE = re.compile(r"bold|black|heavy|demi", re.IGNORECASE)
_MATH_FONT_RE = re.compile(
    r"CMMI|CMSY|CMEX|CMBSY|MSAM|MSBM|EUFM|EUSM|MathJax|Math", re.IGNORECASE
)
_PAGE_NUMBER_RE = re.compile(
    r"(page\s*)?\d+(\s*(of|/)\s*\d+)?"
    r"|m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})",
    re.IGNORECASE,
)


def is_bold_font(fontname: str) -> bool:
    """Whether a PDF font name denotes a bold weight."""
    return bool(_BOLD_RE.search(fontname or ""))


def is_math_font(fontname: str) -> bool:
    """Whether a PDF font name belongs to a math font (TeX, MathJax, ...)."""
    return bool(_MATH_FONT_RE.search(fontname or ""))


@dataclass
class TextLine:
    """One visual line of text with the font statistics we need."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float
    bold: bool
    math_chars: int = 0
    total_chars: int = 0

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, self.size, 1.0)

    @property
    def math_ratio(self) -> float:
        return self.math_chars / self.total_chars if self.total_chars else 0.0

    @property
    def style_key(self) -> Tuple[float, bool]:
        return (round(self.size * 2) / 2, self.bold)


def _line_from_chars(text: str, box: Dict[str, Any], chars: List[Dict]) -> TextLine:
    visible = [c for c in chars if str(c.get("text", "")).strip()]
    sizes = Counter(round(float(c.get("size", 0.0)), 1) for c in visible)
    size = sizes.most_common(1)[0][0] if sizes else 0.0
    bold_count = sum(1 for c in visible if is_bold_font(c.get("fontname", "")))
    math_count = sum(1 for c in visible if is_math_font(c.get("fontname", "")))
    return TextLine(
        text=text,
        x0=float(box["x0"]),
        top=float(box["top"]),
        x1=float(box["x1"]),
        bottom=float(box["bottom"]),
        size=size,
        bold=bool(visible) and bold_count >= 0.8 * len(visible),
        math_chars=math_count,
        total_chars=len(visible),
    )


def extract_lines(page: Any) -> List[TextLine]:
    """Text lines of a (possibly filtered) pdfplumber page, top to bottom."""
    lines: List[TextLine] = []
    for raw in page.extract_text_lines(strip=True, return_chars=True):
        text = raw.get("text", "")
        if not text.strip():
            continue
        segments = _split_at_column_gaps(raw.get("chars", []))
        if len(segments) <= 1:
            lines.append(_line_from_chars(text, raw, raw.get("chars", [])))
            continue
        for seg in segments:
            seg_text = _chars_to_text(seg)
            if seg_text.strip():
                box = {
                    "x0": min(float(c["x0"]) for c in seg),
                    "x1": max(float(c["x1"]) for c in seg),
                    "top": min(float(c["top"]) for c in seg),
                    "bottom": max(float(c["bottom"]) for c in seg),
                }
                lines.append(_line_from_chars(seg_text, box, seg))
    return lines


def _split_at_column_gaps(chars: List[Dict]) -> List[List[Dict]]:
    """Split one visual line where a wide horizontal gap separates columns.

    pdfplumber groups text by baseline across the whole page, so a heading in
    the left column and body text in the right column at the same height come
    back as one line. A gap wider than ~3 font sizes is treated as a column
    break.
    """
    ordered = sorted(chars, key=lambda c: float(c.get("x0", 0.0)))
    if len(ordered) < 2:
        return [ordered]
    segments: List[List[Dict]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        size = max(float(prev.get("size", 0.0)), float(cur.get("size", 0.0)), 1.0)
        gap = float(cur.get("x0", 0.0)) - float(prev.get("x1", 0.0))
        if gap > max(3.0 * size, 15.0):
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


def _chars_to_text(chars: List[Dict]) -> str:
    """Rebuild text from chars, adding spaces at word gaps."""
    out: List[str] = []
    prev = None
    for c in chars:
        if prev is not None:
            gap = float(c.get("x0", 0.0)) - float(prev.get("x1", 0.0))
            if gap > 0.25 * max(float(c.get("size", 0.0)), 1.0) and not (
                out and out[-1].endswith(" ")
            ):
                out.append(" ")
        out.append(str(c.get("text", "")))
        prev = c
    return "".join(out).strip()


def body_font_size(counter: Counter) -> Optional[float]:
    """The most common character size, i.e. the body text size."""
    if not counter:
        return None
    return float(counter.most_common(1)[0][0])


def is_heading_candidate(text: str, size: float, body: Optional[float]) -> bool:
    """Conservative test for a heading line: short, large, not a sentence."""
    if not body or size < body * HEADING_MIN_RATIO:
        return False
    t = text.strip()
    if not 2 <= len(t) <= HEADING_MAX_CHARS:
        return False
    if len(t.split()) > HEADING_MAX_WORDS:
        return False
    if t[-1] in ".,;:":
        return False
    letters = sum(ch.isalpha() for ch in t)
    if letters == 0:
        return False
    # Codes, barcodes and IDs (e.g. "*TXN98765202411231432*") are not headings:
    # require mostly letters among the non-space characters.
    if letters < 0.5 * len(t.replace(" ", "")):
        return False
    if _PAGE_NUMBER_RE.fullmatch(t):
        return False
    return True


class HeadingLevels:
    """Maps heading styles to Markdown levels: largest size is ``#``."""

    def __init__(self, styles: Iterable[Tuple[float, bool]]):
        ranked = sorted(set(styles), reverse=True)
        self._levels = {
            key: min(i + 1, MAX_HEADING_LEVEL) for i, key in enumerate(ranked)
        }

    def level(self, key: Tuple[float, bool]) -> int:
        return self._levels.get(key, MAX_HEADING_LEVEL)


def _same_heading(prev: TextLine, cur: TextLine) -> bool:
    """Whether two heading lines are one wrapped heading."""
    gap = cur.top - prev.bottom
    # Wrapped heading lines sit ~0.2 font sizes apart; separate headings further.
    return prev.style_key == cur.style_key and gap < 0.4 * max(prev.size, 1.0)


def heading_runs(
    lines: Sequence[TextLine], body: Optional[float]
) -> List[Tuple[int, int, TextLine]]:
    """Runs of consecutive heading lines as (start, end, first_line)."""
    runs: List[Tuple[int, int, TextLine]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not is_heading_candidate(line.text, line.size, body):
            i += 1
            continue
        j = i + 1
        while (
            j < len(lines)
            and is_heading_candidate(lines[j].text, lines[j].size, body)
            and _same_heading(lines[j - 1], lines[j])
        ):
            j += 1
        runs.append((i, j, line))
        i = j
    return runs


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def annotate_headings(text: str, headings: Sequence[Tuple[str, int]]) -> str:
    """Turn lines of plain extracted text into Markdown headings.

    ``headings`` holds (heading text, level) found from font data. A heading
    may be split over up to three consecutive lines of ``text`` (pdfminer
    often splits "1 Introduction" into "1" and "Introduction"). Text is
    returned unchanged when nothing matches.
    """
    if not headings or not text:
        return text
    wanted: Dict[str, List[int]] = {}
    for heading, level in headings:
        wanted.setdefault(_norm(heading), []).append(level)

    lines = text.split("\n")
    out: List[str] = []
    changed = False
    i = 0
    while i < len(lines):
        match = None
        if lines[i].strip():
            parts: List[str] = []
            j = i
            while j < len(lines) and len(parts) < 3:
                if lines[j].strip():
                    parts.append(lines[j].strip())
                    key = _norm(" ".join(parts))
                    if wanted.get(key):
                        match = (j, key)
                        break
                j += 1
        if match is None:
            out.append(lines[i])
            i += 1
            continue
        end, key = match
        level = wanted[key].pop(0)
        if out and out[-1].strip():
            out.append("")
        out.append("#" * level + " " + key)
        if end + 1 < len(lines) and lines[end + 1].strip():
            out.append("")
        changed = True
        i = end + 1
    return "\n".join(out) if changed else text


@dataclass
class Block:
    """A non-text element placed on a page: table, image or formula."""

    top: float
    x0: float
    markdown: str


def render_page(
    lines: Sequence[TextLine],
    blocks: Sequence[Block],
    body: Optional[float],
    levels: HeadingLevels,
) -> str:
    """Markdown for one page: paragraphs, headings and blocks in reading order."""
    heading_at: Dict[int, Tuple[int, TextLine]] = {
        start: (end, first) for start, end, first in heading_runs(lines, body)
    }
    items: List[Tuple[float, float, int, Any]] = []
    i = 0
    while i < len(lines):
        if i in heading_at:
            end, first = heading_at[i]
            text = " ".join(_norm(l.text) for l in lines[i:end])
            md = "#" * levels.level(first.style_key) + " " + text
            items.append((first.top, first.x0, 0, md))
            i = end
        else:
            items.append((lines[i].top, lines[i].x0, 1, lines[i]))
            i += 1
    for block in blocks:
        items.append((block.top, block.x0, 0, block.markdown))
    items.sort(key=lambda it: (it[0], it[1]))

    out: List[str] = []
    para: List[str] = []
    prev: Optional[TextLine] = None
    for _top, _x0, kind, payload in items:
        if kind == 1:
            line: TextLine = payload
            if para and prev is not None:
                gap = line.top - prev.bottom
                if gap > 0.7 * max(prev.size, line.size, 1.0):
                    out.append("\n".join(para))
                    para = []
            para.append(line.text)
            prev = line
        else:
            if para:
                out.append("\n".join(para))
                para = []
            prev = None
            out.append(payload)
    if para:
        out.append("\n".join(para))
    return "\n\n".join(p for p in out if p.strip())
