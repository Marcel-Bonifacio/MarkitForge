# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Running headers and footers: text repeated in the page margins.

A line is page furniture when the same text (digits ignored, so "Page 3 of
12" and "Page 4 of 12" are the same) sits in the top or bottom margin band
of most pages. Documents under three pages are never touched.
"""

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set, Tuple

from ._layout import Flow, TextLine

# Documents with fewer pages keep all their text.
MIN_PAGES = 3
# Share of pages (or of odd / even pages) a line must repeat on.
MIN_PAGE_SHARE = 0.6
# Margin bands, as a share of the page height.
MARGIN_BAND = 0.1
# Only the outermost lines of a band are considered.
MAX_LINES_PER_BAND = 3
MAX_FURNITURE_CHARS = 150
# Stripping is called off when it would leave more than this share of the
# affected pages without any content.
MAX_EMPTIED_SHARE = 0.5

# A line whose numbers change from page to page only counts as furniture
# when it is short and its numbers never decrease with the page number
# (page numbers, "Page 3 of 12"). Varying totals or IDs in the margins are
# content.
MAX_VARYING_WORDS = 8

_DIGITS_RE = re.compile(r"\d+")
_ROMAN_RE = re.compile(r"(?=[ivxl])(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class PageLines:
    """The lines of one page with the page height they are measured against."""

    index: int
    height: float
    flow: Flow


def furniture_key(text: str) -> str:
    """Text with case, spacing and numbers normalised."""
    key = _SPACE_RE.sub(" ", text).strip().casefold()
    if _ROMAN_RE.fullmatch(key):
        return "0"
    return _DIGITS_RE.sub("0", key)


def _band_lines(page: PageLines) -> List[Tuple[str, TextLine]]:
    """Candidate lines of a page as (band, line): top, bottom or side."""
    lines = [x for x in page.flow if isinstance(x, TextLine) and x.text.strip()]
    out: List[Tuple[str, TextLine]] = [("side", x) for x in lines if x.rotated]
    upright = sorted((x for x in lines if not x.rotated), key=lambda x: x.top)
    if not upright or page.height <= 0:
        return out
    top_limit = MARGIN_BAND * page.height
    bottom_limit = (1 - MARGIN_BAND) * page.height
    top = [x for x in upright if (x.top + x.bottom) / 2 <= top_limit]
    bottom = [x for x in upright if (x.top + x.bottom) / 2 >= bottom_limit]
    out.extend(("top", x) for x in _outermost(top, first=True))
    out.extend(("bottom", x) for x in _outermost(bottom, first=False))
    return out


def _outermost(lines: List[TextLine], first: bool) -> List[TextLine]:
    """Lines on the outermost rows of a band (a row may hold several)."""
    rows: List[float] = []
    picked: List[TextLine] = []
    for line in lines if first else reversed(lines):
        if not any(abs(line.top - r) < 0.5 * max(line.size, 1.0) for r in rows):
            if len(rows) == MAX_LINES_PER_BAND:
                break
            rows.append(line.top)
        picked.append(line)
    return picked


def find_furniture(pages: Sequence[PageLines]) -> Dict[Tuple[str, str], str]:
    """Furniture of a document as {(band, key): first text seen}."""
    if len(pages) < MIN_PAGES:
        return {}
    seen: Dict[Tuple[str, str], List[int]] = {}
    example: Dict[Tuple[str, str], str] = {}
    texts: Dict[Tuple[str, str], List[Tuple[int, str]]] = {}
    for page in pages:
        keys: Set[Tuple[str, str]] = set()
        for band, line in _band_lines(page):
            if len(line.text) > MAX_FURNITURE_CHARS:
                continue
            key = (band, furniture_key(line.text))
            if key[1] and key not in keys:
                keys.add(key)
                seen.setdefault(key, []).append(page.index)
                example.setdefault(key, line.text.strip())
                texts.setdefault(key, []).append((page.index, line.text))
    odd = sum(1 for p in pages if p.index % 2)
    even = len(pages) - odd

    def enough(count: int, total: int) -> bool:
        return count >= max(MIN_PAGES, math.ceil(MIN_PAGE_SHARE * total))

    found: Dict[Tuple[str, str], str] = {}
    for key, indexes in seen.items():
        n_odd = sum(1 for i in indexes if i % 2)
        n_even = len(indexes) - n_odd
        # Books alternate headers between left and right pages.
        if (
            enough(len(indexes), len(pages))
            or (n_even == 0 and enough(n_odd, odd))
            or (n_odd == 0 and enough(n_even, even))
        ) and _stable_or_paged(texts[key]):
            found[key] = example[key]
    return found


def _stable_or_paged(occurrences: Sequence[Tuple[int, str]]) -> bool:
    """Whether a repeated line looks like furniture rather than content.

    Identical text on every page qualifies. Text whose numbers vary must be
    short, and it must vary like a page counter: every number either stays
    the same or moves in step with the page number (a constant offset, so
    printed page numbers that start at 1 after a cover page still match).
    Running totals, IDs and dates that change otherwise are content.
    """
    normalised = {
        _SPACE_RE.sub(" ", text).strip().casefold() for _, text in occurrences
    }
    if len(normalised) == 1:
        return True
    if len(occurrences[0][1].split()) > MAX_VARYING_WORDS:
        return False
    rows = [(index, _numbers(text)) for index, text in occurrences]
    width = len(rows[0][1])
    if width == 0 or any(len(numbers) != width for _, numbers in rows):
        return False
    paged = False
    for k in range(width):
        values = {numbers[k] for _, numbers in rows}
        if len(values) == 1:
            continue
        offsets = {numbers[k] - index for index, numbers in rows}
        if len(offsets) != 1:
            return False
        paged = True
    return paged


_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50}


def _numbers(text: str) -> List[int]:
    """Numbers in a line; a line that is just a roman numeral gives its value."""
    key = _SPACE_RE.sub(" ", text).strip().casefold()
    if key and _ROMAN_RE.fullmatch(key):
        total = 0
        for ch, nxt in zip(key, key[1:] + " "):
            value = _ROMAN_VALUES[ch]
            total += -value if _ROMAN_VALUES.get(nxt, 0) > value else value
        return [total]
    return [int(n) for n in _DIGITS_RE.findall(text)]


def strip_furniture(
    pages: Sequence[PageLines], furniture: Dict[Tuple[str, str], str]
) -> List[str]:
    """Remove furniture lines from the page flows in place.

    Returns the removed texts, one per distinct pattern, in order of first
    appearance. ``furniture`` is emptied when stripping is called off.
    """
    if not furniture:
        return []
    removed: Dict[Tuple[str, str], str] = {}
    drops: List[Tuple[PageLines, Set[int]]] = []
    emptied = 0
    for page in pages:
        drop: Set[int] = set()
        for band, line in _band_lines(page):
            key = (band, furniture_key(line.text))
            if key in furniture:
                drop.add(id(line))
                removed.setdefault(key, furniture[key])
        if drop:
            drops.append((page, drop))
            if all(id(x) in drop for x in page.flow if _has_content(x)):
                emptied += 1
    # Furniture frames content. If removing it would blank most of the pages
    # it sits on, the repeated lines are the content (slides, forms, labels).
    if emptied > MAX_EMPTIED_SHARE * len(drops):
        furniture.clear()
        return []
    for page, drop in drops:
        page.flow[:] = [x for x in page.flow if id(x) not in drop]
    return _unique(list(removed.values()))


def _has_content(item: object) -> bool:
    if isinstance(item, TextLine):
        return bool(item.text.strip())
    return bool(getattr(item, "markdown", "").strip())


def strip_furniture_text(text: str, furniture: Dict[Tuple[str, str], str]) -> str:
    """Remove furniture from plain extracted page text.

    Only the first and last few non-empty lines are checked; used where page
    text comes without positions.
    """
    if not furniture or not text:
        return text
    head_keys = {key for band, key in furniture if band != "bottom"}
    tail_keys = {key for band, key in furniture if band != "top"}
    lines = text.split("\n")
    filled = [i for i, line in enumerate(lines) if line.strip()]
    head = set(filled[:MAX_LINES_PER_BAND])
    tail = set(filled[-MAX_LINES_PER_BAND:]) - head
    kept = [
        line
        for i, line in enumerate(lines)
        if not (
            (i in head and furniture_key(line) in head_keys)
            or (i in tail and furniture_key(line) in tail_keys)
        )
    ]
    return "\n".join(kept) if len(kept) != len(lines) else text


def _unique(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out
