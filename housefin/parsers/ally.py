"""Ally Bank CSV export parser.

Format: header ``Date, Time, Amount, Type, Description`` (leading spaces after
commas), rows newest first. Amount is signed dollars with cents; Type is
Deposit or Withdrawal and must agree with the sign.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .base import ParsedRow, ParseFormatError, ParseResult, RowError

EXPECTED_HEADER = ["date", "time", "amount", "type", "description"]
_AMOUNT_RE = re.compile(r"^[+-]?\d+(\.\d{1,2})?$")
_TYPES = {"deposit": "Deposit", "withdrawal": "Withdrawal"}


def parse_amount_cents(text: str) -> int:
    """Parse a signed dollar amount like ``-600.00`` into integer cents."""
    s = text.strip().replace(",", "").replace("$", "")
    if not _AMOUNT_RE.match(s):
        raise ValueError(f"unparseable amount {text!r}")
    try:
        return int((Decimal(s) * 100).to_integral_exact())
    except InvalidOperation as e:  # pragma: no cover - guarded by regex
        raise ValueError(f"unparseable amount {text!r}") from e


def _parse_date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"unparseable date {text!r}") from None


def _parse_time(text: str) -> dt.time:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"unparseable time {text!r}")


class AllyParser:
    name = "ally"

    def detect(self, header: list[str]) -> bool:
        return [h.strip().lower() for h in header] == EXPECTED_HEADER

    def parse_file(self, path: str | Path) -> ParseResult:
        # utf-8-sig tolerates a BOM; newline="" is required by the csv module.
        with open(path, encoding="utf-8-sig", newline="") as f:
            return self._parse(f)

    def parse_text(self, text: str) -> ParseResult:
        return self._parse(io.StringIO(text.lstrip("﻿"), newline=""))

    def _parse(self, f) -> ParseResult:
        reader = csv.reader(f, skipinitialspace=True)
        result = ParseResult()
        try:
            header = next(reader)
        except StopIteration:
            raise ParseFormatError("empty file: no header row") from None
        if not self.detect(header):
            raise ParseFormatError(
                f"not an Ally export: header {header!r}, expected "
                "Date, Time, Amount, Type, Description"
            )
        prev_end = reader.line_num
        while True:
            try:
                cells = next(reader)
            except StopIteration:
                break
            except csv.Error as e:
                result.errors.append(RowError(prev_end + 1, f"malformed CSV: {e}"))
                prev_end = reader.line_num
                continue
            line_no = prev_end + 1
            prev_end = reader.line_num
            if not cells or all(not c.strip() for c in cells):
                continue  # blank line
            row, err = self._parse_row(cells, line_no)
            if err:
                result.errors.append(RowError(line_no, err))
            else:
                result.rows.append(row)
        return result

    def _parse_row(self, cells: list[str], line_no: int):
        cells = [c.strip() for c in cells]
        if len(cells) != 5:
            return None, f"expected 5 columns, got {len(cells)}"
        date_s, time_s, amount_s, type_s, desc = cells
        try:
            date = _parse_date(date_s)
            time = _parse_time(time_s)
            cents = parse_amount_cents(amount_s)
        except ValueError as e:
            return None, str(e)
        type_ = _TYPES.get(type_s.lower())
        if type_ is None:
            return None, f"unknown type {type_s!r} (expected Deposit or Withdrawal)"
        # Zero amounts are accepted with either type.
        if (type_ == "Deposit" and cents < 0) or (type_ == "Withdrawal" and cents > 0):
            return None, f"type {type_} disagrees with amount {amount_s}"
        if not desc:
            return None, "empty description"
        return ParsedRow(date, time, cents, type_, desc, line_no), None
