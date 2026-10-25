"""Shared test helpers (import as ``from helpers import ...``).

Who is in the house comes from room assignments, so tests that need people
"in the house" for some months give each their own room for that span.
"""

from housefin import dates, db, rent, rooms

ALWAYS = "2000-01"


def move_in(conn, person_id, start=ALWAYS, end=None):
    """Assign a person to their own room ("<Name>'s room", 1%) from the 1st
    of month ``start`` through the last day of month ``end`` (None =
    ongoing). Returns the assignment id."""
    name = conn.execute("SELECT name FROM people WHERE id = ?", (person_id,)).fetchone()[0]
    room = conn.execute("SELECT id FROM rooms WHERE name = ?", (f"{name}'s room",)).fetchone()
    room_id = room[0] if room else rooms.add_room(conn, f"{name}'s room", 1)
    return rooms.add_assignment(
        conn, room_id, person_id, f"{start}-01", dates.month_end(end) if end else None
    )


def add_resident(conn, name, start=ALWAYS, end=None):
    """Add a person who is in the house from month ``start`` through month
    ``end`` (inclusive; None = still there). Returns their id."""
    pid = db.add_person(conn, name)
    move_in(conn, pid, start, end)
    return pid


def purpose_id(conn, name):
    return db.get_purpose(conn, name)["id"]


def house_id(conn):
    return conn.execute("SELECT id FROM people WHERE is_house = 1").fetchone()[0]


def room_split_of(conn, txn_id):
    """The room split a rent payment gets: its amount, month and rotation."""
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return rent.room_split(conn, dates.month_of(txn), -txn["amount_cents"], rotate=txn_id)


def needs_room_review(conn, txn_id):
    """A rent payment with no split, waiting for someone to cover a vacancy."""
    return conn.execute(
        f"SELECT 1 FROM transactions t WHERE t.id = ? AND {rent.NEEDS_REVIEW_SQL}", (txn_id,)
    ).fetchone() is not None
