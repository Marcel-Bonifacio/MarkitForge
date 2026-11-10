# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Math detection on PDF text without an LLM.

Three signals mark a character as mathematical besides its font name:
Unicode ranges (Greek, Mathematical Operators, super/subscripts,
Mathematical Alphanumeric Symbols), a baseline shift (a smaller character
raised or lowered against its neighbours is a super/subscript) and the
density of operators on a line.

The module offers two things built on them:

- :func:`is_display_math` / :func:`display_math_flags` decide which lines
  are stand-alone formulas (the regions an LLM transcribes);
- :func:`inline_math_text` rewrites simple inline math on one line as
  ``$...$`` LaTeX. It is conservative: a run needs real evidence
  (a script on a variable, a Greek letter, a math operator) and its LaTeX
  must pass :func:`safe_latex`, otherwise the text is left alone.

Everything works on pdfplumber character dicts and is free of I/O.
"""

import bisect
import dataclasses
import re
import statistics
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .._text_safety import safe_latex
from . import _layout
from ._layout import TextLine

BBox = Tuple[float, float, float, float]

# A character this much smaller than the line's body text can be a script.
SCRIPT_MAX_SIZE_RATIO = 0.9
# Baseline shift, as a share of the body size, that makes it one.
SUPERSCRIPT_MIN_RISE = 0.18
SUBSCRIPT_MIN_DROP = 0.08
# pdfminer boxes start at the font descent; this is close for most fonts.
_DESCENT = 0.21

# A display-math line: this share of its characters are math signals or
# operators, and at most this share sit in ordinary words.
DISPLAY_MIN_DENSITY = 0.25
DISPLAY_MAX_PROSE = 0.35
# Lines with more characters than this are skipped by the inline converter.
MAX_INLINE_LINE_CHARS = 1000

# ----------------------------------------------------------------- characters

_GREEK_LATEX: Dict[str, str] = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "varepsilon",
    "ϵ": "epsilon",
    "ζ": "zeta",
    "η": "eta",
    "θ": "theta",
    "ϑ": "vartheta",
    "ι": "iota",
    "κ": "kappa",
    "λ": "lambda",
    "μ": "mu",
    "µ": "mu",
    "ν": "nu",
    "ξ": "xi",
    "π": "pi",
    "ϖ": "varpi",
    "ρ": "rho",
    "ϱ": "varrho",
    "σ": "sigma",
    "ς": "varsigma",
    "τ": "tau",
    "υ": "upsilon",
    "φ": "varphi",
    "ϕ": "phi",
    "χ": "chi",
    "ψ": "psi",
    "ω": "omega",
    "Γ": "Gamma",
    "Δ": "Delta",
    "∆": "Delta",
    "Θ": "Theta",
    "Λ": "Lambda",
    "Ξ": "Xi",
    "Π": "Pi",
    "Σ": "Sigma",
    "Υ": "Upsilon",
    "Φ": "Phi",
    "Ψ": "Psi",
    "Ω": "Omega",
    "Ω": "Omega",
}
# Greek capitals (and omicron) that TeX writes with Latin letters.
_GREEK_AS_LATIN: Dict[str, str] = dict(zip("ΑΒΕΖΗΙΚΜΝΟΡΤΧο", "ABEZHIKMNOPTXo"))

# Operators that are evidence of math on their own.
_STRONG_OPERATORS: Dict[str, str] = {
    "≤": "leq",
    "≥": "geq",
    "≠": "neq",
    "≈": "approx",
    "≡": "equiv",
    "≃": "simeq",
    "≅": "cong",
    "≪": "ll",
    "≫": "gg",
    "∝": "propto",
    "∞": "infty",
    "∑": "sum",
    "∏": "prod",
    "∫": "int",
    "∮": "oint",
    "∂": "partial",
    "∇": "nabla",
    "√": "surd",
    "∈": "in",
    "∉": "notin",
    "∋": "ni",
    "⊂": "subset",
    "⊃": "supset",
    "⊆": "subseteq",
    "⊇": "supseteq",
    "∪": "cup",
    "∩": "cap",
    "∅": "emptyset",
    "∀": "forall",
    "∃": "exists",
    "∧": "wedge",
    "∨": "vee",
    "⊕": "oplus",
    "⊗": "otimes",
    "⊥": "perp",
    "∥": "parallel",
    "∠": "angle",
    "∘": "circ",
    "∓": "mp",
}
# Operators that also appear in ordinary text; they join a formula but do
# not start one.
_WEAK_OPERATORS: Dict[str, str] = {
    "±": "pm",
    "×": "times",
    "÷": "div",
    "·": "cdot",
    "⋅": "cdot",
    "∙": "cdot",
    "∼": "sim",
    "¬": "neg",
    "→": "to",
    "←": "leftarrow",
    "↔": "leftrightarrow",
    "⇒": "Rightarrow",
    "⇐": "Leftarrow",
    "⇔": "Leftrightarrow",
    "↦": "mapsto",
    "…": "ldots",
    "⟨": "langle",
    "⟩": "rangle",
}
_PLAIN_OPERATORS: Dict[str, str] = {"−": "-", "∗": "*", "∕": "/", "′": "'", "∣": "|"}
_LETTERLIKE: Dict[str, str] = {
    "ℂ": "\\mathbb{C}",
    "ℍ": "\\mathbb{H}",
    "ℕ": "\\mathbb{N}",
    "ℙ": "\\mathbb{P}",
    "ℚ": "\\mathbb{Q}",
    "ℝ": "\\mathbb{R}",
    "ℤ": "\\mathbb{Z}",
    "ℓ": "\\ell",
    "ℏ": "\\hbar",
    "ℜ": "\\Re",
    "ℑ": "\\Im",
    "ℵ": "\\aleph",
}
_ASCII_ESCAPES: Dict[str, str] = {
    "<": "\\lt",
    ">": "\\gt",
    "%": "\\%",
    "#": "\\#",
    "&": "\\&",
}
# ASCII characters with a meaning in TeX or Markdown that a run of plain
# PDF text cannot be trusted to use as math.
_ASCII_REJECT = frozenset("\\{}^_$`~")
_ASCII_OPERATORS = frozenset("=+-*/<>")

_SUPERSCRIPT_CHARS: Dict[str, str] = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿⁱ", "0123456789+-ni"))
_SUBSCRIPT_CHARS: Dict[str, str] = dict(
    zip("₀₁₂₃₄₅₆₇₈₉₊₋ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼ", "0123456789+-aeoxhklmnpstij")
)

# pdfminer returns Symbol-font text as the Latin-1 character of each code.
_SYMBOL_FONT_RE = re.compile(r"(?:^|\+)Symbol", re.IGNORECASE)
_SYMBOL_FONT: Dict[str, str] = {
    **dict(zip("ABGDEZHQIKLMNXOPRSTUFCYW", "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ")),
    **dict(zip("abgdezhqiklmnxoprstufcyw", "αβγδεζηθικλμνξοπρστυφχψω")),
    "j": "ϕ",
    "J": "ϑ",
    "v": "ϖ",
    "V": "ς",
    "-": "−",
    "*": "∗",
    "~": "∼",
    "@": "≅",
    '"': "∀",
    "$": "∃",
    "\xa2": "′",
    "\xa3": "≤",
    "\xa5": "∞",
    "\xab": "↔",
    "\xac": "←",
    "\xae": "→",
    "\xb0": "°",
    "\xb1": "±",
    "\xb3": "≥",
    "\xb4": "×",
    "\xb5": "∝",
    "\xb6": "∂",
    "\xb8": "÷",
    "\xb9": "≠",
    "\xba": "≡",
    "\xbb": "≈",
    "\xbc": "…",
    "\xc6": "∅",
    "\xc7": "∩",
    "\xc8": "∪",
    "\xcc": "⊂",
    "\xcd": "⊆",
    "\xce": "∈",
    "\xd1": "∇",
    "\xd5": "∏",
    "\xd6": "√",
    "\xd7": "⋅",
    "\xd8": "¬",
    "\xd9": "∧",
    "\xda": "∨",
    "\xdb": "⇔",
    "\xde": "⇒",
    "\xe5": "∑",
    "\xf2": "∫",
}

# Unit prefixes and symbols written with Greek letters ("5 μm", "10 kΩ").
_GREEK_UNIT_RE = re.compile(r"^(?:[µμ][A-Za-z]{1,3}|[kMGmµμ]?[ΩΩ])$")
_ORDINAL_RE = re.compile(r"^[A-Za-z]{2,}$")
_SCRIPT_TEXT_RE = re.compile(r"^[0-9A-Za-z+\-−,*'′α-ωΑ-Ω]{1,6}$")
_OPERAND_RE = re.compile(r"^\(?(?:\d+(?:[.,]\d+)*[A-Za-z]{0,2}|[A-Za-z]\d*)\)?$")
_TRAILING_PUNCT = ".,;:!?"


def is_greek(ch: str) -> bool:
    """Greek letters, including the micro and ohm signs."""
    return ch in _GREEK_LATEX or ch in _GREEK_AS_LATIN or "Ͱ" <= ch <= "Ͽ"


def is_math_alphanumeric(ch: str) -> bool:
    """Mathematical Alphanumeric Symbols (U+1D400-U+1D7FF) and the
    letter-like symbols TeX fonts map to (double-struck, script)."""
    return "\U0001d400" <= ch <= "\U0001d7ff" or ch in _LETTERLIKE


def is_math_operator(ch: str) -> bool:
    """A character from the Mathematical Operators block (U+2200-U+22FF)
    or one of the operators known outside it."""
    return "∀" <= ch <= "⋿" or ch in _STRONG_OPERATORS or ch in _WEAK_OPERATORS


def is_strong_math_char(ch: str) -> bool:
    """A character that is evidence of math by itself: an operator that
    prose does not use, a Unicode super/subscript or a math letter."""
    if ch in _WEAK_OPERATORS or ch in _PLAIN_OPERATORS:
        return False
    return (
        ch in _STRONG_OPERATORS
        or ch in _SUPERSCRIPT_CHARS
        or ch in _SUBSCRIPT_CHARS
        or is_math_alphanumeric(ch)
        or "∀" <= ch <= "⋿"
    )


# --------------------------------------------------------------------- glyphs


@dataclass
class Glyph:
    """One character of a line, with its script position."""

    text: str
    raw: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    math_font: bool = False
    shift: int = 0  # +1 superscript, -1 subscript


@dataclass
class MathLine:
    """A visual line rebuilt from characters, scripts included."""

    glyphs: List[Glyph]
    bbox: BBox

    @property
    def text(self) -> str:
        return _plain_text(self.glyphs)


def _glyph(char: Mapping[str, Any]) -> Optional[Glyph]:
    try:
        raw = str(char.get("text", ""))
        x0, x1 = float(char["x0"]), float(char["x1"])
        top, bottom = float(char["top"]), float(char["bottom"])
        size = float(char.get("size") or (bottom - top))
    except (KeyError, TypeError, ValueError):
        return None
    if not raw or not char.get("upright", True) or bottom <= top:
        return None
    font = str(char.get("fontname", "") or "")
    text = raw
    symbol = bool(_SYMBOL_FONT_RE.search(font))
    if symbol:
        text = "".join(_SYMBOL_FONT.get(ch, ch) for ch in raw)
    return Glyph(
        text=text,
        raw=raw,
        x0=x0,
        x1=x1,
        top=top,
        bottom=bottom,
        size=max(size, 0.1),
        math_font=(symbol and text != raw) or _layout.is_math_font(font),
    )


def _mark_scripts(glyphs: Sequence[Glyph]) -> None:
    """Set ``shift`` on characters that are smaller than the line's body
    text and sit clearly above or below its baseline."""
    visible = [g for g in glyphs if g.text.strip()]
    if not visible:
        return
    sizes: Dict[float, int] = {}
    for g in visible:
        key = round(g.size, 1)
        sizes[key] = sizes.get(key, 0) + 1
    body = max(sizes, key=lambda s: (sizes[s], s))
    base = [g.bottom for g in visible if round(g.size, 1) == body]
    baseline = statistics.median(base) - _DESCENT * body
    for g in visible:
        if g.text in _SUPERSCRIPT_CHARS:
            g.shift = 1
            continue
        if g.text in _SUBSCRIPT_CHARS:
            g.shift = -1
            continue
        if g.size > SCRIPT_MAX_SIZE_RATIO * body:
            continue
        rise = baseline - (g.bottom - _DESCENT * g.size)
        if rise >= SUPERSCRIPT_MIN_RISE * body:
            g.shift = 1
        elif rise <= -SUBSCRIPT_MIN_DROP * body:
            g.shift = -1


def _split_columns(glyphs: List[Glyph]) -> List[List[Glyph]]:
    """Split a line at gaps wide enough to separate page columns (the same
    rule the layout code applies to its text lines)."""
    parts: List[List[Glyph]] = [[glyphs[0]]]
    for prev, cur in zip(glyphs, glyphs[1:]):
        if cur.x0 - prev.x1 > max(3.0 * max(prev.size, cur.size, 1.0), 15.0):
            parts.append([cur])
        else:
            parts[-1].append(cur)
    return parts


def math_lines(chars: Sequence[Mapping[str, Any]]) -> List[MathLine]:
    """Rebuild visual lines from page characters, top to bottom.

    Unlike a plain top-coordinate grouping this keeps super- and subscripts
    on the line they belong to: a character joins the line whose body text
    it overlaps vertically.
    """
    glyphs = [g for g in (_glyph(c) for c in chars) if g is not None]
    glyphs.sort(key=lambda g: (g.top, g.x0))
    groups: List[List[Glyph]] = []
    bands: List[Tuple[float, float, float]] = []  # top, bottom, size of body text
    for g in glyphs:
        placed = False
        height = g.bottom - g.top
        for k in range(len(groups) - 1, max(len(groups) - 4, -1), -1):
            top, bottom, size = bands[k]
            overlap = min(g.bottom, bottom) - max(g.top, top)
            if overlap >= 0.3 * min(height, bottom - top):
                groups[k].append(g)
                if g.text.strip() and g.size > size + 0.05:
                    bands[k] = (g.top, g.bottom, g.size)
                elif g.text.strip() and abs(g.size - size) <= 0.05:
                    # Body text in another font can sit a little higher
                    # or lower; the band covers both.
                    grown = (min(top, g.top), max(bottom, g.bottom), size)
                    if grown[1] - grown[0] <= 1.5 * size:
                        bands[k] = grown
                placed = True
                break
        if not placed:
            groups.append([g])
            bands.append((g.top, g.bottom, g.size if g.text.strip() else 0.0))
    lines: List[MathLine] = []
    for group in groups:
        group.sort(key=lambda g: (g.x0, g.x1))
        for part in _split_columns(group):
            if not any(g.text.strip() for g in part):
                continue
            _mark_scripts(part)
            bbox = (
                min(g.x0 for g in part),
                min(g.top for g in part),
                max(g.x1 for g in part),
                max(g.bottom for g in part),
            )
            lines.append(MathLine(glyphs=part, bbox=bbox))
    lines.sort(key=lambda line: (line.bbox[1], line.bbox[0]))
    return lines


def _tokens(glyphs: Sequence[Glyph]) -> List[List[Glyph]]:
    """Words of a line: glyphs split at space characters and word gaps."""
    tokens: List[List[Glyph]] = []
    current: List[Glyph] = []
    prev: Optional[Glyph] = None
    for g in glyphs:
        gap = g.x0 - prev.x1 if prev is not None else 0.0
        blank = not g.text.strip()
        if current and (blank or gap > 0.25 * max(g.size, 1.0)):
            tokens.append(current)
            current = []
        if not blank:
            current.append(g)
        prev = g
    if current:
        tokens.append(current)
    return tokens


def _plain_text(glyphs: Sequence[Glyph]) -> str:
    return " ".join("".join(g.text for g in token) for token in _tokens(glyphs))


# --------------------------------------------------------------- display math


def math_signal_counts(text: str) -> Tuple[int, int, int, int]:
    """(characters, strong math signals, operators, letters in words) of a
    line's text. Greek letters count as math unless they form a word."""
    total = strong = operators = prose = 0
    run: List[str] = []

    def close_run() -> None:
        nonlocal strong, prose
        if len(run) >= 4:
            prose += len(run)
        else:
            strong += sum(1 for ch in run if is_greek(ch))
        run.clear()

    for ch in text:
        if ch.isspace():
            close_run()
            continue
        total += 1
        if ch.isalpha() and not is_math_alphanumeric(ch):
            run.append(ch)
            continue
        close_run()
        if is_strong_math_char(ch):
            strong += 1
        elif ch in _ASCII_OPERATORS or ch in _WEAK_OPERATORS or ch in _PLAIN_OPERATORS:
            operators += 1
    close_run()
    return total, strong, operators, prose


def is_display_math(text: str, extra_signals: int = 0) -> bool:
    """Whether a line of text is a stand-alone formula.

    ``extra_signals`` counts evidence the text cannot show: characters in
    a math font and characters shifted off the baseline.
    """
    total, strong, operators, prose = math_signal_counts(text)
    strong += max(int(extra_signals), 0)
    if total < 3 or strong < 1:
        return False
    if operators < 1 and strong < 3:
        return False
    if prose > DISPLAY_MAX_PROSE * total:
        return False
    return min(strong, total) + operators >= DISPLAY_MIN_DENSITY * total


class _LineIndex:
    """Finds the text lines whose centre lies inside a box."""

    def __init__(self, lines: Sequence[TextLine]):
        centres = [((ln.top + ln.bottom) / 2, i) for i, ln in enumerate(lines)]
        centres.sort()
        self._lines = lines
        self._ys = [cy for cy, _i in centres]
        self._order = [i for _cy, i in centres]

    def inside(self, bbox: BBox) -> List[int]:
        lo = bisect.bisect_left(self._ys, bbox[1] - 1)
        hi = bisect.bisect_right(self._ys, bbox[3] + 1)
        found = []
        for i in self._order[lo:hi]:
            line = self._lines[i]
            if bbox[0] - 1 <= (line.x0 + line.x1) / 2 <= bbox[2] + 1:
                found.append(i)
        return sorted(found)


def display_math_flags(
    lines: Sequence[TextLine], chars: Optional[Sequence[Mapping[str, Any]]] = None
) -> List[bool]:
    """For each text line, whether it belongs to a display formula.

    Without ``chars`` only the line text is examined. With the page's
    characters, baseline shifts and Symbol-font letters count as well, and
    the script fragments that line extraction splits off a formula are
    flagged together with it.
    """
    flags = [is_display_math(line.text) for line in lines]
    if not chars:
        return flags
    index = _LineIndex(lines)
    for visual in math_lines(chars):
        extra = sum(
            1 for g in visual.glyphs if g.text.strip() and (g.shift or g.math_font)
        )
        if not extra or not is_display_math(visual.text, extra):
            continue
        for i in index.inside(visual.bbox):
            flags[i] = True
    return flags


# ---------------------------------------------------------------- inline math


@dataclass
class _Token:
    glyphs: List[Glyph]
    prefix: str = ""
    suffix: str = ""
    core: List[Glyph] = dataclasses.field(default_factory=list)
    math: bool = False

    @property
    def text(self) -> str:
        return _text(self.glyphs)

    @property
    def core_text(self) -> str:
        return _text(self.core)


def _split_punctuation(token: _Token) -> None:
    """Move sentence punctuation and an unbalanced bracket out of the core."""
    core = list(token.glyphs)
    suffix: List[str] = []
    while len(core) > 1 and core[-1].shift == 0 and core[-1].text in _TRAILING_PUNCT:
        suffix.insert(0, core.pop().text)
    text = "".join(g.text for g in core)
    prefix = ""
    if len(core) > 1 and text.count("(") > text.count(")") and core[0].text == "(":
        prefix = core.pop(0).text
    elif len(core) > 1 and text.count(")") > text.count("(") and core[-1].text == ")":
        if core[-1].shift == 0:
            suffix.insert(0, core.pop().text)
    token.prefix, token.core, token.suffix = prefix, core, "".join(suffix)


def _script_text(glyphs: Sequence[Glyph]) -> str:
    """Script characters as plain ones (a Unicode superscript two is "2")."""
    return "".join(
        _SUPERSCRIPT_CHARS.get(ch, _SUBSCRIPT_CHARS.get(ch, ch))
        for g in glyphs
        for ch in g.text
    )


def _scripts_attached(core: Sequence[Glyph]) -> Optional[bool]:
    """None when the token has no script; True when every script hangs on
    something a formula would put it on (a variable, a number, a closing
    bracket); False for footnote marks and ordinals."""
    found = False
    i = 0
    while i < len(core):
        if core[i].shift == 0:
            i += 1
            continue
        j = i
        while j < len(core) and core[j].shift == core[i].shift:
            j += 1
        found = True
        script = _script_text(core[i:j])
        k = i
        while k > 0 and core[k - 1].shift != 0:
            k -= 1
        stem = "".join(g.text for g in core[:k])
        if not stem or not _SCRIPT_TEXT_RE.match(script):
            return False
        last = stem[-1]
        if last.isdigit():
            if _ORDINAL_RE.match(script):
                return False
        elif last.isalpha():
            letters = 0
            for ch in reversed(stem):
                if not ch.isalpha():
                    break
                letters += 1
            if letters > 2:
                return False
        elif last not in ")]":
            return False
        i = j
    return found if found else None


def _is_math_token(token: _Token, use_fonts: bool = True) -> bool:
    core = token.core
    text = token.core_text
    if not text or any(ch in _ASCII_REJECT for ch in text):
        return False
    scripts = _scripts_attached(core)
    if scripts is False:
        return False
    if scripts:
        return True
    if any(is_strong_math_char(g.text) for g in core):
        return True
    greek = [ch for ch in text if is_greek(ch)]
    if greek:
        if _GREEK_UNIT_RE.match(text) or re.search(r"[Ͱ-Ͽ]{3,}", text):
            return False
        return True
    # Letters set in a math font ("where x is ..."): short tokens only.
    if not use_fonts:
        return False
    return len(text) <= 3 and all(g.math_font for g in core) and text.isalnum()


def _is_operator_token(token: _Token) -> bool:
    text = token.text
    return bool(text) and all(
        ch in _ASCII_OPERATORS
        or ch in _WEAK_OPERATORS
        or ch in _PLAIN_OPERATORS
        or ch in _STRONG_OPERATORS
        for ch in text
    )


def _is_operand_token(token: _Token) -> bool:
    return (
        all(g.shift == 0 for g in token.core)
        and bool(_OPERAND_RE.match(token.core_text))
        and not token.prefix
    )


def _latex_char(ch: str) -> Optional[str]:
    """LaTeX for one character, or None when it cannot go into a formula."""
    if ch in _GREEK_LATEX:
        return "\\" + _GREEK_LATEX[ch]
    if ch in _GREEK_AS_LATIN:
        return _GREEK_AS_LATIN[ch]
    if ch in _STRONG_OPERATORS:
        return "\\" + _STRONG_OPERATORS[ch]
    if ch in _WEAK_OPERATORS:
        return "\\" + _WEAK_OPERATORS[ch]
    if ch in _PLAIN_OPERATORS:
        return _PLAIN_OPERATORS[ch]
    if ch in _LETTERLIKE:
        return _LETTERLIKE[ch]
    if ch in _ASCII_ESCAPES:
        return _ASCII_ESCAPES[ch]
    if ch in _ASCII_REJECT:
        return None
    if ord(ch) < 128:
        return ch if ch.isprintable() else None
    plain = unicodedata.normalize("NFKC", ch)
    if plain != ch and plain.isascii() and plain.isalnum():
        return plain
    category = unicodedata.category(ch)
    if category[0] in ("L", "N") or category == "Sm":
        return ch
    return None


def _join_latex(pieces: Sequence[str]) -> str:
    out: List[str] = []
    for piece in pieces:
        # "\pi" followed by a letter needs a space to end the command.
        if out and re.search(r"\\[A-Za-z]+$", out[-1]) and piece[:1].isalpha():
            out.append(" ")
        out.append(piece)
    return "".join(out)


def _text(glyphs: Sequence[Glyph]) -> str:
    return "".join(g.text for g in glyphs)


def _latex(glyphs: Sequence[Glyph]) -> Optional[str]:
    """LaTeX for the characters of one token."""
    pieces: List[str] = []
    i = 0
    while i < len(glyphs):
        shift = glyphs[i].shift
        j = i
        while j < len(glyphs) and glyphs[j].shift == shift:
            j += 1
        source = _script_text(glyphs[i:j]) if shift else _text(glyphs[i:j])
        chars = [_latex_char(ch) for ch in source]
        if any(ch is None for ch in chars):
            return None
        body = [ch for ch in chars if ch is not None]
        if shift:
            pieces.append(("^{" if shift > 0 else "_{") + _join_latex(body) + "}")
        else:
            pieces.extend(body)
        i = j
    return _join_latex(pieces)


def _grow_runs(tokens: Sequence[_Token]) -> List[bool]:
    """Extend math tokens over ``operand operator`` neighbours:
    ``E = mc^2`` is one run although only ``mc^2`` carries evidence."""
    in_run = [t.math for t in tokens]
    n = len(tokens)
    changed = True
    while changed:
        changed = False
        for i in range(n):
            if not in_run[i]:
                continue
            for step in (-1, 1):
                a, b = i + step, i + 2 * step
                if not 0 <= a < n or in_run[a]:
                    continue
                here_op = _is_operator_token(tokens[i])
                if here_op and _is_operand_token(tokens[a]):
                    in_run[a] = changed = True
                elif not here_op and _is_operator_token(tokens[a]) and 0 <= b < n:
                    if in_run[b] or _is_operand_token(tokens[b]):
                        in_run[a] = in_run[b] = changed = True
    return in_run


def _inline_text(glyphs: Sequence[Glyph]) -> Optional[Tuple[str, int]]:
    tokens = [_Token(glyphs=t) for t in _tokens(glyphs)]
    if not tokens or sum(len(t.glyphs) for t in tokens) > MAX_INLINE_LINE_CHARS:
        return None
    plain = " ".join(t.text for t in tokens)
    # A dollar sign already on the line (prices) would pair up with ours.
    if "$" in plain:
        return None
    # Greek prose is text, not a row of variables.
    if len(re.findall(r"[Ͱ-Ͽ]{4,}", plain)) >= 2:
        return None
    # A line set mostly in a math font is a formula or uses that font for
    # its text; either way the font says nothing about single words.
    visible = [g for t in tokens for g in t.glyphs]
    use_fonts = sum(1 for g in visible if g.math_font) < 0.5 * len(visible)
    for token in tokens:
        _split_punctuation(token)
        token.math = _is_math_token(token, use_fonts)
    if not any(t.math for t in tokens):
        return None
    in_run = _grow_runs(tokens)

    out: List[str] = []
    converted = 0
    i = 0
    while i < len(tokens):
        if not in_run[i]:
            out.append(tokens[i].text)
            i += 1
            continue
        j = i
        while j < len(tokens) and in_run[j]:
            j += 1
        run = tokens[i:j]
        # Punctuation inside the run stays in the formula; at its ends it
        # stays outside the dollar signs, unless it is a bracket whose
        # partner is inside.
        prefix, suffix = run[0].prefix, run[-1].suffix
        inner = "".join(t.text for t in run)
        inner = inner[len(prefix) : len(inner) - len(suffix)]
        if prefix and inner.count(")") > inner.count("("):
            prefix = ""
        elif suffix.startswith(")") and inner.count("(") > inner.count(")"):
            suffix = suffix[1:]
        parts: List[Optional[str]] = []
        for k, token in enumerate(run):
            first = len(prefix) if k == 0 else 0
            last = len(token.glyphs) - (len(suffix) if k == len(run) - 1 else 0)
            parts.append(_latex(token.glyphs[first:last]))
        latex = None
        if all(p is not None for p in parts):
            latex = safe_latex(" ".join(p for p in parts if p is not None))
        if latex:
            out.append(f"{prefix}${latex}${suffix}")
            converted += 1
        else:
            out.extend(t.text for t in run)
        i = j
    if not converted:
        return None
    return " ".join(out), converted


def inline_math_text(chars: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The text of one line with its inline math written as ``$...$``.

    ``chars`` are the pdfplumber characters of a single visual line.
    Super- and subscripts become ``^{}``/``_{}``, Greek letters and common
    operators become LaTeX commands. Returns None when the line has no
    math the converter is sure about, so callers keep their own text.
    """
    glyphs = [g for g in (_glyph(c) for c in chars) if g is not None]
    if not glyphs:
        return None
    glyphs.sort(key=lambda g: (g.x0, g.x1))
    _mark_scripts(glyphs)
    result = _inline_text(glyphs)
    return result[0] if result else None


def _signature(text: str) -> str:
    return "".join(sorted("".join(unicodedata.normalize("NFKC", text).split())))


def apply_inline_math(
    lines: Sequence[TextLine],
    chars: Sequence[Mapping[str, Any]],
    *,
    form_page: bool = False,
) -> Tuple[List[TextLine], int]:
    """Rewrite inline math in a page's text lines.

    ``lines`` are the page's text lines and ``chars`` the characters they
    were built from. Returns the new line list and the number of formulas
    written. A line is only replaced when the characters found for it are
    exactly the line's own text; script fragments that line extraction
    split off are folded into the line they belong to. Form pages are
    never touched.
    """
    result = list(lines)
    if form_page or not lines or not chars:
        return result, 0
    total = 0
    drop: Set[int] = set()
    index = _LineIndex(lines)
    for visual in math_lines(chars):
        converted = _inline_text(visual.glyphs)
        if converted is None:
            continue
        text, count = converted
        members = [i for i in index.inside(visual.bbox) if i not in drop]
        if not members:
            continue
        raw = "".join(g.raw for g in visual.glyphs)
        if _signature("".join(result[i].text for i in members)) != _signature(raw):
            continue
        keep = max(members, key=lambda i: len(result[i].text))
        group = [result[i] for i in members]
        result[keep] = dataclasses.replace(
            result[keep],
            text=text,
            x0=min(line.x0 for line in group),
            top=min(line.top for line in group),
            x1=max(line.x1 for line in group),
            bottom=max(line.bottom for line in group),
        )
        drop.update(i for i in members if i != keep)
        total += count
    return [line for i, line in enumerate(result) if i not in drop], total
