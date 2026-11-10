# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Markdown for a page flow: paragraphs rebuilt from hard-wrapped lines.

Lines of one paragraph are joined with spaces and end-of-line hyphenation is
undone where that is safe. Paragraphs end at a clearly larger vertical gap,
a first-line indent, a change of block or font size, or a line that stops
short of the right margin. List items stay one per line; fixed-width (code)
lines and tabular lines are never joined.
"""

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ._layout import Block, Flow, TextLine

# Without leading statistics a gap above this share of the font size ends a
# paragraph (the long-standing rule for unjoined output).
PLAIN_GAP_RATIO = 0.7
# With statistics: a baseline distance this many times the usual one of the
# block (plus slack, in points) ends a paragraph.
PITCH_FACTOR = 1.2
PITCH_SLACK = 0.5
# A smaller increase still ends a paragraph when the line before ends short.
SHORT_LINE_PITCH_FACTOR = 1.08
SHORT_LINE_PITCH_SLACK = 0.3
# Lines further apart than this share of the font size (beyond double
# spacing) never belong to one paragraph.
MAX_GAP_RATIO = 1.25
# Sizes differing by more than this ratio are different text styles.
SIZE_CHANGE = 0.08
# A first-line indent is between these many character widths.
MIN_INDENT_CHARS = 1.0
MAX_INDENT_CHARS = 8.0
# Groups with fewer lines take the right margin of their block.
MIN_MARGIN_LINES = 4

_BULLET_RE = re.compile(
    r"[-\u2013\u2014*\u2022\u25e6\u25aa\u2023\u00b7\u25cf\u25cb\u25a0\u25a1"
    r"\u27a2\u27a4\u2713\uf0b7]\s+\S"
)
_NUMBERED_RE = re.compile(
    r"(?:\(?\d{1,3}[.)]|\(?[A-Za-z][.)]|\(?[ivxIVX]{1,5}[.)]|\.\d{1,2})\s+\S"
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*")
_HYPHEN_END_RE = re.compile(r"([^\W\d_]+(?:-[^\W\d_]+)*)[-‐]$")
# "Report ID: 1234" under "Warehouse: East": labelled fields, one per line.
_FIELD_RE = re.compile(r"[^\W\d_][\w #/&.'()-]{0,28}:\s+\S")
_ENDS_WITH_HYPHEN_RE = re.compile(r"\w[-\u2010]$")
_FIRST_WORD_RE = re.compile(r"[^\W\d_]+")
_SENTENCE_END = ".!?:;。"
# Prefixes that normally keep their hyphen ("self-contained", "non-linear").
_HYPHEN_PREFIXES = {
    "all",
    "anti",
    "cross",
    "ex",
    "half",
    "multi",
    "non",
    "post",
    "pre",
    "quasi",
    "self",
    "semi",
    "well",
}
SOFT_HYPHEN = "­"


def is_bullet_item(text: str) -> bool:
    """Whether a line starts with a bullet character."""
    return bool(_BULLET_RE.match(text))


def is_list_item(text: str) -> bool:
    """Whether a line starts with a bullet or an item number ("1.", "(a)")."""
    return bool(_BULLET_RE.match(text) or _NUMBERED_RE.match(text))


def build_vocabulary(lines: Iterable[str]) -> Set[str]:
    """Lower-case words of a document, used to judge hyphenated line ends.

    The halves of a word split at a line end are not words of their own, so
    the word before a final hyphen and the one starting the next line are
    left out.
    """
    vocab: Set[str] = set()
    after_hyphen = False
    for text in lines:
        text = text.strip()
        words = _WORD_RE.findall(text)
        hyphen = text.endswith(("-", "\u2010", SOFT_HYPHEN))
        if after_hyphen and words:
            words = words[1:]
        if hyphen and words:
            words = words[:-1]
        vocab.update(w.lower() for w in words)
        after_hyphen = hyphen
    return vocab


def join_lines(first: str, second: str, vocab: Optional[Set[str]] = None) -> str:
    """Join two wrapped lines, undoing end-of-line hyphenation when safe.

    The hyphen is kept when the next line starts with a capital or a digit,
    when the fragment is a single letter ("e-mail") or already part of a
    hyphenated compound ("state-of-the-" + "art"), when the document uses the
    hyphenated form or both halves as words elsewhere, or after prefixes
    such as "self-".
    """
    first = first.rstrip()
    second = second.lstrip()
    if not first:
        return second
    if not second:
        return first
    if first.endswith(SOFT_HYPHEN):
        return first[:-1] + second
    if not _ENDS_WITH_HYPHEN_RE.search(first) or not second[0].isalnum():
        return f"{first} {second}"
    m = _HYPHEN_END_RE.search(first)
    nxt = _FIRST_WORD_RE.match(second)
    if not m or not nxt or not nxt.group(0)[0].islower():
        # "Jean-" + "Paul", "COVID-" + "19", "1-" + "3": a real hyphen.
        return first + second
    fragment, rest = m.group(1), nxt.group(0)
    if "-" in fragment or len(fragment) < 2:
        return first + second
    lowered = fragment.lower()
    joined, hyphenated = lowered + rest.lower(), f"{lowered}-{rest.lower()}"
    words = vocab or set()
    if joined not in words and (
        hyphenated in words
        or lowered in _HYPHEN_PREFIXES
        # Two words the document uses on their own: a compound such as
        # "chat-optimized" rather than a word broken for the line end.
        or (
            len(lowered) >= 4
            and lowered in words
            and (rest.lower() in words or len(rest) >= 7)
        )
    ):
        return first + second
    return first[:-1] + second


class _Group:
    """Consecutive lines of one block and style, with their margins."""

    def __init__(
        self,
        lines: List[TextLine],
        pitch: Optional[float] = None,
        block_right: Optional[float] = None,
    ):
        self.lines = lines
        self.pitch = pitch  # usual baseline distance of the block, if known
        self.left = min(line.x0 for line in lines)
        self.right = max(line.x1 for line in lines)
        self.text_right = self.right  # right edge of these lines themselves
        # A few lines cannot show where the right margin is; the block can.
        if len(lines) < MIN_MARGIN_LINES and block_right is not None:
            self.right = max(self.right, block_right)
        widths = sorted(line.char_width for line in lines if line.char_width > 0)
        self.char_width = widths[len(widths) // 2] if widths else 5.0


def _base(line: TextLine) -> float:
    return line.baseline if line.baseline is not None else line.bottom


def _typical_pitch(lines: Sequence[TextLine]) -> Dict[int, float]:
    """Median baseline distance per block; short blocks are left out."""
    found: Dict[int, List[float]] = {}
    prev: Optional[TextLine] = None
    for line in lines:
        if prev is not None and prev.block == line.block:
            pitch = _base(line) - _base(prev)
            if 0 < pitch < 4 * max(line.size, 1.0):
                found.setdefault(line.block, []).append(pitch)
        prev = line
    out: Dict[int, float] = {}
    for block, values in found.items():
        if len(values) >= 2:
            values.sort()
            out[block] = values[len(values) // 2]
    return out


def _gap_break(prev: TextLine, cur: TextLine, typical: Optional[float]) -> bool:
    gap = cur.top - prev.bottom
    size = max(prev.size, cur.size, 1.0)
    if gap < -0.5 * size:
        return True  # the next line is above: not the same paragraph
    if typical is None:
        return gap > PLAIN_GAP_RATIO * size
    if gap > MAX_GAP_RATIO * size:
        return True
    return _base(cur) - _base(prev) > PITCH_FACTOR * typical + PITCH_SLACK


def _style_change(prev: TextLine, cur: TextLine) -> bool:
    if prev.mono != cur.mono:
        return True
    big = max(prev.size, cur.size, 1.0)
    return abs(prev.size - cur.size) > SIZE_CHANGE * big


def _continues_across(prev: TextLine, cur: TextLine) -> bool:
    """A paragraph running on into the next column."""
    if prev.rotated or cur.rotated or prev.tabular or cur.tabular:
        return False
    if prev.mono or cur.mono or _style_change(prev, cur):
        return False
    a, b = prev.text.rstrip(), cur.text.lstrip()
    if not a or not b or a[-1] in _SENTENCE_END:
        return False
    return b[0].islower() and not is_list_item(b)


def _split_groups(
    flow: Flow, reflow: bool
) -> List[Tuple[Optional[Block], Optional[_Group], bool]]:
    """Cut a flow into blocks and line groups.

    Each entry is (block, None, False) or (None, group, continues), where
    ``continues`` says the group carries on the paragraph of the previous
    group (a column break in mid-sentence).
    """
    lines = [x for x in flow if isinstance(x, TextLine)]
    typical = _typical_pitch(lines) if reflow else {}
    rights: Dict[int, float] = {}
    for line in lines:
        if not line.heading_level and not line.rotated:
            rights[line.block] = max(rights.get(line.block, line.x1), line.x1)
    out: List[Tuple[Optional[Block], Optional[_Group], bool]] = []
    current: List[TextLine] = []
    carry = False

    def close() -> None:
        nonlocal current
        if current:
            out.append(
                (
                    None,
                    _Group(
                        current,
                        typical.get(current[0].block),
                        rights.get(current[0].block),
                    ),
                    carry,
                )
            )
        current = []

    for item in flow:
        if isinstance(item, Block):
            close()
            carry = False
            if item.markdown.strip():
                out.append((item, None, False))
            continue
        if not item.text.strip():
            continue
        if current:
            prev = current[-1]
            if (
                item.heading_level
                or prev.heading_level
                or item.rotated
                or prev.block != item.block
                or _style_change(prev, item)
                or _gap_break(prev, item, typical.get(item.block))
            ):
                close()
                carry = (
                    reflow
                    and prev.block != item.block
                    and not item.heading_level
                    and not prev.heading_level
                    and _continues_across(prev, item)
                    and _is_full(prev, out[-1][1])
                )
        current.append(item)
    close()
    return out


def _is_full(line: TextLine, group: Optional[_Group]) -> bool:
    """Whether a line reaches the right margin of its group."""
    if group is None:
        return False
    return line.x1 >= group.right - 2.0 * group.char_width


def _centred_pair(prev: TextLine, line: TextLine, char_width: float) -> bool:
    """Two centred lines of different width: a title block, not a paragraph."""
    return (
        abs((prev.x0 + prev.x1) - (line.x0 + line.x1)) < 1.5 * char_width
        and abs(prev.x0 - line.x0) > char_width
        and abs(prev.x1 - line.x1) > char_width
    )


def _returns_to_margin(
    lines: Sequence[TextLine], index: int, group: "_Group", short: bool
) -> bool:
    """Whether an indented line is a first-line indent, not a hanging one.

    After a first-line indent the text returns to the left margin; a hanging
    indent stays in. The last line of a group has no line after it, so the
    line before decides: a paragraph ends short.
    """
    if index + 1 >= len(lines):
        return short
    return lines[index + 1].x0 - group.left < 0.5 * group.char_width


def _reflow_group(group: _Group, vocab: Optional[Set[str]]) -> List[str]:
    """Paragraph texts of one group ("\\n" inside a text keeps a line break)."""
    paragraphs: List[str] = []
    text = ""
    in_list = False
    item_x0 = 0.0
    prev: Optional[TextLine] = None
    cw = group.char_width

    def flush() -> None:
        nonlocal text
        if text:
            paragraphs.append(text)
        text = ""

    lines = group.lines
    for index, line in enumerate(lines):
        content = line.text.strip()
        if line.mono and cw:
            # Code keeps its indentation; centred lines are not indented.
            left, right = line.x0 - group.left, group.text_right - line.x1
            if abs(left - right) > 1.5 * cw:
                content = " " * max(int(round(left / cw)), 0) + content
        if prev is None:
            text = content
            in_list = is_list_item(content)
            item_x0 = line.x0
            prev = line
            continue
        short = (
            prev.x1 + 0.6 * prev.char_width + line.first_word_width
            < group.right - 0.5 * cw
        )
        indent = line.x0 - group.left
        prev_indent = prev.x0 - group.left
        shifted = abs(line.x0 - prev.x0) > 0.5 * cw
        action = "join"
        if line.mono or prev.mono or line.tabular or prev.tabular:
            action = "line"
        elif is_bullet_item(content) or (
            # "(1)" and "a." also open sentences in running text; they start
            # an item only where a line could start a paragraph.
            is_list_item(content) and (in_list or short or shifted)
        ):
            action = "line"
            in_list = True
            item_x0 = line.x0
        elif in_list:
            hanging = line.x0 > item_x0 + 0.5 * cw
            if not hanging and short:
                action = "para" if indent < 0.5 * cw else "line"
                in_list = False
        elif (
            MIN_INDENT_CHARS * cw <= indent <= MAX_INDENT_CHARS * cw
            and prev_indent < 0.5 * cw
            and line.x1 > group.left + 0.5 * (group.right - group.left)
            and _returns_to_margin(lines, index, group, short)
        ):
            action = "para"
        elif (
            short
            or _centred_pair(prev, line, cw)
            or (_FIELD_RE.match(content) and _FIELD_RE.match(prev.text.strip()))
        ):
            action = "line"
        if (
            action == "line"
            and short
            and group.pitch is not None
            and _base(line) - _base(prev)
            > SHORT_LINE_PITCH_FACTOR * group.pitch + SHORT_LINE_PITCH_SLACK
        ):
            # Ends short and sits a little further down: a new paragraph.
            action = "para"
        if action == "join":
            text = join_lines(text, content, vocab)
        elif action == "line":
            text = f"{text}\n{content}"
        else:
            flush()
            text = content
            in_list = False
        prev = line
    flush()
    return paragraphs


def render_flow(
    flow: Flow, *, reflow: bool = True, vocab: Optional[Set[str]] = None
) -> str:
    """Markdown for one page: headings, paragraphs and blocks in flow order."""
    out: List[str] = []
    for block, group, continues in _split_groups(flow, reflow):
        if block is not None:
            out.append(block.markdown)
            continue
        assert group is not None
        first = group.lines[0]
        if first.heading_level > 0:
            title = " ".join(first.text.split())
            out.append("#" * first.heading_level + " " + title)
            for extra in group.lines[1:]:
                out.append(extra.text.strip())
            continue
        if not reflow:
            out.append("\n".join(line.text for line in group.lines))
            continue
        paragraphs = _reflow_group(group, vocab)
        if continues and out and paragraphs:
            out[-1] = join_lines(out[-1], paragraphs[0], vocab)
            paragraphs = paragraphs[1:]
        out.extend(paragraphs)
    return "\n\n".join(p for p in out if p.strip())
