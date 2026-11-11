# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""YAML scalars for front matter blocks."""

import re
from typing import Any

# Characters a YAML stream may not contain raw, plus the Unicode line breaks
# a YAML parser would normalize.
_YAML_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff\ufffe\uffff]")


def escape_yaml_controls(text: str) -> str:
    """``text`` (the inside of a double-quoted scalar) with every character
    YAML rejects written as a ``\\uXXXX`` escape."""
    return _YAML_UNSAFE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def yaml_scalar(value: Any) -> str:
    """Render a scalar value as a YAML string."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        rendered = repr(value)
        if isinstance(value, float) and "e" in rendered:
            mantissa, exponent = rendered.split("e", 1)
            # YAML 1.1 requires a decimal point in exponential float mantissas.
            if "." not in mantissa:
                mantissa += ".0"
            rendered = f"{mantissa}e{exponent}"
        return rendered
    s = str(value)
    # Quote when necessary: contains special chars, leading/trailing whitespace,
    # or characters that would confuse a YAML parser.
    if (
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9 _./-]*", s)
        or s != s.strip()
        or s.lower() in ("null", "true", "false", "yes", "no", "on", "off", "y", "n")
    ):
        # Escape backslashes, double quotes, and common whitespace characters.
        escaped = (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        # Quoting alone does not make YAML control characters valid.
        return f'"{escape_yaml_controls(escaped)}"'
    return s
