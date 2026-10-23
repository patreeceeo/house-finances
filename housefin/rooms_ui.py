"""Helpers for the Rooms page (pages/7_Rooms.py) that need no Streamlit.

The page edits rooms and assignments in ``st.data_editor`` tables and saves
each table with one button. The editor reports what changed in its
session-state format::

    {"edited_rows": {row_position: {column: value}},
     "added_rows": [{column: value}],
     "deleted_rows": [row_position]}

:func:`save_room_table` and :func:`save_assignment_table` apply such a change
set in one transaction: either every change lands or none does, and the first
refusal from housefin.rooms is raised unchanged (its message is shown to the
user as is). Nothing is rounded, rescaled or guessed.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, localcontext
from fractions import Fraction

from housefin import colors, db, rooms
from housefin.rooms import RoomError

# Column names shared with the page.
ROOM = "Room"
PERCENT = "Percent of rent"
NOW = "Now"
PERSON = "Person"
FIRST = "First night"
LAST = "Last night"
STATUS = "Status"

SEEDED = "seeded, check"
CURRENT = "in the room"
ENDED = "ended"
UPCOMING = "upcoming"
VACANT = "vacant"


def today() -> str:
    return _dt.date.today().isoformat()


# --- editor values ----------------------------------------------------------------


def is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple)) and not value:
        return True
    try:
        import pandas as pd

        return value is pd.NaT
    except ImportError:  # pragma: no cover
        return False


def date_text(value, *, label: str) -> str | None:
    """An editor date cell (date, datetime, Timestamp or ISO text) as
    'YYYY-MM-DD', or None when blank. Bad dates raise RoomError."""
    if is_blank(value):
        return None
    if isinstance(value, _dt.datetime):
        value = value.date().isoformat()
    elif isinstance(value, _dt.date):
        value = value.isoformat()
    else:
        value = str(value).strip()
        if len(value) > 10 and value[10] in "T ":
            value = value[:10]
    return rooms.check_date(value, label=label)


def _as_date(value: str | None) -> _dt.date | None:
    return None if value is None else _dt.date.fromisoformat(value)


def exact_total(percents: Sequence) -> str:
    """The sum of entered percents written out in full ('100', '99.999')."""
    total = sum((Fraction(str(p)) for p in percents if not is_blank(p)), Fraction(0))
    with localcontext() as ctx:
        ctx.prec = 50
        d = Decimal(total.numerator) / Decimal(total.denominator)
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _changes(changes: Mapping | None) -> tuple[dict[int, dict], list[dict], set[int]]:
    changes = changes or {}
    edited = {int(k): dict(v) for k, v in (changes.get("edited_rows") or {}).items()}
    added = [dict(r) for r in (changes.get("added_rows") or [])]
    deleted = {int(i) for i in (changes.get("deleted_rows") or [])}
    return edited, added, deleted


def has_changes(changes: Mapping | None) -> bool:
    edited, added, deleted = _changes(changes)
    return bool(any(edited.values()) or added or deleted)


def _apply_all(conn: sqlite3.Connection, first: list[Callable], rest: list[Callable]) -> None:
    """Run ``first`` in order, then ``rest`` in whatever order lets them all
    pass (so moving one date before another's doesn't clash midway), all in
    one transaction. If some can never pass, everything is undone and the
    first remaining refusal is raised."""
    with db.transaction(conn):
        for op in first:
            op()
        pending = list(rest)
        while pending:
            failed: list[tuple[Callable, RoomError]] = []
            for op in pending:
                try:
                    with db.transaction(conn):
                        op()
                except RoomError as exc:
                    failed.append((op, exc))
            if len(failed) == len(pending):
                raise failed[0][1]
            pending = [op for op, _ in failed]


# --- room percents table --------------------------------------------------------------


def room_rows(conn: sqlite3.Connection, date: str | None = None) -> list[dict]:
    """Rows for the room percents editor: id, Room, Percent of rent, Now
    (list with the current occupant's name, empty if vacant)."""
    people = {p["id"]: p for p in db.list_people(conn)}
    now = rooms.occupants(conn, date or today())
    out = []
    for r in rooms.list_rooms(conn):
        pid = now.get(r["id"])
        out.append(
            {
                "id": r["id"],
                ROOM: r["name"],
                PERCENT: r["percent"],
                NOW: [people[pid]["name"]] if pid in people else [],
            }
        )
    return out


def room_percents_after(rows: Sequence[Mapping], changes: Mapping | None) -> list:
    """Every room's percent as the editor shows it now (deleted rows left
    out, new rows included, blanks kept as None)."""
    edited, added, deleted = _changes(changes)
    out = []
    for i, row in enumerate(rows):
        if i in deleted:
            continue
        out.append(edited.get(i, {}).get(PERCENT, row[PERCENT]))
    out.extend(r.get(PERCENT) for r in added if not _blank_room_row(r))
    return out


def _blank_room_row(row: Mapping) -> bool:
    return is_blank(row.get(ROOM)) and is_blank(row.get(PERCENT))


def save_room_table(
    conn: sqlite3.Connection, rows: Sequence[Mapping], changes: Mapping | None
) -> None:
    """Apply the room editor's changes (renames, new rooms, deleted rooms and
    percents) together. Percents are saved for all rooms at once and must
    total exactly 100% (rooms.set_room_percents)."""
    edited, added, deleted = _changes(changes)
    final: dict[int, object] = {}
    renames: list[Callable] = []
    deletes: list[Callable] = []
    adds: list[tuple[str, object]] = []
    percents_changed = False
    for i, row in enumerate(rows):
        rid = row["id"]
        if i in deleted:
            deletes.append(lambda rid=rid: rooms.delete_room(conn, rid))
            percents_changed = True
            continue
        edit = edited.get(i, {})
        name = edit.get(ROOM, row[ROOM])
        if is_blank(name):
            raise RoomError(f"Room name cannot be empty (was {row[ROOM]}).")
        if name != row[ROOM]:
            renames.append(lambda rid=rid, name=name: rooms.rename_room(conn, rid, name))
        pct = edit.get(PERCENT, row[PERCENT])
        if is_blank(pct):
            raise RoomError(f"{name}: give a percent of the rent.")
        if pct != row[PERCENT]:
            percents_changed = True
        final[rid] = pct
    for r in added:
        if _blank_room_row(r):
            continue
        name = r.get(ROOM)
        if is_blank(name):
            raise RoomError("A new room needs a name.")
        pct = r.get(PERCENT)
        if is_blank(pct):
            raise RoomError(f"{name}: give a percent of the rent.")
        adds.append((name, pct))
        percents_changed = True

    with db.transaction(conn):
        _apply_all(conn, deletes, renames)
        for name, pct in adds:
            final[rooms.add_room(conn, name, pct)] = pct
        if percents_changed and final:
            rooms.set_room_percents(conn, final)


# --- assignments per room ---------------------------------------------------------------


def assignment_status(row: Mapping, date: str | None = None) -> str:
    """'seeded, check' until a person edits or confirms a seeded row, else
    'in the room', 'ended' or 'upcoming' as of ``date`` (default today)."""
    if row["seeded"]:
        return SEEDED
    date = date or today()
    if row["start_date"] > date:
        return UPCOMING
    if row["end_date"] is not None and row["end_date"] < date:
        return ENDED
    return CURRENT


def assignment_rows(conn: sqlite3.Connection, room_id: int, date: str | None = None) -> list[dict]:
    """Rows for one room's assignment editor, newest first by first night:
    id, seeded, Person (list holding the name, for a colored chip), First
    night and Last night (dates; None = ongoing), Status."""
    out = []
    for a in rooms.list_assignments(conn, room_id):
        out.append(
            {
                "id": a["id"],
                "seeded": bool(a["seeded"]),
                "person_id": a["person_id"],
                PERSON: [a["person_name"]],
                FIRST: _as_date(a["start_date"]),
                LAST: _as_date(a["end_date"]),
                STATUS: assignment_status(a, date),
            }
        )
    return out


def vacant_rows(conn: sqlite3.Connection, room_id: int) -> list[dict]:
    """The room's computed vacant spans, newest first: First night, Last
    night (None = vacant from then on), Nights (None when open-ended)."""
    out = []
    for start, end in reversed(rooms.vacancies(conn, room_id)):
        nights = None if end is None else (_as_date(end) - _as_date(start)).days + 1
        out.append({FIRST: _as_date(start), LAST: _as_date(end), "Nights": nights})
    return out


def _person_id(value, people_by_name: Mapping[str, int]) -> int:
    if isinstance(value, str):
        value = [value]
    names = [] if is_blank(value) else [str(v) for v in value]
    if not names:
        raise RoomError("Pick a person for every row.")
    if len(names) > 1:
        raise RoomError(f"Pick one person per row, not {len(names)} ({', '.join(names)}).")
    if names[0] not in people_by_name:
        raise RoomError(f"No person named {names[0]!r}.")
    return people_by_name[names[0]]


def save_assignment_table(
    conn: sqlite3.Connection,
    room_id: int,
    rows: Sequence[Mapping],
    changes: Mapping | None,
    people_by_name: Mapping[str, int],
) -> None:
    """Apply one room's assignment editor changes (edited, added and deleted
    rows) together; any refusal undoes them all and is raised unchanged.
    Editing a seeded row clears its seeded mark (rooms.update_assignment)."""
    edited, added, deleted = _changes(changes)
    deletes: list[Callable] = []
    rest: list[Callable] = []
    for i, row in enumerate(rows):
        aid = row["id"]
        if i in deleted:
            deletes.append(lambda aid=aid: rooms.delete_assignment(conn, aid))
            continue
        edit = edited.get(i)
        if not edit:
            continue
        person = _person_id(edit.get(PERSON, row[PERSON]), people_by_name)
        start = date_text(edit.get(FIRST, row[FIRST]), label=FIRST)
        end = date_text(edit.get(LAST, row[LAST]), label=LAST)
        before = (
            row["person_id"],
            date_text(row[FIRST], label=FIRST),
            date_text(row[LAST], label=LAST),
        )
        if (person, start, end) == before:
            continue
        if start is None:
            raise RoomError("Pick the first night in the room.")
        rest.append(
            lambda aid=aid, p=person, s=start, e=end: rooms.update_assignment(
                conn, aid, room_id, p, s, e
            )
        )
    for r in added:
        if all(is_blank(r.get(c)) for c in (PERSON, FIRST, LAST)):
            continue  # an empty row the editor added; nothing typed
        person = _person_id(r.get(PERSON), people_by_name)
        start = date_text(r.get(FIRST), label=FIRST)
        end = date_text(r.get(LAST), label=LAST)
        if start is None:
            raise RoomError("Pick the first night in the room.")
        rest.append(
            lambda p=person, s=start, e=end: rooms.add_assignment(conn, room_id, p, s, e)
        )
    _apply_all(conn, deletes, rest)


def confirm_seeded(conn: sqlite3.Connection, room_id: int) -> int:
    """Mark every seeded assignment in a room as checked; returns how many."""
    ids = [a["id"] for a in rooms.list_assignments(conn, room_id) if a["seeded"]]
    with db.transaction(conn):
        for aid in ids:
            rooms.confirm_assignment(conn, aid)
    return len(ids)


# --- timeline ---------------------------------------------------------------------------


def timeline_frame(conn: sqlite3.Connection):
    """(names, person_ids) DataFrames for the timeline: index 'From' (one row
    per date any room changes hands, newest first), one column per room;
    names hold the person's name or 'vacant', person_ids the id or None."""
    import pandas as pd

    room_list = rooms.list_rooms(conn)
    people = {p["id"]: p for p in db.list_people(conn)}
    columns = [r["name"] for r in room_list]
    names, ids, index = [], [], []
    for entry in rooms.timeline(conn):
        index.append(entry["date"])
        row_ids = [entry["rooms"].get(r["id"]) for r in room_list]
        ids.append(row_ids)
        names.append([people[p]["name"] if p in people else VACANT for p in row_ids])
    idx = pd.Index(index, name="From")
    return (
        pd.DataFrame(names, index=idx, columns=columns, dtype=object),
        pd.DataFrame(ids, index=idx, columns=columns, dtype=object),
    )


VACANT_STYLE = "background-color: rgba(128, 128, 128, 0.12); color: #808080; font-style: italic"


def cell_style(color: str | None) -> str:
    """CSS for one timeline cell: the person's color with readable text, or
    the vacant look when there is no color (vacant, or a person with none)."""
    if not color:
        return VACANT_STYLE
    return f"background-color: {color}; color: {colors.text_color_for(color)}"


def timeline_styler(conn: sqlite3.Connection):
    """The timeline as a pandas Styler (cells in each person's color)."""
    names, ids = timeline_frame(conn)
    color_of = {p["id"]: p["color"] for p in db.list_people(conn)}
    css = ids.map(
        lambda pid: VACANT_STYLE if pid is None else cell_style(color_of.get(pid)) if color_of.get(pid) else ""
    )
    return names.style.apply(lambda _: css, axis=None)
