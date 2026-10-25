"""Text for people to read: money, percents, weights, months and dates.

Months and dates follow the browser's language (``st.context.locale``) so
that what a page shows matches what its date inputs show (31/07/2026 for
en-GB, 07/31/2026 for en-US). Without a browser (tests, scripts) dates stay
YYYY-MM-DD and months read 'Oct 2026'. Every function takes an optional
``locale`` to override the browser's.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re
from decimal import Decimal

# --- money -------------------------------------------------------------------


def cents(value: int | None, *, symbol: str = "$") -> str:
    """-123456 -> '-$1,234.56'; None -> ''."""
    if value is None:
        return ""
    value = int(value)
    sign = "-" if value < 0 else ""
    dollars, rem = divmod(abs(value), 100)
    return f"{sign}{symbol}{dollars:,}.{rem:02d}"


def signed_cents(value: int) -> str:
    """Like :func:`cents` with a '+' on positive amounts: '+$1.00', '$0.00'."""
    return ("+" if value > 0 else "") + cents(value)


def md_cents(value: int | None) -> str:
    """:func:`cents` for Streamlit markdown (st.warning, captions, labels...),
    where two '$' in one string would otherwise be read as a math formula."""
    return cents(value).replace("$", "\\$")


def csv_dollars(value: int) -> str:
    """Plain decimal dollars for CSV: -123456 -> '-1234.56'."""
    sign = "-" if value < 0 else ""
    dollars, rem = divmod(abs(int(value)), 100)
    return f"{sign}{dollars}.{rem:02d}"


def dollars(value: int | None) -> float | None:
    """Cents as dollars for numeric table columns (st.column_config.NumberColumn)."""
    return None if value is None else int(value) / 100


# --- percents and weights ----------------------------------------------------


def percent(value: float) -> str:
    """A percent as entered, every digit kept: 12.125 -> '12.125%', 50.0 -> '50%'."""
    text = repr(float(value))
    return (text[:-2] if text.endswith(".0") else text) + "%"


def weight(value: Decimal) -> str:
    """A room-split weight: '3,100', '1,034.625', '92.75'."""
    text = format(value, ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# --- months and dates --------------------------------------------------------


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


def month(value: str, locale: str | None = None) -> str:
    """'2026-10' -> 'Oct 2026'; anything else is returned unchanged."""
    try:
        year, mon = value.split("-")
        first = _dt.date(int(year), int(mon), 1)
    except (ValueError, IndexError, AttributeError, TypeError):
        return value
    loc = _babel_locale(locale)
    if loc is None:
        return f"{calendar.month_abbr[first.month]} {first.year}"
    from babel.dates import format_skeleton

    return format_skeleton("yMMM", first, locale=loc)


def date_pattern(locale: str | None = None) -> str | None:
    """The locale's numeric date pattern with day and month padded to two
    digits and a four-digit year, as browsers' date inputs show it
    ('dd/MM/yyyy' for en-GB); None when there is no usable locale."""
    loc = _babel_locale(locale)
    if loc is None:
        return None
    from babel.dates import get_date_format

    pattern = get_date_format("short", locale=loc).pattern
    for letter, width in (("d", 2), ("M", 2), ("y", 4)):
        pattern = re.sub(f"{letter}+", letter * width, pattern)
    return pattern


def date(value, locale: str | None = None) -> str:
    """A date ('YYYY-MM-DD', date or datetime); '' for None; text that isn't
    a date is returned unchanged."""
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
    from babel.dates import format_date

    return format_date(value, format=pattern, locale=_babel_locale(locale))


def day_span(first: str, last: str, locale: str | None = None) -> str:
    """Nights within one month as day and month: '11–31 Jul' ('Jul 11 – 31'
    for en-US), or '11 Jul' for a single night."""
    a, b = _dt.date.fromisoformat(first), _dt.date.fromisoformat(last)
    loc = _babel_locale(locale)
    if loc is None:
        mon = b.strftime("%b")
        return f"{a.day} {mon}" if a == b else f"{a.day}–{b.day} {mon}"
    from babel.dates import format_interval, format_skeleton

    if a == b:
        return format_skeleton("MMMd", a, locale=loc)
    return format_interval(a, b, "MMMd", locale=loc)
