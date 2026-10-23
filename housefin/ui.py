"""Shared helpers for the Streamlit pages.

The pure helpers (formatting, month lists, SQL fragments) have no Streamlit
dependency at call time so they can be unit tested; the widget helpers import
streamlit lazily.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from housefin import db

ALL = "All"

# 'YYYY-MM' a transaction counts toward: its applies_month override, else the
# month of its date. Use with an optional table alias via month_expr().
TX_MONTH_SQL = "COALESCE(applies_month, substr(date, 1, 7))"


def month_expr(alias: str | None = None) -> str:
    """The transaction-month SQL expression, optionally qualified by a table alias."""
    if not alias:
        return TX_MONTH_SQL
    return f"COALESCE({alias}.applies_month, substr({alias}.date, 1, 7))"


def db_path() -> Path:
    """Database file: env HOUSEFIN_DB if set, else finances.db at the repo root."""
    env = os.environ.get("HOUSEFIN_DB")
    return Path(env) if env else db.DEFAULT_DB_PATH


# --- money -------------------------------------------------------------------


def format_cents(cents: int | None, *, symbol: str = "$") -> str:
    """-123456 -> '-$1,234.56'; None -> ''."""
    if cents is None:
        return ""
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}{symbol}{dollars:,}.{rem:02d}"


def format_percent(value: float) -> str:
    """A percent as entered, every digit kept: 12.125 -> '12.125%', 50.0 -> '50%'."""
    text = repr(float(value))
    return (text[:-2] if text.endswith(".0") else text) + "%"


def parse_dollars(text: str | float | int) -> int:
    """'$1,234.56' / '-12.5' / 12.5 -> integer cents (rounded half away from zero).

    Raises ValueError on input that is not a number.
    """
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

    if isinstance(text, (int, float)):
        raw = str(text)
    else:
        raw = text.strip().replace(",", "").replace("$", "").replace(" ", "")
        if raw.startswith("(") and raw.endswith(")"):
            raw = "-" + raw[1:-1]
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def cents_to_dollars(cents: int | None) -> float | None:
    """For numeric table columns (st.column_config.NumberColumn)."""
    return None if cents is None else int(cents) / 100


# --- months ------------------------------------------------------------------


def months_present(conn: sqlite3.Connection) -> list[str]:
    """Distinct transaction months ('YYYY-MM'), newest first."""
    rows = conn.execute(
        f"SELECT DISTINCT {TX_MONTH_SQL} AS m FROM transactions ORDER BY m DESC"
    ).fetchall()
    return [r[0] for r in rows]


def format_month(month: str, locale: str | None = None) -> str:
    """'2026-10' -> 'Oct 2026' in the browser's language (or ``locale``);
    anything else is returned unchanged."""
    import calendar
    import datetime as _dt

    try:
        year, mon = month.split("-")
        first = _dt.date(int(year), int(mon), 1)
    except (ValueError, IndexError, AttributeError, TypeError):
        return month
    loc = _babel_locale(locale)
    if loc is None:
        return f"{calendar.month_abbr[first.month]} {first.year}"
    from babel.dates import format_skeleton

    return format_skeleton("yMMM", first, locale=loc)


# --- dates -------------------------------------------------------------------
# Dates are shown in the browser's language, as its own date input shows them
# (e.g. 31/07/2026 for en-GB, 07/31/2026 for en-US), so a date cell reads the
# same whether or not it is being edited. Without a browser (tests, scripts)
# dates stay YYYY-MM-DD.


def browser_locale() -> str | None:
    """The browser's language ('en-GB'), or None outside a browser session."""
    try:
        import streamlit as st

        return st.context.locale
    except Exception:
        return None


def _babel_locale(locale: str | None):
    from babel import Locale, UnknownLocaleError

    tag = locale or browser_locale()
    if not tag:
        return None
    try:
        return Locale.parse(tag, sep="-" if "-" in tag else "_")
    except (UnknownLocaleError, ValueError):
        return None


def date_pattern(locale: str | None = None) -> str | None:
    """The locale's numeric date pattern with day and month padded to two
    digits and a four-digit year, as browsers' date inputs show it
    ('dd/MM/yyyy' for en-GB); None when there is no usable locale."""
    import re

    loc = _babel_locale(locale)
    if loc is None:
        return None
    from babel.dates import get_date_format

    pattern = get_date_format("short", locale=loc).pattern
    for letter, width in (("d", 2), ("M", 2), ("y", 4)):
        pattern = re.sub(f"{letter}+", letter * width, pattern)
    return pattern


def format_date(value, locale: str | None = None) -> str:
    """A date ('YYYY-MM-DD', date or datetime) in the browser's format; '' for
    None; text that isn't a date is returned unchanged."""
    import datetime as _dt

    if value is None or value == "":
        return ""
    if isinstance(value, _dt.datetime):
        value = value.date()
    elif not isinstance(value, _dt.date):
        try:
            value = _dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)
    pattern = date_pattern(locale)
    if pattern is None:
        return value.isoformat()
    from babel.dates import format_date as _format

    return _format(value, format=pattern, locale=_babel_locale(locale))


def format_day_span(first: str, last: str, locale: str | None = None) -> str:
    """Nights within one month as day and month: '11–31 Jul' ('Jul 11 – 31'
    for en-US), or '11 Jul' for a single night."""
    import datetime as _dt

    a, b = _dt.date.fromisoformat(first), _dt.date.fromisoformat(last)
    loc = _babel_locale(locale)
    if loc is None:
        mon = b.strftime("%b")
        return f"{a.day} {mon}" if a == b else f"{a.day}–{b.day} {mon}"
    from babel.dates import format_interval, format_skeleton

    if a == b:
        return format_skeleton("MMMd", a, locale=loc)
    return format_interval(a, b, "MMMd", locale=loc)


def date_column_format(locale: str | None = None) -> str:
    """The date pattern as a moment.js format for st.column_config.DateColumn."""
    pattern = date_pattern(locale)
    if pattern is None:
        return "YYYY-MM-DD"
    out, literal = [], False
    for part in pattern.split("'"):
        if literal:
            out.append(f"[{part}]" if part else "'")
        else:
            out.append(part.replace("dd", "DD").replace("yyyy", "YYYY"))
        literal = not literal
    return "".join(out)


def date_column(label: str, **kwargs):
    """A DateColumn shown in the browser's date format."""
    import streamlit as st

    return st.column_config.DateColumn(label, format=date_column_format(), **kwargs)


def to_dates(values) -> list:
    """'YYYY-MM-DD' strings -> datetime.date (None stays None), for date columns."""
    import datetime as _dt

    return [None if not v else _dt.date.fromisoformat(str(v)[:10]) for v in values]


def month_filter_sql(months: list[str] | None, alias: str | None = None) -> tuple[str, list[str]]:
    """WHERE fragment and params for a month selection; None/[] means all time.

    Returns ('1', []) for all time so it can always be AND-ed in.
    """
    if not months:
        return "1", []
    placeholders = ", ".join("?" for _ in months)
    return f"{month_expr(alias)} IN ({placeholders})", list(months)


def normalize_month_selection(choice: str, picked: list[str]) -> list[str] | None:
    """Selection from the month widget: None for All, else sorted months.

    Picking "Months" with nothing selected falls back to All.
    """
    if choice == ALL or not picked:
        return None
    return sorted(picked)


# --- Streamlit helpers -------------------------------------------------------


def get_conn() -> sqlite3.Connection:
    """One DB connection per browser session (Streamlit reruns reuse it)."""
    import streamlit as st

    path = str(db_path())
    conn = st.session_state.get("_housefin_conn")
    if conn is None or st.session_state.get("_housefin_conn_path") != path:
        conn = connect_threadsafe(path)
        st.session_state["_housefin_conn"] = conn
        st.session_state["_housefin_conn_path"] = path
    return conn


def connect_threadsafe(path: str | Path) -> sqlite3.Connection:
    """Like db.connect, but usable from the different threads Streamlit may run
    a session's reruns on (only one rerun per session runs at a time)."""
    db.connect(path).close()  # migrate + seed
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def month_selector(
    conn: sqlite3.Connection, *, key: str = "months", container=None
) -> list[str] | None:
    """Sidebar month picker. Returns None for All (default) or a sorted month list.

    The choice persists across pages: Streamlit drops widget state for widgets
    not rendered on the current page, so it is mirrored in non-widget keys.
    """
    import streamlit as st

    where = container if container is not None else st.sidebar
    ss = st.session_state
    mode_key, picked_key = f"_{key}_mode", f"_{key}_picked"
    months = months_present(conn)
    # Restore from the mirror (dropping months no longer in the data).
    if mode_key not in ss:
        ss[mode_key] = ss.get(f"{key}_saved_mode", ALL)
    if picked_key not in ss:
        ss[picked_key] = [m for m in ss.get(f"{key}_saved_picked", []) if m in months]

    choice = where.radio(
        "Month", [ALL, "Months"], horizontal=True, key=mode_key,
        help="All time, or pick one or more months present in the data.",
    )
    picked: list[str] = []
    if choice != ALL:
        picked = where.multiselect(
            "Months", months, format_func=format_month, key=picked_key,
            placeholder="Choose months",
        )
        if not picked:
            where.caption("No months picked; showing all time.")
    ss[f"{key}_saved_mode"] = choice
    ss[f"{key}_saved_picked"] = list(ss.get(picked_key, []))
    return normalize_month_selection(choice, picked)


def describe_months(months: list[str] | None) -> str:
    if not months:
        return "All time"
    return ", ".join(format_month(m) for m in months)
