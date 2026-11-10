# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Text lines and heading inference for PDF pages.

Headings come from the best source a document offers: its outline
(bookmarks), heading tags of a tagged PDF, or a score built from font size
clusters, weight, spacing and numbering.
"""

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

# A line must be at least this much larger than the body text to become a
# heading. Kept high on purpose: a false heading is worse than a missed one.
# 1.15 catches LaTeX-style section titles (12pt over a 10pt body).
HEADING_MIN_RATIO = 1.15
HEADING_MAX_CHARS = 100
HEADING_MAX_WORDS = 14
MAX_HEADING_LEVEL = 3
# Outlines and structure tags carry their own depth.
MAX_OUTLINE_LEVEL = 6
# Sizes within this ratio of the body size are body text.
BODY_SIZE_TOLERANCE = 0.04
# Sizes closer than this ratio belong to one size cluster.
SIZE_CLUSTER_LINK = 0.05
# A line needs this score to become a heading (see score_heading).
HEADING_MIN_SCORE = 3.0

_BOLD_RE = re.compile(r"bold|black|heavy|demi", re.IGNORECASE)
_MATH_FONT_RE = re.compile(
    r"CMMI|CMSY|CMEX|CMBSY|MSAM|MSBM|EUFM|EUSM|MathJax|Math", re.IGNORECASE
)
_MONO_FONT_RE = re.compile(
    r"courier|mono|consol|menlo|typewriter|CMTT|SFTT|LMTT|fixed|inconsolata"
    r"|source ?code|andale|lucida ?console|terminal",
    re.IGNORECASE,
)
_HEADING_TAG_RE = re.compile(r"^(?:H([1-6])|H|Title)$")
# "2 Methods", "2.1 Setup", "A. Proofs", "IV. Results", "Chapter 3".
_NUMBERING_RE = re.compile(
    r"(?:(?i:chapter|section|part|appendix)\s+(?:\d+|[IVXLC]+|[A-Z])\b"
    r"|\d{1,3}(?:\.\d{1,3}){0,4}\.?\s+\S"
    r"|[A-Z]\.(?:\d{1,3}\.?)*\s+\S"
    r"|[IVX]{1,5}\.\s+\S)"
)
_LEADING_NUMBER_RE = re.compile(
    r"^(?:(?i:chapter|section|part|appendix)\s+)?"
    r"(?:\d{1,3}(?:\.\d{1,3})*|[A-Z]|[IVXLC]+)[.:)]?\s+"
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


def is_mono_font(fontname: str) -> bool:
    """Whether a PDF font name denotes a fixed-width (code) font."""
    return bool(_MONO_FONT_RE.search(fontname or ""))


def heading_tag_level(tag: Optional[str]) -> int:
    """Heading level of a marked-content tag (H1..H6, H, Title), else 0."""
    m = _HEADING_TAG_RE.match(tag or "")
    if not m:
        return 0
    return int(m.group(1)) if m.group(1) else 1


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
    # Set by the reading-order analysis.
    block: int = 0  # reading-order block the line belongs to
    mono: bool = False  # fixed-width font
    tabular: bool = False  # cells separated by wide gaps
    rotated: bool = False  # not horizontal (margin stamps)
    tag: Optional[str] = None  # marked-content tag of a tagged PDF
    char_width: float = 0.0
    first_word_width: float = 0.0
    # Bottom of the main text of the line, without sub- or superscripts.
    baseline: Optional[float] = None
    # Set by assign_headings: 0 for body text, else the Markdown level.
    heading_level: int = 0

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, self.size, 1.0)

    @property
    def math_ratio(self) -> float:
        return self.math_chars / self.total_chars if self.total_chars else 0.0

    @property
    def style_key(self) -> Tuple[float, bool]:
        return (round(self.size * 2) / 2, self.bold)


# A page in reading order: text lines and placed blocks.
Flow = List[Union[TextLine, "Block"]]


def _line_from_chars(text: str, box: Dict[str, Any], chars: List[Dict]) -> TextLine:
    visible = [c for c in chars if str(c.get("text", "")).strip()]
    sizes = Counter(round(float(c.get("size", 0.0)), 1) for c in visible)
    size = sizes.most_common(1)[0][0] if sizes else 0.0
    bold_count = sum(1 for c in visible if is_bold_font(c.get("fontname", "")))
    math_count = sum(1 for c in visible if is_math_font(c.get("fontname", "")))
    mono_count = sum(1 for c in visible if is_mono_font(c.get("fontname", "")))
    width = float(box["x1"]) - float(box["x0"])
    char_width = width / len(text) if text else 0.0
    words = text.split()
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
        mono=bool(visible) and mono_count >= 0.8 * len(visible),
        char_width=char_width,
        first_word_width=char_width * len(words[0]) if words else 0.0,
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


def _same_heading(prev: TextLine, cur: TextLine) -> bool:
    """Whether two heading lines are one wrapped heading."""
    gap = cur.top - prev.bottom
    # Wrapped heading lines sit ~0.2 font sizes apart; separate headings further.
    return prev.style_key == cur.style_key and gap < 0.4 * max(prev.size, 1.0)


def simple_flow(lines: Sequence[TextLine], blocks: Sequence["Block"]) -> Flow:
    """Lines and blocks sorted top to bottom, then left to right."""
    items: List[Tuple[float, float, int, Union[TextLine, "Block"]]] = []
    for i, line in enumerate(lines):
        items.append((line.top, line.x0, i, line))
    for j, block in enumerate(blocks):
        items.append((block.top, block.x0, len(lines) + j, block))
    items.sort(key=lambda it: (it[0], it[1], it[2]))
    return [it[3] for it in items]


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


# -- headings: ground truth first, scored heuristic otherwise -----------------


@dataclass
class OutlineEntry:
    """One bookmark: nesting level (1 = top), title, 1-based page or None."""

    level: int
    title: str
    page: Optional[int] = None


class SizeClusters:
    """1-D clustering of the font sizes used in a document.

    The body cluster holds the sizes within a few percent of the most common
    size. The other sizes are chained into clusters whenever neighbours are
    close, so 13.9pt and 14pt headings share a rank while 12pt and 14pt do
    not.
    """

    def __init__(self, sizes: Counter):
        self.body = body_font_size(sizes)
        self._clusters: List[Tuple[float, float]] = []
        if not self.body:
            return
        lo = self.body * (1 - BODY_SIZE_TOLERANCE)
        hi = self.body * (1 + BODY_SIZE_TOLERANCE)
        clusters: List[List[float]] = [[lo, hi]]
        for group in (
            sorted((s for s in sizes if 0 < s < lo), reverse=True),
            sorted(s for s in sizes if s > hi),
        ):
            current: Optional[List[float]] = None
            for size in group:
                near = current is not None and (
                    abs(size - current[1]) <= SIZE_CLUSTER_LINK * max(size, current[1])
                    or abs(size - current[0])
                    <= SIZE_CLUSTER_LINK * max(size, current[0])
                )
                if near and current is not None:
                    current[0] = min(current[0], size)
                    current[1] = max(current[1], size)
                else:
                    current = [size, size]
                    clusters.append(current)
        self._clusters = sorted((c[0], c[1]) for c in clusters)
        self._body_index = self._clusters.index((lo, hi))

    def rank(self, size: float) -> int:
        """Cluster rank relative to body text: 0 body, >0 larger, <0 smaller."""
        if not self._clusters:
            return 0
        best = min(
            range(len(self._clusters)),
            key=lambda i: (
                0.0
                if self._clusters[i][0] <= size <= self._clusters[i][1]
                else min(
                    abs(size - self._clusters[i][0]), abs(size - self._clusters[i][1])
                )
            ),
        )
        return best - self._body_index


def has_numbering(text: str) -> bool:
    """Whether text starts like a numbered heading ("2.1 Setup", "Chapter 3")."""
    return bool(_NUMBERING_RE.match(text.strip()))


def _heading_text_ok(text: str) -> bool:
    """Text rules shared by every heuristic heading (see is_heading_candidate)."""
    t = text.strip()
    if not 2 <= len(t) <= HEADING_MAX_CHARS:
        return False
    if len(t.split()) > HEADING_MAX_WORDS:
        return False
    if t[-1] in ".,;:":
        return False
    letters = sum(ch.isalpha() for ch in t)
    if letters == 0 or letters < 0.5 * len(t.replace(" ", "")):
        return False
    return not _PAGE_NUMBER_RE.fullmatch(t)


def score_heading(
    line: TextLine,
    clusters: SizeClusters,
    *,
    gap_above: Optional[float],
    typical_gap: float,
    next_line: Optional[TextLine],
    body_bold: bool = False,
) -> float:
    """Heading evidence for one line; HEADING_MIN_SCORE or more is a heading.

    Points: font size cluster above body (3 when clearly larger, 2 when
    slightly), bold (1), extra space above compared with the body leading
    (1), heading-style numbering (1), short (0.5), followed by body text
    (0.5). A line that is neither larger nor bold never qualifies.
    """
    body = clusters.body
    if not body or line.tabular or line.mono or line.rotated:
        return 0.0
    if not _heading_text_ok(line.text):
        return 0.0
    ratio = line.size / body
    rank = clusters.rank(line.size)
    if rank < 0:
        return 0.0
    size_pts = 0.0
    if ratio >= HEADING_MIN_RATIO:
        size_pts = 3.0
    elif rank > 0 and ratio >= 1.05:
        size_pts = 2.0
    bold_pts = 1.0 if line.bold and not body_bold else 0.0
    if not size_pts and not bold_pts:
        return 0.0
    score = size_pts + bold_pts
    if gap_above is None or gap_above >= 1.5 * typical_gap + 1.0:
        score += 1.0
    if has_numbering(line.text):
        score += 1.0
    if len(line.text.split()) <= 8:
        score += 0.5
    if (
        next_line is not None
        and not next_line.bold
        and clusters.rank(next_line.size) == 0
        and not next_line.tabular
    ):
        score += 0.5
    return score


def _match_key(text: str) -> str:
    return _norm(text).casefold().rstrip(" .:")


def _strip_number(key: str) -> str:
    return _LEADING_NUMBER_RE.sub("", key, count=1)


def _outline_match(title: str, text: str) -> bool:
    a, b = _match_key(title), _match_key(text)
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = _strip_number(_norm(title)).casefold(), _strip_number(_norm(text))
    sb = sb.casefold()
    return bool(sa) and sa.rstrip(" .:") == sb.rstrip(" .:")


def _apply_outline(
    pages: Dict[int, Flow], outline: Sequence[OutlineEntry]
) -> Tuple[int, List[Tuple[int, TextLine, int]]]:
    """Match bookmarks to lines.

    Returns the number of entries matched and (page, first line, lines
    spanned) for each match. A title may be wrapped over up to three lines.
    """
    used: Set[int] = set()
    matches: List[Tuple[int, TextLine, int, int]] = []
    order = sorted(pages)
    for entry in outline:
        if entry.page is not None:
            candidates = [
                p for p in (entry.page, entry.page + 1, entry.page - 1) if p in pages
            ]
        else:
            candidates = order
        found = False
        for page_no in candidates:
            lines = [x for x in pages[page_no] if isinstance(x, TextLine)]
            for i, line in enumerate(lines):
                if id(line) in used or line.rotated:
                    continue
                for span in (1, 2, 3):
                    group = lines[i : i + span]
                    if len(group) < span or any(id(g) in used for g in group):
                        break
                    if span > 1 and group[-1].block != line.block:
                        break
                    joined = " ".join(g.text for g in group)
                    if len(joined) > 3 * HEADING_MAX_CHARS:
                        break
                    if _outline_match(entry.title, joined):
                        used.update(id(g) for g in group)
                        level = max(1, min(entry.level, MAX_OUTLINE_LEVEL))
                        matches.append((page_no, line, span, level))
                        found = True
                        break
                if found:
                    break
            if found:
                break
    result: List[Tuple[int, TextLine, int]] = []
    for page_no, line, span, level in matches:
        line.heading_level = level
        result.append((page_no, line, span))
    return len(matches), result


def _merge_span(flow: Flow, first: TextLine, span: int) -> None:
    """Fold the lines of a wrapped heading into its first line."""
    if span <= 1:
        return
    lines = [x for x in flow if isinstance(x, TextLine)]
    start = next(i for i, x in enumerate(lines) if x is first)
    rest = lines[start + 1 : start + span]
    first.text = " ".join([first.text] + [r.text for r in rest])
    first.x0 = min([first.x0] + [r.x0 for r in rest])
    first.x1 = max([first.x1] + [r.x1 for r in rest])
    first.bottom = max([first.bottom] + [r.bottom for r in rest])
    drop = {id(r) for r in rest}
    flow[:] = [x for x in flow if id(x) not in drop]


def _typical_gap(pages: Dict[int, Flow], clusters: SizeClusters) -> float:
    """Median gap between consecutive body lines of one block (the leading)."""
    gaps: List[float] = []
    for flow in pages.values():
        prev: Optional[TextLine] = None
        for item in flow:
            if not isinstance(item, TextLine):
                prev = None
                continue
            if (
                prev is not None
                and prev.block == item.block
                and clusters.rank(prev.size) == 0
                and clusters.rank(item.size) == 0
            ):
                gap = item.top - prev.bottom
                if -0.5 * item.size < gap < 3 * item.size:
                    gaps.append(max(gap, 0.0))
            prev = item
    if not gaps:
        return 0.3 * (clusters.body or 10.0)
    gaps.sort()
    return gaps[len(gaps) // 2]


def assign_headings(
    pages: Dict[int, Flow],
    sizes: Counter,
    outline: Sequence[OutlineEntry] = (),
) -> str:
    """Set ``heading_level`` on the heading lines of every page flow.

    ``pages`` maps page numbers to flows (lines and blocks in reading
    order); wrapped headings are merged into one line in place. Returns the
    source used: "outline", "tags", "heuristic" or "none".
    """
    # 1. Bookmarks, when most of them can be found in the text.
    if outline:
        snapshot = {
            id(x): (x.text, x.x0, x.x1, x.bottom)
            for flow in pages.values()
            for x in flow
            if isinstance(x, TextLine)
        }
        count, matches = _apply_outline(pages, outline)
        if count and count >= 0.5 * len(outline):
            for page_no, line, span in matches:
                _merge_span(pages[page_no], line, span)
            return "outline"
        for flow in pages.values():
            for x in flow:
                if isinstance(x, TextLine) and id(x) in snapshot:
                    x.heading_level = 0

    # 2. Heading tags of a tagged PDF.
    tagged = False
    for flow in pages.values():
        for x in flow:
            if isinstance(x, TextLine) and not x.rotated:
                level = heading_tag_level(x.tag)
                if level and x.text.strip():
                    x.heading_level = level
                    tagged = True
    if tagged:
        for flow in pages.values():
            _merge_heading_runs(flow)
        return "tags"

    # 3. Scored heuristic over font size clusters.
    clusters = SizeClusters(sizes)
    if not clusters.body:
        return "none"
    typical = _typical_gap(pages, clusters)
    all_lines = [x for flow in pages.values() for x in flow if isinstance(x, TextLine)]
    body_lines = [x for x in all_lines if clusters.rank(x.size) == 0]
    body_bold = bool(body_lines) and sum(1 for x in body_lines if x.bold) > 0.5 * len(
        body_lines
    )
    found: List[TextLine] = []
    for flow in pages.values():
        prev: Optional[TextLine] = None
        for i, item in enumerate(flow):
            if not isinstance(item, TextLine):
                prev = None
                continue
            nxt = flow[i + 1] if i + 1 < len(flow) else None
            gap: Optional[float] = None
            if prev is not None and prev.block == item.block:
                gap = item.top - prev.bottom
            score = score_heading(
                item,
                clusters,
                gap_above=gap,
                typical_gap=typical,
                next_line=nxt if isinstance(nxt, TextLine) else None,
                body_bold=body_bold,
            )
            if score >= HEADING_MIN_SCORE:
                item.heading_level = -1  # level assigned below
                found.append(item)
            prev = item
    if not found:
        return "none"
    for flow in pages.values():
        _continue_wrapped_headings(flow, clusters)
    found = [x for x in all_lines if x.heading_level == -1]
    keys = sorted(
        {(clusters.rank(x.size), x.bold) for x in found},
        reverse=True,
    )
    levels = {key: min(i + 1, MAX_HEADING_LEVEL) for i, key in enumerate(keys)}
    for x in found:
        x.heading_level = levels[(clusters.rank(x.size), x.bold)]
    for flow in pages.values():
        _merge_heading_runs(flow)
    return "heuristic"


def _continue_wrapped_headings(flow: Flow, clusters: SizeClusters) -> None:
    """A larger-font line right under a heading of its style is its second line."""
    prev: Optional[TextLine] = None
    for item in flow:
        if not isinstance(item, TextLine):
            prev = None
            continue
        if (
            prev is not None
            and prev.heading_level == -1
            and item.heading_level == 0
            and clusters.rank(item.size) > 0
            and prev.block == item.block
            and _same_heading(prev, item)
            and len(item.text) <= HEADING_MAX_CHARS
        ):
            item.heading_level = -1
        prev = item


def _merge_heading_runs(flow: Flow) -> None:
    """Join consecutive lines of one wrapped heading into its first line."""
    out: Flow = []
    for item in flow:
        last = out[-1] if out else None
        if (
            isinstance(item, TextLine)
            and isinstance(last, TextLine)
            and item.heading_level
            and item.heading_level == last.heading_level
            and item.block == last.block
            and _same_heading(last, item)
        ):
            last.text = f"{last.text} {item.text}"
            last.x0 = min(last.x0, item.x0)
            last.x1 = max(last.x1, item.x1)
            last.bottom = item.bottom
            continue
        out.append(item)
    flow[:] = out


def heading_list(flow: Sequence[Union[TextLine, "Block"]]) -> List[Tuple[str, int]]:
    """(text, level) of the headings in a flow, for annotate_headings."""
    return [
        (x.text, x.heading_level)
        for x in flow
        if isinstance(x, TextLine) and x.heading_level > 0
    ]


@dataclass
class Block:
    """A non-text element placed on a page: table, image or formula.

    ``x1`` and ``bottom`` are optional; with them the block takes part in
    the reading-order analysis as an obstacle (a full-width table separates
    the columns above it from those below).
    """

    top: float
    x0: float
    markdown: str
    x1: Optional[float] = None
    bottom: Optional[float] = None
