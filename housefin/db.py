"""SQLite storage: connection, schema migrations, seed data and small lookups.

All money is integer cents. Months are 'YYYY-MM' text, dates 'YYYY-MM-DD'.
Booleans are stored as 0/1 integers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
# v2: default rule tagging 'Interest Paid' deposits to the House (spec open
# question 4). Seeded by a migration so it runs once per DB and a user delete
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

MIGRATIONS: list[str] = [_SCHEMA_V1, _SEED_INTEREST_RULE_V2, _SPLIT_SOURCE_V3]


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


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations, then make sure the seed rows exist."""
    version = schema_version(conn)
    for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
        # Each migration and its version bump commit together.
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")
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


def _ensure_house(conn: sqlite3.Connection) -> None:
    """The House cannot be deleted, so recreate it if it is ever missing."""
    with transaction(conn):
        if conn.execute("SELECT 1 FROM people WHERE is_house = 1").fetchone() is None:
            conn.execute("INSERT INTO people (name, is_house) VALUES (?, 1)", (HOUSE_NAME,))


# --- lookups -----------------------------------------------------------------


def get_house_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM people WHERE is_house = 1").fetchone()[0]


def is_active(person: sqlite3.Row, month: str) -> bool:
    """Whether a people row is active in 'YYYY-MM' (both bounds inclusive)."""
    start, end = person["active_from"], person["active_until"]
    return (start is None or start <= month) and (end is None or end >= month)


def list_people(conn: sqlite3.Connection, *, include_house: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM people"
    if not include_house:
        sql += " WHERE is_house = 0"
    return conn.execute(sql + " ORDER BY is_house DESC, name").fetchall()


def active_people(
    conn: sqlite3.Connection, month: str, *, include_house: bool = False
) -> list[sqlite3.Row]:
    """People active in 'YYYY-MM', ordered by name. House excluded by default.

    The House is never moved out, so when included it is always present.
    """
    sql = (
        "SELECT * FROM people"
        " WHERE (active_from IS NULL OR active_from <= :m)"
        " AND (active_until IS NULL OR active_until >= :m)"
    )
    if not include_house:
        sql += " AND is_house = 0"
    return conn.execute(sql + " ORDER BY is_house DESC, name", {"m": month}).fetchall()


def get_person_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()


def add_person(
    conn: sqlite3.Connection,
    name: str,
    active_from: str | None = None,
    active_until: str | None = None,
) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO people (name, active_from, active_until) VALUES (?, ?, ?)",
            (name, active_from, active_until),
        )
    return cur.lastrowid


def list_purposes(conn: sqlite3.Connection, *, include_excluded: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM purposes"
    if not include_excluded:
        sql += " WHERE excluded = 0"
    return conn.execute(sql + " ORDER BY excluded, id").fetchall()


def get_purpose(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Purpose row by exact name, or None."""
    return conn.execute("SELECT * FROM purposes WHERE name = ?", (name,)).fetchone()


def get_purpose_id(conn: sqlite3.Connection, name: str) -> int:
    """Id of an existing purpose; KeyError if there is none by that name."""
    row = get_purpose(conn, name)
    if row is None:
        raise KeyError(name)
    return row["id"]


def get_transfer_purpose_id(conn: sqlite3.Connection) -> int:
    return get_purpose_id(conn, TRANSFER)


def add_purpose(conn: sqlite3.Connection, name: str, *, excluded: bool = False) -> int:
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO purposes (name, excluded) VALUES (?, ?)", (name, int(excluded))
        )
    return cur.lastrowid
