"""Rooms and who was in them (data layer for the Rooms screen and rent split).

See docs/rooms-spec.md. Each room carries a fixed percent of the rent; all
rooms together must total exactly 100% (checked like split percents, as the
decimals entered). People are assigned to rooms for date ranges.

Dates are 'YYYY-MM-DD' text and stand for *nights*: both ends of an
assignment are inclusive, and a NULL ``end_date`` means still there
(ongoing). Someone moving out on the 10th has the 9th as their last night.

Rules enforced here (messages are user-facing and name who clashes, when):

* a room's assignments never overlap (one person per room at a time);
* a person is in at most one room on any night;
* the House is never assigned to a room.

Nothing here guesses: a day nobody is assigned to a room is *vacant*, and
vacancies are reported (:func:`vacancies`, :func:`month_occupancy`) for a
person to decide about, never filled in or scaled away.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from housefin import db, splits

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# Stand-in end for an ongoing assignment in comparisons ('YYYY-MM-DD' text
# compares in date order).
_FOREVER = "9999-12-31"


class RoomError(ValueError):
    """A rooms change that is not allowed (message is user-facing)."""


# --- dates ---------------------------------------------------------------------


def check_date(value: str | None, *, label: str = "Date") -> str | None:
    """Validate a 'YYYY-MM-DD' date (None and blank pass through as None)."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if not _DATE_RE.match(value):
        raise RoomError(f"{label} must look like YYYY-MM-DD, not {value!r}.")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise RoomError(f"{label} {value!r} is not a real date.") from None
    return value


def _day(value: str) -> _dt.date:
    return _dt.date.fromisoformat(value)


def _shift(value: str, days: int) -> str:
    return (_day(value) + _dt.timedelta(days=days)).isoformat()


def _nights(start: str, end: str) -> int:
    """Number of nights from start to end, both inclusive."""
    return (_day(end) - _day(start)).days + 1


def month_bounds(month: str) -> tuple[str, str]:
    """First and last date of a 'YYYY-MM' month."""
    if not _MONTH_RE.match(str(month)):
        raise RoomError(f"Month must look like YYYY-MM, not {month!r}.")
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def describe_span(start: str, end: str | None) -> str:
    """'2026-07-01 to 2026-07-31', '2026-07-01' (one night) or
    '2026-07-01 onwards (ongoing)', with dates in the browser's format."""
    from housefin import ui

    start, end = ui.format_date(start), None if end is None else ui.format_date(end)
    if end is None:
        return f"{start} onwards (ongoing)"
    if end == start:
        return start
    return f"{start} to {end}"


# --- rooms -----------------------------------------------------------------------


def list_rooms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All rooms, by name."""
    return conn.execute("SELECT * FROM rooms ORDER BY name").fetchall()


def get_room(conn: sqlite3.Connection, room_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None:
        raise RoomError(f"No room with id {room_id}.")
    return row


def _clean_room_name(conn: sqlite3.Connection, name: str, exclude_id: int | None = None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise RoomError("Room name cannot be empty.")
    clash = conn.execute(
        "SELECT id FROM rooms WHERE lower(name) = lower(?) AND id IS NOT ?",
        (name, exclude_id),
    ).fetchone()
    if clash is not None:
        raise RoomError(f"There is already a room named {name!r}.")
    return name


def _clean_percent(percent, room_name: str) -> float:
    try:
        value = float(percent)
    except (TypeError, ValueError):
        raise RoomError(f"{room_name}: percent must be a number, not {percent!r}.") from None
    if not math.isfinite(value) or value <= 0:
        raise RoomError(f"{room_name}: percent must be more than 0.")
    return value


def add_room(conn: sqlite3.Connection, name: str, percent: float) -> int:
    """Add a room with its percent of the rent. Percents are not required to
    total 100 after this alone (adding a room always changes the total); the
    Rooms screen shows :func:`percent_error` until they are saved together
    with :func:`set_room_percents`."""
    name = _clean_room_name(conn, name)
    percent = _clean_percent(percent, name)
    with db.transaction(conn):
        cur = conn.execute("INSERT INTO rooms (name, percent) VALUES (?, ?)", (name, percent))
    return cur.lastrowid


def rename_room(conn: sqlite3.Connection, room_id: int, name: str) -> None:
    get_room(conn, room_id)
    name = _clean_room_name(conn, name, exclude_id=room_id)
    with db.transaction(conn):
        conn.execute("UPDATE rooms SET name = ? WHERE id = ?", (name, room_id))


def delete_room(conn: sqlite3.Connection, room_id: int) -> None:
    """Delete a room nobody has ever been assigned to. A room with history
    keeps it (its assignments decide past rent)."""
    room = get_room(conn, room_id)
    n = conn.execute(
        "SELECT COUNT(*) FROM room_assignments WHERE room_id = ?", (room_id,)
    ).fetchone()[0]
    if n:
        raise RoomError(
            f"{room['name']} has {n} assignment{'s' if n != 1 else ''}; "
            "delete those first."
        )
    with db.transaction(conn):
        conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))


def percent_error(conn: sqlite3.Connection) -> str | None:
    """Why the stored room percents can't be used (they don't total exactly
    100%, or there are no rooms), or None when they are fine."""
    rooms = list_rooms(conn)
    if not rooms:
        return "There are no rooms yet."
    return splits.percent_total_error(r["percent"] for r in rooms)


def set_room_percents(conn: sqlite3.Connection, percents: Mapping[int, float]) -> None:
    """Save every room's percent together: ``{room_id: percent}`` must name
    each room exactly once, every percent above 0, totalling exactly 100%
    (as the decimals entered; nothing is rounded or rescaled). Rent splits
    already written keep their weights."""
    rooms = {r["id"]: r for r in list_rooms(conn)}
    given = {int(k): v for k, v in percents.items()}
    unknown = sorted(set(given) - set(rooms))
    if unknown:
        raise RoomError(f"No room with id {unknown[0]}.")
    missing = [rooms[i]["name"] for i in rooms if i not in given]
    if missing:
        raise RoomError(f"Give a percent for every room (missing: {', '.join(sorted(missing))}).")
    clean = {i: _clean_percent(p, rooms[i]["name"]) for i, p in given.items()}
    # Check the values as entered (e.g. '33.375'), not their float images.
    error = splits.percent_total_error(given[i] for i in clean)
    if error:
        raise RoomError(error)
    with db.transaction(conn):
        conn.executemany(
            "UPDATE rooms SET percent = ? WHERE id = ?", [(p, i) for i, p in clean.items()]
        )


# --- assignments -------------------------------------------------------------------


_ASSIGNMENT_SQL = """
SELECT a.*, r.name AS room_name, p.name AS person_name
  FROM room_assignments a
  JOIN rooms r ON r.id = a.room_id
  JOIN people p ON p.id = a.person_id
"""


def list_assignments(
    conn: sqlite3.Connection,
    room_id: int | None = None,
    *,
    person_id: int | None = None,
) -> list[sqlite3.Row]:
    """Assignments (with ``room_name`` and ``person_name``), newest first by
    first night; optionally only one room's and/or one person's."""
    where, params = [], {}
    if room_id is not None:
        where.append("a.room_id = :room")
        params["room"] = room_id
    if person_id is not None:
        where.append("a.person_id = :person")
        params["person"] = person_id
    sql = _ASSIGNMENT_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY a.start_date DESC, a.id DESC", params).fetchall()


def get_assignment(conn: sqlite3.Connection, assignment_id: int) -> sqlite3.Row:
    row = conn.execute(_ASSIGNMENT_SQL + " WHERE a.id = ?", (assignment_id,)).fetchone()
    if row is None:
        raise RoomError(f"No room assignment with id {assignment_id}.")
    return row


def _overlapping(
    conn: sqlite3.Connection,
    column: str,
    value: int,
    start: str,
    end: str | None,
    exclude_id: int | None,
) -> sqlite3.Row | None:
    """The earliest assignment with ``column = value`` sharing a night with
    start..end (inclusive), other than ``exclude_id``."""
    return conn.execute(
        _ASSIGNMENT_SQL
        + f" WHERE a.{column} = :v AND a.id IS NOT :x"
        "   AND a.start_date <= :end AND COALESCE(a.end_date, :forever) >= :start"
        " ORDER BY a.start_date LIMIT 1",
        {"v": value, "x": exclude_id, "start": start, "end": end or _FOREVER,
         "forever": _FOREVER},
    ).fetchone()


def _validate(
    conn: sqlite3.Connection,
    room_id: int,
    person_id: int,
    start_date: str | None,
    end_date: str | None,
    exclude_id: int | None = None,
) -> tuple[str, str | None]:
    room = get_room(conn, room_id)
    person = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if person is None:
        raise RoomError(f"No person with id {person_id}.")
    if person["is_house"]:
        raise RoomError("The House can't be assigned to a room.")
    start = check_date(start_date, label="First night")
    if start is None:
        raise RoomError("Pick the first night in the room.")
    end = check_date(end_date, label="Last night")
    if end is not None and end < start:
        raise RoomError(f"Last night ({end}) can't be before the first night ({start}).")
    clash = _overlapping(conn, "room_id", room_id, start, end, exclude_id)
    if clash is not None:
        raise RoomError(
            f"{room['name']} already has {clash['person_name']} "
            f"{describe_span(clash['start_date'], clash['end_date'])}; "
            "a room holds one person at a time."
        )
    clash = _overlapping(conn, "person_id", person_id, start, end, exclude_id)
    if clash is not None:
        raise RoomError(
            f"{person['name']} is already in {clash['room_name']} "
            f"{describe_span(clash['start_date'], clash['end_date'])}; "
            "a person can be in only one room a night."
        )
    return start, end


def add_assignment(
    conn: sqlite3.Connection,
    room_id: int,
    person_id: int,
    start_date: str,
    end_date: str | None = None,
) -> int:
    """Put a person in a room from ``start_date`` to ``end_date`` (nights,
    inclusive; None = ongoing). Refused if it overlaps the room's or the
    person's other assignments, or if the person is the House."""
    start, end = _validate(conn, room_id, person_id, start_date, end_date)
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO room_assignments (room_id, person_id, start_date, end_date)"
            " VALUES (?, ?, ?, ?)",
            (room_id, person_id, start, end),
        )
    return cur.lastrowid


def update_assignment(
    conn: sqlite3.Connection,
    assignment_id: int,
    room_id: int,
    person_id: int,
    start_date: str,
    end_date: str | None = None,
) -> None:
    """Replace an assignment's room, person and dates (same checks as
    :func:`add_assignment`). Editing a seeded row counts as checking it."""
    get_assignment(conn, assignment_id)
    start, end = _validate(conn, room_id, person_id, start_date, end_date, assignment_id)
    with db.transaction(conn):
        conn.execute(
            "UPDATE room_assignments SET room_id = ?, person_id = ?, start_date = ?,"
            " end_date = ?, seeded = 0 WHERE id = ?",
            (room_id, person_id, start, end, assignment_id),
        )


def confirm_assignment(conn: sqlite3.Connection, assignment_id: int) -> None:
    """Mark a seeded assignment as checked by a person, unchanged."""
    get_assignment(conn, assignment_id)
    with db.transaction(conn):
        conn.execute("UPDATE room_assignments SET seeded = 0 WHERE id = ?", (assignment_id,))


def delete_assignment(conn: sqlite3.Connection, assignment_id: int) -> None:
    get_assignment(conn, assignment_id)
    with db.transaction(conn):
        conn.execute("DELETE FROM room_assignments WHERE id = ?", (assignment_id,))


# --- who was where -----------------------------------------------------------------


def _gaps(
    spans: list[tuple[str, str | None]], start: str | None, end: str | None
) -> list[tuple[str, str | None]]:
    """Uncovered nights in start..end (None = unbounded) given non-overlapping
    ``(start, end)`` spans. With no lower bound, nights before the first span
    are not a gap (the room's history starts there)."""
    out: list[tuple[str, str | None]] = []
    cursor = start  # first night not yet accounted for
    for s, e in sorted(spans):
        if end is not None and s > end:
            break
        if e is not None and start is not None and e < start:
            continue
        if cursor is not None and s > cursor:
            out.append((cursor, _shift(s, -1)))
        if e is None:
            return out
        cursor = max(cursor, _shift(e, 1)) if cursor else _shift(e, 1)
    if cursor is not None and (end is None or cursor <= end):
        out.append((cursor, end))
    return out


def _room_spans(conn: sqlite3.Connection, room_id: int) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        "SELECT start_date, end_date FROM room_assignments WHERE room_id = ?", (room_id,)
    ).fetchall()
    return [(r["start_date"], r["end_date"]) for r in rows]


def vacancies(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[tuple[str, str | None]]:
    """Vacant ``(first_night, last_night)`` ranges for a room, oldest first;
    ``last_night`` None = vacant from then on.

    Without bounds: the gaps between its assignments, plus from the night
    after the last one ends when nobody is in it now. With ``start`` /
    ``end`` (inclusive dates), every vacant night in that window, clipped to
    it. A room with no assignments at all and no ``start`` has no known
    vacancy (its history hasn't begun)."""
    get_room(conn, room_id)
    start = check_date(start, label="Start")
    end = check_date(end, label="End")
    if start and end and end < start:
        return []
    return _gaps(_room_spans(conn, room_id), start, end)


def occupants(conn: sqlite3.Connection, date: str) -> dict[int, int | None]:
    """``{room_id: person_id or None (vacant)}`` for every room on a night."""
    date = check_date(date)
    rows = conn.execute(
        "SELECT r.id AS room_id, a.person_id FROM rooms r"
        " LEFT JOIN room_assignments a ON a.room_id = r.id"
        "  AND a.start_date <= :d AND COALESCE(a.end_date, :forever) >= :d",
        {"d": date, "forever": _FOREVER},
    ).fetchall()
    return {r["room_id"]: r["person_id"] for r in rows}


def room_of(conn: sqlite3.Connection, person_id: int, date: str) -> int | None:
    """The room a person is in on a night, or None."""
    date = check_date(date)
    row = conn.execute(
        "SELECT room_id FROM room_assignments WHERE person_id = :p"
        " AND start_date <= :d AND COALESCE(end_date, :forever) >= :d",
        {"p": person_id, "d": date, "forever": _FOREVER},
    ).fetchone()
    return None if row is None else row["room_id"]


def timeline(conn: sqlite3.Connection) -> list[dict]:
    """Read-only history: one row per date on which any room changes hands,
    newest first. Each row is ``{"date": 'YYYY-MM-DD', "rooms": {room_id:
    person_id or None}}`` giving who is in each room from that night until
    the next row's date (None = vacant). Dates where nothing actually changes
    (an assignment ending and the same person's next one in the same room
    starting the next night) get no row."""
    rows = conn.execute("SELECT start_date, end_date FROM room_assignments").fetchall()
    dates = set()
    for r in rows:
        dates.add(r["start_date"])
        if r["end_date"] is not None:
            dates.add(_shift(r["end_date"], 1))
    out: list[dict] = []
    previous = None
    for d in sorted(dates):
        state = occupants(conn, d)
        if state != previous:
            out.append({"date": d, "rooms": state})
            previous = state
    out.reverse()
    return out


@dataclass
class RoomMonth:
    """One room's nights in a month: who had how many, and which were vacant."""

    room_id: int
    name: str
    percent: float
    days: dict[int, int] = field(default_factory=dict)  # person_id -> nights
    vacant: list[tuple[str, str]] = field(default_factory=list)  # inclusive ranges

    @property
    def vacant_days(self) -> int:
        return sum(_nights(s, e) for s, e in self.vacant)


@dataclass
class MonthOccupancy:
    month: str  # 'YYYY-MM'
    days_in_month: int
    rooms: list[RoomMonth]

    def person_days(self) -> dict[int, int]:
        """Nights per person across all rooms."""
        out: dict[int, int] = {}
        for room in self.rooms:
            for pid, n in room.days.items():
                out[pid] = out.get(pid, 0) + n
        return out

    @property
    def fully_occupied(self) -> bool:
        return all(room.vacant_days == 0 for room in self.rooms)


def month_occupancy(conn: sqlite3.Connection, month: str) -> MonthOccupancy:
    """Who was in each room on each night of a 'YYYY-MM' month: nights per
    person per room, and the vacant nights (as date ranges) per room. For
    every room, its people's nights plus its vacant nights equal the days in
    the month."""
    first, last = month_bounds(month)
    rooms = []
    for room in list_rooms(conn):
        rm = RoomMonth(room["id"], room["name"], room["percent"])
        assigned = conn.execute(
            "SELECT person_id, start_date, end_date FROM room_assignments"
            " WHERE room_id = :r AND start_date <= :last"
            "   AND COALESCE(end_date, :forever) >= :first",
            {"r": room["id"], "first": first, "last": last, "forever": _FOREVER},
        ).fetchall()
        for a in assigned:
            s = max(a["start_date"], first)
            e = min(a["end_date"] or _FOREVER, last)
            rm.days[a["person_id"]] = rm.days.get(a["person_id"], 0) + _nights(s, e)
        rm.vacant = _gaps([(a["start_date"], a["end_date"]) for a in assigned], first, last)
        rooms.append(rm)
    return MonthOccupancy(month, _nights(first, last), rooms)
