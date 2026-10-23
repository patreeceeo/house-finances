"""Shared test helpers (import as ``from helpers import ...``).

Who is in the house comes from room assignments, so tests that need people
"in the house" for some months give each their own room for that span.
"""

from housefin import db, rooms

ALWAYS = "2000-01"


def move_in(conn, person_id, start=ALWAYS, end=None):
    """Assign a person to their own room ("<Name>'s room", 1%) from the 1st
    of month ``start`` through the last day of month ``end`` (None =
    ongoing). Returns the assignment id."""
    name = conn.execute("SELECT name FROM people WHERE id = ?", (person_id,)).fetchone()[0]
    room = conn.execute("SELECT id FROM rooms WHERE name = ?", (f"{name}'s room",)).fetchone()
    room_id = room[0] if room else rooms.add_room(conn, f"{name}'s room", 1)
    return rooms.add_assignment(
        conn, room_id, person_id, f"{start}-01", db.month_end(end) if end else None
    )


def add_resident(conn, name, start=ALWAYS, end=None):
    """Add a person who is in the house from month ``start`` through month
    ``end`` (inclusive; None = still there). Returns their id."""
    pid = db.add_person(conn, name)
    move_in(conn, pid, start, end)
    return pid
