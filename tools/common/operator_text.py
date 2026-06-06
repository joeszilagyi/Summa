"""Helpers for rendering operator-facing text safely."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ANSI_ESCAPE_SEQUENCE_RE = re.compile(
    r"""
    \x1b
    (?:
        \[[0-?]*[ -/]*[@-~]
        |
        \][^\x1b\x07]*(?:\x07|\x1b\\)
        |
        P.*?\x1b\\
        |
        _.*?\x1b\\
        |
        \^.*?\x1b\\
    )
    """,
    re.VERBOSE | re.DOTALL,
)
NON_PRINTING_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_terminal_escapes(value: str) -> str:
    """Remove terminal escape sequences and control codes from text."""

    text = ANSI_ESCAPE_SEQUENCE_RE.sub("", value)
    return NON_PRINTING_CONTROL_RE.sub("", text)


def format_operator_text_value(value: Any) -> str:
    """Render a value for text output without letting it control the format."""

    if value is None:
        return "null"
    if isinstance(value, str):
        return json.dumps(strip_terminal_escapes(value), ensure_ascii=False)
    if isinstance(value, Path):
        return json.dumps(strip_terminal_escapes(str(value)), ensure_ascii=False)
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return json.dumps(strip_terminal_escapes(str(value)), ensure_ascii=False)
