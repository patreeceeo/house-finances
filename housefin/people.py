"""People: add, rename, move out, delete (data layer for the People screen).

Months are 'YYYY-MM' text; active_from / active_until are both inclusive and
NULL means unbounded (see db.is_active / db.active_people). The House is
always present: it can be renamed but never moved out or deleted.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class PersonError(ValueError):
    """A people change that is not allowed (message is user-facing)."""


# --- months --------------------------------------------------------------------


def check_month(month: str | None) -> str | None:
    """Validate a 'YYYY-MM' month (None passes through)."""
    if month is None:
        return None
    month = str(month).strip()
    if not _MONTH_RE.match(month):
        raise PersonError(f"Month must look like YYYY-MM, not {month!r}.")
    return month


def add_months(month: str, n: int) -> str:
    year, mon = map(int, month.split("-"))
    total = year * 12 + (mon - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def current_month(today: _dt.date | None = None) -> str:
    today = today or _dt.date.today()
    return f"{today.year:04d}-{today.month:02d}"


def month_range(start: str, end: str) -> list[str]:
    """Inclusive list of months from start to end (empty if start > end)."""
    out, m = [], start
    while m <= end:
        out.append(m)
        m = add_months(m, 1)
    return out


def month_choices(
    conn: sqlite3.Connection, *, today: _dt.date | None = None, extra: tuple[str | None, ...] = ()
) -> list[str]:
    """Months to offer in pickers, newest first: from the earliest transaction
    (or 24 months back) through 12 months ahead, plus any `extra` months."""
    now = current_month(today)
    start = add_months(now, -24)
    first = conn.execute(
        "SELECT MIN(COALESCE(applies_month, substr(date, 1, 7))) FROM transactions"
    ).fetchone()[0]
    if first and first < start:
        start = first
    months = set(month_range(start, add_months(now, 12)))
    months.update(m for m in extra if m)
    return sorted(months, reverse=True)


# --- lookups -------------------------------------------------------------------


def get_person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise PersonError(f"No person with id {person_id}.")
    return row


def usage(conn: sqlite3.Connection, person_id: int) -> dict[str, int]:
    """How often a person is referenced: tagged deposits/transactions, split
    shares, rules and remembered (purpose) splits."""
    q = lambda sql: conn.execute(sql, (person_id,)).fetchone()[0]  # noqa: E731
    return {
        "transactions": q("SELECT COUNT(*) FROM transactions WHERE person_id = ?"),
        "splits": q("SELECT COUNT(*) FROM splits WHERE person_id = ?"),
        "rules": q("SELECT COUNT(*) FROM rules WHERE person_id = ?"),
        "purpose_splits": q("SELECT COUNT(*) FROM purpose_splits WHERE person_id = ?"),
    }


def delete_blocker(conn: sqlite3.Connection, person_id: int) -> str | None:
    """Why this person cannot be deleted, or None if they can."""
    person = get_person(conn, person_id)
    if person["is_house"]:
        return "The House cannot be deleted."
    u = usage(conn, person_id)
    reasons = []
    if u["transactions"] or u["splits"]:
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE person_id = :p"
            " OR id IN (SELECT transaction_id FROM splits WHERE person_id = :p)",
            {"p": person_id},
        ).fetchone()[0]
        reasons.append(f"{n} transaction{'s' if n != 1 else ''}")
    if u["rules"]:
        reasons.append(f"{u['rules']} rule{'s' if u['rules'] != 1 else ''}")
    if reasons:
        return (
            f"{person['name']} has {' and '.join(reasons)}; move them out instead."
        )
    return None


def can_delete(conn: sqlite3.Connection, person_id: int) -> bool:
    return delete_blocker(conn, person_id) is None


# --- changes -------------------------------------------------------------------


def _clean_name(conn: sqlite3.Connection, name: str, exclude_id: int | None = None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise PersonError("Name cannot be empty.")
    clash = conn.execute(
        "SELECT id FROM people WHERE lower(name) = lower(?) AND id IS NOT ?",
        (name, exclude_id),
    ).fetchone()
    if clash is not None:
        raise PersonError(f"There is already a person named {name!r}.")
    return name


def add_person(conn: sqlite3.Connection, name: str, active_from: str | None = None) -> int:
    """Add a housemate. They are not added to any remembered split."""
    name = _clean_name(conn, name)
    active_from = check_month(active_from)
    with conn:
        cur = conn.execute(
            "INSERT INTO people (name, active_from) VALUES (?, ?)", (name, active_from)
        )
    return cur.lastrowid


def rename_person(conn: sqlite3.Connection, person_id: int, name: str) -> None:
    """Rename anyone, the House included; history follows (it is keyed by id)."""
    get_person(conn, person_id)
    name = _clean_name(conn, name, exclude_id=person_id)
    with conn:
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))


def set_active_from(conn: sqlite3.Connection, person_id: int, month: str | None) -> None:
    person = get_person(conn, person_id)
    if person["is_house"]:
        raise PersonError("The House is always active.")
    month = check_month(month)
    until = person["active_until"]
    if month and until and month > until:
        raise PersonError("Active from cannot be after the move-out month.")
    with conn:
        conn.execute("UPDATE people SET active_from = ? WHERE id = ?", (month, person_id))


def move_out(conn: sqlite3.Connection, person_id: int, last_month: str) -> None:
    """Set the last month (inclusive) a person is active; from the following
    month they leave the dropdowns. History is untouched."""
    person = get_person(conn, person_id)
    if person["is_house"]:
        raise PersonError("The House cannot be moved out.")
    last_month = check_month(last_month)
    if last_month is None:
        raise PersonError("Pick the last month they lived here.")
    start = person["active_from"]
    if start and last_month < start:
        raise PersonError("The move-out month cannot be before their Active from month.")
    with conn:
        conn.execute("UPDATE people SET active_until = ? WHERE id = ?", (last_month, person_id))


def clear_move_out(conn: sqlite3.Connection, person_id: int) -> None:
    """Undo a move-out (they become current again)."""
    get_person(conn, person_id)
    with conn:
        conn.execute("UPDATE people SET active_until = NULL WHERE id = ?", (person_id,))


def set_active_range(
    conn: sqlite3.Connection, person_id: int, active_from: str | None, active_until: str | None
) -> None:
    """Set both bounds at once (None = unbounded / still here)."""
    person = get_person(conn, person_id)
    if person["is_house"]:
        raise PersonError("The House is always active and cannot be moved out.")
    active_from, active_until = check_month(active_from), check_month(active_until)
    if active_from and active_until and active_from > active_until:
        raise PersonError("Active from cannot be after Active until.")
    with conn:
        conn.execute(
            "UPDATE people SET active_from = ?, active_until = ? WHERE id = ?",
            (active_from, active_until, person_id),
        )


def delete_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Delete someone with no transactions or rules. They are also dropped
    from any remembered purpose split (the others keep their weights)."""
    reason = delete_blocker(conn, person_id)
    if reason:
        raise PersonError(reason)
    with conn:
        conn.execute("DELETE FROM purpose_splits WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM people WHERE id = ?", (person_id,))


def status(person: sqlite3.Row, month: str) -> str:
    """'House', 'Active', 'Moved out' or 'Not yet active' as of `month`."""
    if person["is_house"]:
        return "House"
    if person["active_until"] and person["active_until"] < month:
        return "Moved out"
    if person["active_from"] and person["active_from"] > month:
        return "Not yet active"
    return "Active"
