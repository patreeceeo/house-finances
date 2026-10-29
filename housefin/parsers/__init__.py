"""CSV parsers for bank exports. Add a new format by implementing ``Parser``
(see base.py) and appending an instance to ``PARSERS``."""

from __future__ import annotations

import csv
from pathlib import Path

from .ally import AllyParser
from .base import (
    ParsedRow,
    ParseFormatError,
    Parser,
    ParseResult,
    RowError,
    fingerprint,
    normalize_description,
)

PARSERS: list[Parser] = [AllyParser()]


def detect_parser(path: str | Path) -> Parser:
    """Pick the parser whose ``detect`` accepts the file's header row."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return detect_parser_text(path, f.read())


def detect_parser_text(name: str | Path, text: str) -> Parser:
    """:func:`detect_parser` for a file's text (``name`` is for messages)."""
    header = next(csv.reader(text.splitlines()), None)
    if header is None:
        raise ParseFormatError(f"{name}: empty file")
    for p in PARSERS:
        if p.detect(header):
            return p
    raise ParseFormatError(f"{name}: unrecognized CSV header {header!r}")


def parse_file(path: str | Path) -> ParseResult:
    """Detect the format and parse the file."""
    return detect_parser(path).parse_file(path)


__all__ = [
    "AllyParser",
    "PARSERS",
    "ParseFormatError",
    "ParseResult",
    "ParsedRow",
    "Parser",
    "RowError",
    "detect_parser",
    "detect_parser_text",
    "fingerprint",
    "normalize_description",
    "parse_file",
]
