"""Date and month arithmetic, and the SQL for a transaction's month.

Dates are 'YYYY-MM-DD' text and months 'YYYY-MM' text; both sort and compare
in date order. A transaction counts toward its *effective month*: its
``applies_month`` override if set, else the month of its date.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re
import sqlite3
from collections.abc import Mapping

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def is_month(text) -> bool:
    return bool(_MONTH_RE.match(str(text)))


def current_month(today: _dt.date | None = None) -> str:
    return (today or _dt.date.today()).strftime("%Y-%m")


def add_months(month: str, n: int) -> str:
    """The month ``n`` months after (or before, if negative) ``month``."""
    year, mon = map(int, month.split("-"))
    total = year * 12 + (mon - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_span(first: str, last: str) -> list[str]:
    """Every month from ``first`` to ``last``, inclusive (empty if first > last)."""
    out, m = [], first
    while m <= last:
        out.append(m)
        m = add_months(m, 1)
    return out


def month_bounds(month: str) -> tuple[str, str]:
    """First and last date ('YYYY-MM-DD') of a month."""
    if not is_month(month):
        raise ValueError(f"Month must look like YYYY-MM, not {month!r}.")
    year, mon = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def month_end(month: str) -> str:
    return month_bounds(month)[1]


def shift(date: str, days: int) -> str:
    """The date ``days`` days after (or before, if negative) ``date``."""
    return (_dt.date.fromisoformat(date) + _dt.timedelta(days=days)).isoformat()


def nights(first: str, last: str) -> int:
    """Number of nights from ``first`` to ``last``, both inclusive."""
    return (_dt.date.fromisoformat(last) - _dt.date.fromisoformat(first)).days + 1


# --- transactions' months ------------------------------------------------------------


def month_sql(alias: str | None = None) -> str:
    """SQL for a transaction's effective month, optionally on a table alias."""
    p = f"{alias}." if alias else ""
    return f"COALESCE({p}applies_month, substr({p}date, 1, 7))"


def month_filter_sql(months: list[str] | None, alias: str | None = None) -> tuple[str, list[str]]:
    """WHERE fragment and params keeping transactions in ``months``. None or
    empty means all time, given as ('1', []) so it can always be AND-ed in."""
    if not months:
        return "1", []
    return f"{month_sql(alias)} IN ({', '.join('?' * len(months))})", list(months)


def month_of(row: sqlite3.Row | Mapping) -> str:
    """The effective month of a transactions row."""
    return row["applies_month"] or row["date"][:7]


def months_present(conn: sqlite3.Connection) -> list[str]:
    """Months that have transactions, newest first."""
    rows = conn.execute(f"SELECT DISTINCT {month_sql()} AS m FROM transactions ORDER BY m DESC")
    return [r[0] for r in rows]
