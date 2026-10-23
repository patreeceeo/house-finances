"""People: add, rename, colors, status, delete (data layer for the People screen).

Months are 'YYYY-MM' text. Who is in the house comes from room assignments
(housefin.rooms; see db.is_active / db.active_people): a person is in the
house for a month when assigned to any room on any night of it. The House is
always present: it can be renamed but never assigned to a room or deleted.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3

from housefin import colors, db, rooms

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
        "room_assignments": q("SELECT COUNT(*) FROM room_assignments WHERE person_id = ?"),
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
    n_rooms = u["room_assignments"]
    if n_rooms:
        reasons.append(f"{n_rooms} room assignment{'s' if n_rooms != 1 else ''}")
    if reasons:
        return f"{person['name']} has {' and '.join(reasons)}, so they cannot be deleted."
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


def add_person(conn: sqlite3.Connection, name: str) -> int:
    """Add a housemate with a color far from everyone else's. They are not
    added to any remembered split, and are not in the house until assigned
    to a room (housefin.rooms)."""
    name = _clean_name(conn, name)
    return db.add_person(conn, name)


def rename_person(conn: sqlite3.Connection, person_id: int, name: str) -> None:
    """Rename anyone, the House included; history follows (it is keyed by id)."""
    get_person(conn, person_id)
    name = _clean_name(conn, name, exclude_id=person_id)
    with db.transaction(conn):
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))


def new_color(conn: sqlite3.Connection, person_id: int) -> str:
    """Give a person another random color, distinct from everyone else's.
    Returns the new '#rrggbb'."""
    person = get_person(conn, person_id)
    others = [
        r[0] for r in conn.execute(
            "SELECT color FROM people WHERE id != ? AND color IS NOT NULL", (person_id,)
        )
    ]
    color = (
        colors.another_color(person["color"], others)
        if person["color"] else colors.new_color(others)
    )
    with db.transaction(conn):
        conn.execute("UPDATE people SET color = ? WHERE id = ?", (color, person_id))
    return color


def delete_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Delete someone with no transactions or rules. They are also dropped
    from any remembered purpose split (the others keep their weights)."""
    reason = delete_blocker(conn, person_id)
    if reason:
        raise PersonError(reason)
    with db.transaction(conn):
        conn.execute("DELETE FROM purpose_splits WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM people WHERE id = ?", (person_id,))


IN_THE_HOUSE = "In the house"
MOVED_OUT = "Moved out"
NOT_YET_ARRIVED = "Not yet arrived"
AWAY = "Away"
NOT_IN_A_ROOM = "Not in a room"
HOUSE = "House"


def status(conn: sqlite3.Connection, person: sqlite3.Row | int, month: str) -> str:
    """Where a person stands in 'YYYY-MM', from their room assignments:

    * 'House' for the House;
    * 'In the house': in a room on any night of the month;
    * 'Moved out': no room on or after the month, at least one before it;
    * 'Not yet arrived': a room after the month, none before it;
    * 'Away': rooms both before and after the month but none in it;
    * 'Not in a room': no room assignment at all.
    """
    if isinstance(person, int):
        person = get_person(conn, person)
    if person["is_house"]:
        return HOUSE
    first, last = rooms.month_bounds(month)
    before, during, after = conn.execute(
        "SELECT COALESCE(SUM(start_date < :first), 0),"
        " COALESCE(SUM(start_date <= :last AND (end_date IS NULL OR end_date >= :first)), 0),"
        " COALESCE(SUM(end_date IS NULL OR end_date > :last), 0)"
        " FROM room_assignments WHERE person_id = :pid",
        {"first": first, "last": last, "pid": person["id"]},
    ).fetchone()
    if during:
        return IN_THE_HOUSE
    if before and after:
        return AWAY
    if before:
        return MOVED_OUT
    if after:
        return NOT_YET_ARRIVED
    return NOT_IN_A_ROOM


def moved_out(conn: sqlite3.Connection, person_id: int, month: str) -> bool:
    """No room on or after 'YYYY-MM' and at least one before it (the House
    never moves out)."""
    return status(conn, person_id, month) == MOVED_OUT


def current_room(
    conn: sqlite3.Connection, person_id: int, date: str | None = None
) -> sqlite3.Row | None:
    """The room a person is in on ``date`` ('YYYY-MM-DD', default today), or None."""
    room_id = rooms.room_of(conn, person_id, date or _dt.date.today().isoformat())
    return None if room_id is None else rooms.get_room(conn, room_id)


def not_in_a_room(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """People the rooms migration could not place (not in the Rent default
    split), with their old months, who still have no room assignment, by
    name. Columns: id, name, color, active_from, active_until ('YYYY-MM' or
    NULL = not set)."""
    return conn.execute(
        "SELECT p.id, p.name, p.color, o.active_from, o.active_until"
        " FROM old_active_months o JOIN people p ON p.id = o.person_id"
        " WHERE NOT EXISTS (SELECT 1 FROM room_assignments ra WHERE ra.person_id = p.id)"
        " ORDER BY p.name COLLATE NOCASE"
    ).fetchall()


def dismiss_not_in_a_room(conn: sqlite3.Connection, person_id: int) -> None:
    """Drop someone from the "Not in a room" list without giving them a room
    (e.g. they left before any recorded month). Their old months are forgotten."""
    with db.transaction(conn):
        conn.execute("DELETE FROM old_active_months WHERE person_id = ?", (person_id,))
