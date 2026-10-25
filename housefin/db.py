"""SQLite storage: connection, schema migrations, seed data and small lookups.

All money is integer cents. Months are 'YYYY-MM' text, dates 'YYYY-MM-DD'.
Booleans are stored as 0/1 integers.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from housefin import colors, dates

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "finances.db"

HOUSE_NAME = "House"
TRANSFER = "Transfer"
DEFAULT_PURPOSES = (
    "Rent",
    "PG&E",
    "Water",
    "Recology",
    "Internet",
    "Costco",
    "Restaurant Depot",
    "Other",
)

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS people (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    is_house      INTEGER NOT NULL DEFAULT 0 CHECK (is_house IN (0, 1)),
    active_from   TEXT,   -- 'YYYY-MM'; NULL = always
    active_until  TEXT    -- 'YYYY-MM', inclusive; NULL while current
);
-- At most one House row (seeding guarantees at least one).
CREATE UNIQUE INDEX IF NOT EXISTS people_one_house ON people (is_house) WHERE is_house = 1;

CREATE TABLE IF NOT EXISTS purposes (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL UNIQUE,
    excluded  INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1))
);

CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY,
    fingerprint      TEXT    NOT NULL,
    occurrence       INTEGER NOT NULL DEFAULT 1 CHECK (occurrence >= 1),
    date             TEXT    NOT NULL,  -- 'YYYY-MM-DD'
    time             TEXT    NOT NULL,  -- 'HH:MM:SS'
    amount_cents     INTEGER NOT NULL,  -- deposits > 0, withdrawals < 0
    type             TEXT    NOT NULL,  -- as in the bank export
    description      TEXT    NOT NULL,
    source_file      TEXT,
    imported_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    person_id        INTEGER REFERENCES people (id),
    purpose_id       INTEGER REFERENCES purposes (id),
    applies_month    TEXT,  -- 'YYYY-MM'
    category_source  TEXT CHECK (category_source IN ('rule', 'manual')),
    UNIQUE (fingerprint, occurrence)
);
CREATE INDEX IF NOT EXISTS transactions_date ON transactions (date);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY,
    match_type  TEXT    NOT NULL DEFAULT 'exact' CHECK (match_type IN ('exact', 'contains')),
    pattern     TEXT    NOT NULL,
    applies_to  TEXT    NOT NULL CHECK (applies_to IN ('deposit', 'withdrawal')),
    person_id   INTEGER REFERENCES people (id),
    purpose_id  INTEGER REFERENCES purposes (id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS splits (
    transaction_id  INTEGER NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
    person_id       INTEGER NOT NULL REFERENCES people (id),
    weight          REAL    NOT NULL CHECK (weight > 0),
    share_cents     INTEGER NOT NULL,
    PRIMARY KEY (transaction_id, person_id)
);

CREATE TABLE IF NOT EXISTS purpose_splits (
    purpose_id  INTEGER NOT NULL REFERENCES purposes (id) ON DELETE CASCADE,
    person_id   INTEGER NOT NULL REFERENCES people (id),
    weight      REAL    NOT NULL CHECK (weight > 0),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (purpose_id, person_id)
);

CREATE TABLE IF NOT EXISTS imports (
    id              INTEGER PRIMARY KEY,
    file_name       TEXT    NOT NULL,
    file_hash       TEXT    NOT NULL,
    rows_read       INTEGER NOT NULL DEFAULT 0,
    rows_new        INTEGER NOT NULL DEFAULT 0,
    rows_duplicate  INTEGER NOT NULL DEFAULT 0,
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS imports_file_hash ON imports (file_hash);
"""

# Ordered migrations; MIGRATIONS[i] takes the DB from user_version i to i + 1.
# Append new entries (never edit old ones) to change the schema.
# v2: default rule tagging 'Interest Paid' deposits to the House. Seeded by a
# migration so it runs once per DB and a user delete
# sticks. The House is created here if missing (fresh DB: migrations run
# before _ensure_house); the name matches HOUSE_NAME.
_SEED_INTEREST_RULE_V2 = """
INSERT INTO people (name, is_house)
    SELECT 'House', 1 WHERE NOT EXISTS (SELECT 1 FROM people WHERE is_house = 1);
INSERT INTO rules (match_type, pattern, applies_to, person_id)
    SELECT 'exact', 'Interest Paid', 'deposit', id FROM people WHERE is_house = 1;
"""

# v3: where a transaction's split came from. 'default' = written from the
# purpose's default split (housefin.splits.apply_default_split), 'manual' =
# set by hand in a split editor, NULL = no split. Backfill: rows tagged by a
# rule only ever got default splits, so 'default'; any other row with split
# rows gets 'manual' (before v3 every existing split was treated as manual,
# so this keeps those splits protected from being recomputed).
_SPLIT_SOURCE_V3 = """
ALTER TABLE transactions ADD COLUMN split_source TEXT
    CHECK (split_source IN ('default', 'manual'));
UPDATE transactions
   SET split_source = CASE WHEN category_source = 'rule' THEN 'default' ELSE 'manual' END
 WHERE id IN (SELECT transaction_id FROM splits);
"""

# v4: people a default split left out because they were not active in the
# transaction's month (housefin.splits.default_split_detail), with the weight
# they had in the purpose default. While a transaction has rows here its
# split is pending review: the others' shares were scaled up to cover the
# whole amount, and a person must confirm or change that. Backfill: default
# splits missing an inactive member of their purpose's current default.
_SPLIT_LEFT_OUT_V4 = """
CREATE TABLE IF NOT EXISTS split_left_out (
    transaction_id  INTEGER NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
    person_id       INTEGER NOT NULL REFERENCES people (id),
    weight          REAL    NOT NULL CHECK (weight > 0),
    PRIMARY KEY (transaction_id, person_id)
);
INSERT INTO split_left_out (transaction_id, person_id, weight)
SELECT t.id, ps.person_id, ps.weight
  FROM transactions t
  JOIN purpose_splits ps ON ps.purpose_id = t.purpose_id
  JOIN people p ON p.id = ps.person_id
 WHERE t.split_source = 'default'
   AND p.is_house = 0
   AND (p.active_from > COALESCE(t.applies_month, substr(t.date, 1, 7))
        OR p.active_until < COALESCE(t.applies_month, substr(t.date, 1, 7)))
   AND NOT EXISTS (SELECT 1 FROM splits s
                    WHERE s.transaction_id = t.id AND s.person_id = ps.person_id);
"""

# v5: rooms and who slept in them (data layer in housefin.rooms). Each room carries a fixed percent of the rent (all rooms
# total exactly 100, enforced by the app). Assignment dates are 'YYYY-MM-DD'
# nights, both ends inclusive; NULL end_date = still there. seeded = 1 marks
# rows a migration created that a person has not checked yet. people.color is
# '#rrggbb'; purposes.split_by_rooms marks the purpose (Rent) whose split
# comes from room assignments. Seeding rooms and dropping active_from /
# active_until come in a later migration.
_ROOMS_V5 = """
CREATE TABLE IF NOT EXISTS rooms (
    id       INTEGER PRIMARY KEY,
    name     TEXT    NOT NULL UNIQUE,
    percent  REAL    NOT NULL CHECK (percent > 0)
);
CREATE TABLE IF NOT EXISTS room_assignments (
    id          INTEGER PRIMARY KEY,
    room_id     INTEGER NOT NULL REFERENCES rooms (id),
    person_id   INTEGER NOT NULL REFERENCES people (id),
    start_date  TEXT    NOT NULL,
    end_date    TEXT,
    seeded      INTEGER NOT NULL DEFAULT 0,
    CHECK (end_date IS NULL OR end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS room_assignments_room ON room_assignments (room_id, start_date);
CREATE INDEX IF NOT EXISTS room_assignments_person ON room_assignments (person_id, start_date);
ALTER TABLE people ADD COLUMN color TEXT;
ALTER TABLE purposes ADD COLUMN split_by_rooms INTEGER NOT NULL DEFAULT 0;
"""

# v6: seed rooms from the Rent default split and the old active months, then
# drop people.active_from / active_until. Data-dependent, so it is a Python step (see _seed_rooms_v6).
# old_active_months keeps, for each non-House person who got no room, the
# months they had (NULL = unbounded) so the Rooms screen can list them under
# "Not in a room" (housefin.people.not_in_a_room) until they are placed.
_OLD_ACTIVE_MONTHS_V6 = """
CREATE TABLE IF NOT EXISTS old_active_months (
    person_id     INTEGER PRIMARY KEY REFERENCES people (id) ON DELETE CASCADE,
    active_from   TEXT,  -- 'YYYY-MM'; NULL = no start set
    active_until  TEXT   -- 'YYYY-MM', inclusive; NULL = no end set
);
"""

# v7: kept match groups on Balances (housefin.matching). A group ties some of
# one person's paid transactions to some of their share transactions; side
# says which. The kept_* totals are recorded only to flag later changes. An
# item's person_id is the group's, so the primary key lets each (person,
# transaction, side) sit in at most one kept group.
_MATCH_GROUPS_V7 = """
CREATE TABLE IF NOT EXISTS match_groups (
    id                INTEGER PRIMARY KEY,
    person_id         INTEGER NOT NULL REFERENCES people (id),
    note              TEXT,
    kept_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    kept_paid_cents   INTEGER NOT NULL,
    kept_share_cents  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS match_group_items (
    group_id        INTEGER NOT NULL REFERENCES match_groups (id) ON DELETE CASCADE,
    person_id       INTEGER NOT NULL REFERENCES people (id),
    transaction_id  INTEGER NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
    side            TEXT    NOT NULL CHECK (side IN ('paid', 'share')),
    PRIMARY KEY (person_id, transaction_id, side)
);
CREATE INDEX IF NOT EXISTS match_group_items_group ON match_group_items (group_id);
"""

RENT = "Rent"


def _seed_rooms_v6(conn: sqlite3.Connection) -> None:
    """Rooms and seeded assignments from the Rent default split and old active
    months; colors for everyone; then drop the active-month columns.

    1. One room per non-House person in the Rent default split, named
       "<Person>'s room", with their Rent weight copied exactly as the
       percent (never rescaled; the Rooms screen flags a total that is not
       exactly 100). Skipped if rooms already exist.
    2. Each is assigned to their room with seeded = 1: first night the 1st of
       their old Active from month, or of the earliest month with transactions
       (today's month if there are none); last night the last day of their
       Active until month, or ongoing.
    3. Everyone else (non-House, not given a room) is recorded with their old
       months in old_active_months; until placed they are not in the house.
    4. split_by_rooms set on Rent and Rent's default split removed.
    5. A color for everyone without one; drop active_from and active_until.
    No stored transaction split is changed.
    """
    conn.execute(_OLD_ACTIVE_MONTHS_V6)
    first_month = conn.execute(
        "SELECT MIN(m) FROM (SELECT substr(date, 1, 7) AS m FROM transactions"
        " UNION ALL SELECT applies_month FROM transactions WHERE applies_month IS NOT NULL)"
    ).fetchone()[0] or _dt.date.today().strftime("%Y-%m")
    people = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, name, is_house, active_from, active_until, color FROM people ORDER BY id"
        )
    }
    rent = conn.execute("SELECT id FROM purposes WHERE name = ?", (RENT,)).fetchone()
    rent_split = []
    if rent is not None:
        rent_split = conn.execute(
            "SELECT person_id, weight FROM purpose_splits WHERE purpose_id = ?"
            " ORDER BY person_id",
            (rent["id"],),
        ).fetchall()

    placed: set[int] = set()
    no_rooms_yet = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
    if rent_split and no_rooms_yet:
        for pid, weight in rent_split:
            person = people[pid]
            room_id = conn.execute(
                "INSERT INTO rooms (name, percent) VALUES (?, ?)",
                (f"{person['name']}'s room", weight),
            ).lastrowid
            if person["is_house"]:
                continue  # the House is never assigned; its room starts vacant
            start_month = person["active_from"] or first_month
            until = person["active_until"]
            if until is not None and start_month > until:
                start_month = until  # no transactions before they left
            conn.execute(
                "INSERT INTO room_assignments (room_id, person_id, start_date, end_date, seeded)"
                " VALUES (?, ?, ?, ?, 1)",
                (room_id, pid, f"{start_month}-01", dates.month_end(until) if until else None),
            )
            placed.add(pid)

    has_room = {
        r[0] for r in conn.execute("SELECT DISTINCT person_id FROM room_assignments")
    } | placed
    conn.executemany(
        "INSERT OR IGNORE INTO old_active_months (person_id, active_from, active_until)"
        " VALUES (?, ?, ?)",
        [
            (pid, p["active_from"], p["active_until"])
            for pid, p in people.items()
            if not p["is_house"] and pid not in has_room
        ],
    )

    if rent is not None:
        conn.execute("UPDATE purposes SET split_by_rooms = 0")
        conn.execute("UPDATE purposes SET split_by_rooms = 1 WHERE id = ?", (rent["id"],))
        conn.execute("DELETE FROM purpose_splits WHERE purpose_id = ?", (rent["id"],))

    existing = [p["color"] for p in people.values() if p["color"]]
    missing = [pid for pid, p in people.items() if not p["color"]]
    fresh = colors.generate_colors(len(missing)) if not existing else []
    for i, pid in enumerate(missing):
        color = fresh[i] if fresh else colors.new_color(existing)
        existing.append(color)
        conn.execute("UPDATE people SET color = ? WHERE id = ?", (color, pid))

    conn.execute("ALTER TABLE people DROP COLUMN active_from")
    conn.execute("ALTER TABLE people DROP COLUMN active_until")


# A migration is SQL text (run as a script) or a function taking the
# connection; either way it runs inside one transaction with its version bump.
MIGRATIONS: list[str | Callable[[sqlite3.Connection], None]] = [
    _SCHEMA_V1, _SEED_INTEREST_RULE_V2, _SPLIT_SOURCE_V3, _SPLIT_LEFT_OUT_V4, _ROOMS_V5,
    _seed_rooms_v6, _MATCH_GROUPS_V7,
]


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the database, migrate it and seed defaults.

    Safe to call repeatedly on the same file. Rows come back as sqlite3.Row.
    Pass ":memory:" for a throwaway database.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Group writes so they all land or none do.

    Outermost use: BEGIN, then COMMIT on success or ROLLBACK on error. Inside
    a transaction that is already open (``conn.in_transaction``, e.g. a caller
    grouping several steps), it joins it through a SAVEPOINT instead: nothing
    is committed here, and on error only this block's writes are undone before
    the exception propagates. Use this instead of ``with conn:``, which commits
    whatever transaction the caller has open.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return
    conn.execute("SAVEPOINT housefin")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:  # SQLite may already have rolled everything back
            conn.execute("ROLLBACK TO housefin")
            conn.execute("RELEASE housefin")
        raise
    conn.execute("RELEASE housefin")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, *, upto: int | None = None) -> None:
    """Apply pending migrations (all, or through version ``upto``), then make
    sure the seed rows exist."""
    version = schema_version(conn)
    last = len(MIGRATIONS) if upto is None else upto
    for target, step in enumerate(MIGRATIONS[version:last], start=version + 1):
        # Each migration and its version bump commit together.
        if isinstance(step, str):
            conn.executescript(f"BEGIN;\n{step}\nPRAGMA user_version = {target};\nCOMMIT;")
            continue
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN")
        try:
            step(conn)
            conn.execute(f"PRAGMA user_version = {target}")
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    if version == 0:
        _seed_purposes(conn)
    _ensure_house(conn)


def _seed_purposes(conn: sqlite3.Connection) -> None:
    """Starter purposes plus built-in Transfer; only on a brand-new DB so later
    user edits (renames, deletions) are not undone on the next connect."""
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO purposes (name, excluded) VALUES (?, ?)",
            [(name, 0) for name in DEFAULT_PURPOSES] + [(TRANSFER, 1)],
        )
        if schema_version(conn) >= 5:  # Rent is split by rooms (v5 column)
            conn.execute("UPDATE purposes SET split_by_rooms = 1 WHERE name = ?", (RENT,))


def _ensure_house(conn: sqlite3.Connection) -> None:
    """The House cannot be deleted, so recreate it if it is ever missing."""
    with transaction(conn):
        if conn.execute("SELECT 1 FROM people WHERE is_house = 1").fetchone() is None:
            conn.execute("INSERT INTO people (name, is_house) VALUES (?, 1)", (HOUSE_NAME,))


# --- lookups -----------------------------------------------------------------


# A person is in the house for a month when assigned to any room on any night
# of it. Dates are 'YYYY-MM-DD' text, so they compare in date order; NULL
# end_date = still there.
_IN_MONTH_SQL = (
    "EXISTS (SELECT 1 FROM room_assignments ra WHERE ra.person_id = people.id"
    " AND ra.start_date <= :last AND (ra.end_date IS NULL OR ra.end_date >= :first))"
)


def _month_params(month: str) -> dict[str, str]:
    return {"first": f"{month}-01", "last": dates.month_end(month)}


def in_house_ids(conn: sqlite3.Connection, month: str) -> set[int]:
    """Ids of the housemates in a room on any night of 'YYYY-MM' (no House)."""
    rows = conn.execute(
        f"SELECT id FROM people WHERE is_house = 0 AND {_IN_MONTH_SQL}", _month_params(month)
    )
    return {r[0] for r in rows}


def in_house(conn: sqlite3.Connection, person: sqlite3.Row | int, month: str) -> bool:
    """Whether a person (row or id) is in the house in 'YYYY-MM': assigned to
    any room on any night of it. The House always is."""
    pid = person if isinstance(person, int) else person["id"]
    row = conn.execute(
        f"SELECT is_house = 1 OR {_IN_MONTH_SQL} FROM people WHERE id = :pid",
        {"pid": pid, **_month_params(month)},
    ).fetchone()
    return bool(row and row[0])


def list_people(conn: sqlite3.Connection, *, include_house: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM people"
    if not include_house:
        sql += " WHERE is_house = 0"
    return conn.execute(sql + " ORDER BY is_house DESC, name").fetchall()


def people_in_house(
    conn: sqlite3.Connection, month: str, *, include_house: bool = False
) -> list[sqlite3.Row]:
    """People in the house in 'YYYY-MM' (in a room on any night of it),
    ordered by name. House excluded by default; when included it is always
    present (listed first)."""
    sql = f"SELECT * FROM people WHERE ({_IN_MONTH_SQL}"
    sql += " OR is_house = 1)" if include_house else " AND is_house = 0)"
    return conn.execute(sql + " ORDER BY is_house DESC, name", _month_params(month)).fetchall()


def get_person_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()


def add_person(conn: sqlite3.Connection, name: str, *, color: str | None = None) -> int:
    """Insert a person (no validation; see housefin.people.add_person). They
    get a color far from everyone else's unless one is given. They are in no
    room, so not in the house, until assigned (housefin.rooms)."""
    with transaction(conn):
        if color is None:
            existing = [
                r[0] for r in conn.execute("SELECT color FROM people WHERE color IS NOT NULL")
            ]
            color = colors.new_color(existing)
        cur = conn.execute("INSERT INTO people (name, color) VALUES (?, ?)", (name, color))
    return cur.lastrowid


def list_purposes(conn: sqlite3.Connection, *, include_excluded: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM purposes"
    if not include_excluded:
        sql += " WHERE excluded = 0"
    return conn.execute(sql + " ORDER BY excluded, id").fetchall()


def get_purpose(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Purpose row by exact name, or None."""
    return conn.execute("SELECT * FROM purposes WHERE name = ?", (name,)).fetchone()


def add_purpose(conn: sqlite3.Connection, name: str, *, excluded: bool = False) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO purposes (name, excluded) VALUES (?, ?)", (name, int(excluded))
        )
    return cur.lastrowid
