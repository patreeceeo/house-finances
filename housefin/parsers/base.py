"""Bank-format-agnostic parser interface and shared helpers.

A parser turns one CSV export into a list of ``ParsedRow`` plus a list of
``RowError``. Parsers have no database dependency; ingest and rules build on
the helpers here (``normalize_description``, ``fingerprint``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

_WS = re.compile(r"\s+")


def normalize_description(text: str) -> str:
    """Collapse runs of whitespace to a single space and trim; case is kept."""
    return _WS.sub(" ", text).strip()


def fingerprint(
    date: dt.date, time: dt.time, amount_cents: int, description: str
) -> str:
    """SHA-256 hex digest of ``date|time|amount_cents|normalized description``.

    Date is ISO (YYYY-MM-DD), time is HH:MM:SS.
    """
    key = "|".join(
        (
            date.isoformat(),
            time.strftime("%H:%M:%S"),
            str(int(amount_cents)),
            normalize_description(description),
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedRow:
    date: dt.date
    time: dt.time
    amount_cents: int  # signed: positive = money in, negative = money out
    type: str  # e.g. "Deposit" / "Withdrawal"
    description: str  # cell-trimmed original text (not whitespace-collapsed)
    line_no: int  # 1-based physical line in the file (header is line 1)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.date, self.time, self.amount_cents, self.description)


@dataclass(frozen=True)
class RowError:
    line_no: int
    message: str


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


class ParseFormatError(ValueError):
    """The file as a whole is not in the expected format (e.g. bad header)."""


@runtime_checkable
class Parser(Protocol):
    name: str

    def detect(self, header: list[str]) -> bool:
        """Return True if this parser handles a file with this header row."""
        ...

    def parse_text(self, text: str) -> ParseResult: ...

    def parse_file(self, path: str | Path) -> ParseResult: ...
