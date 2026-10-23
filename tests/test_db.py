import re
import sqlite3

import pytest
from helpers import add_resident

from housefin import db, people, rooms

TABLES = {
    "transactions",
    "people",
    "purposes",
    "rules",
    "splits",
    "purpose_splits",
    "imports",
    "rooms",
    "room_assignments",
}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def _add_txn(conn, fingerprint="fp", occurrence=1, amount_cents=-100):
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, occurrence, date, time, amount_cents, type, description)"
        " VALUES (?, ?, '2026-10-01', '12:00:00', ?, 'Withdrawal', 'X')",
        (fingerprint, occurrence, amount_cents),
    )
    return cur.lastrowid


def test_fresh_db_creates_all_tables(conn):
    assert TABLES <= _tables(conn)
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_foreign_keys_enabled(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO splits VALUES (999, 999, 1, 0)")


def test_seeds_house(conn):
    houses = conn.execute("SELECT * FROM people WHERE is_house = 1").fetchall()
    assert len(houses) == 1
    assert houses[0]["name"] == "House"
    assert db.get_house_id(conn) == houses[0]["id"]


def test_seeds_purposes(conn):
    names = {r["name"]: r["excluded"] for r in db.list_purposes(conn)}
    assert set(names) == set(db.DEFAULT_PURPOSES) | {"Transfer"}
    assert names["Transfer"] == 1
    assert all(names[n] == 0 for n in db.DEFAULT_PURPOSES)
    assert "Transfer" not in {r["name"] for r in db.list_purposes(conn, include_excluded=False)}


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "finances.db"
    c1 = db.connect(path)
    db.add_person(c1, "Alice")
    c1.close()
    c2 = db.connect(path)
    c2.close()
    c3 = db.connect(path)
    assert c3.execute("SELECT COUNT(*) FROM people WHERE is_house = 1").fetchone()[0] == 1
    assert c3.execute("SELECT COUNT(*) FROM purposes").fetchone()[0] == len(db.DEFAULT_PURPOSES) + 1
    assert db.get_person_by_name(c3, "Alice") is not None
    assert db.schema_version(c3) == len(db.MIGRATIONS)
    c3.close()


def test_user_purpose_edits_survive_reconnect(tmp_path):
    path = tmp_path / "finances.db"
    c = db.connect(path)
    with c:
        c.execute("DELETE FROM purposes WHERE name = 'Costco'")
    c.close()
    c = db.connect(path)
    assert db.get_purpose(c, "Costco") is None
    c.close()


def test_only_one_house_allowed(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO people (name, is_house) VALUES ('Other House', 1)")


def test_fingerprint_occurrence_unique(conn):
    _add_txn(conn, "abc", 1)
    _add_txn(conn, "abc", 2)
    with pytest.raises(sqlite3.IntegrityError):
        _add_txn(conn, "abc", 1)


def test_check_constraints(conn):
    tid = _add_txn(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transactions SET category_source = 'guess' WHERE id = ?", (tid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rules (match_type, pattern, applies_to) VALUES ('regex', 'X', 'deposit')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rules (match_type, pattern, applies_to) VALUES ('exact', 'X', 'both')"
        )
    conn.execute("INSERT INTO rules (pattern, applies_to) VALUES ('X', 'withdrawal')")
    assert conn.execute("SELECT match_type FROM rules").fetchone()[0] == "exact"


def test_splits_cascade_with_transaction(conn):
    tid = _add_txn(conn)
    conn.execute(
        "INSERT INTO splits VALUES (?, ?, 1, -100)", (tid, db.get_house_id(conn))
    )
    conn.execute("DELETE FROM transactions WHERE id = ?", (tid,))
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0


def test_active_people_come_from_room_assignments(conn):
    alice = add_resident(conn, "Alice", "2026-01")
    bob = db.add_person(conn, "Bob")
    room = rooms.add_room(conn, "Back", 10)
    # Mid-month nights count for the whole month.
    rooms.add_assignment(conn, room, bob, "2026-03-31", "2026-05-01")
    db.add_person(conn, "Cara")  # no room: not in the house

    def names(month, **kw):
        return [r["name"] for r in db.active_people(conn, month, **kw)]

    assert names("2025-12") == []
    assert names("2026-02") == ["Alice"]
    assert names("2026-03") == ["Alice", "Bob"]
    assert names("2026-05") == ["Alice", "Bob"]  # the last night is inclusive
    assert names("2026-06") == ["Alice"]
    assert names("2026-06", include_house=True) == ["House", "Alice"]
    assert db.in_house_ids(conn, "2026-04") == {alice, bob}


def test_is_active_matches_query(conn):
    bob = db.add_person(conn, "Bob")
    rooms.add_assignment(conn, rooms.add_room(conn, "Back", 10), bob, "2026-03-15", "2026-05-02")
    row = db.get_person_by_name(conn, "Bob")
    assert not db.is_active(conn, row, "2026-02")
    assert db.is_active(conn, row, "2026-03")
    assert db.is_active(conn, bob, "2026-05")
    assert not db.is_active(conn, bob, "2026-06")
    assert db.is_active(conn, db.get_house_id(conn), "1999-01")  # the House always is


def test_add_person_gets_distinct_color(conn):
    a = db.get_person_by_name(conn, "House")
    ids = [db.add_person(conn, n) for n in ("Ann", "Bob", "Cy")]
    got = [conn.execute("SELECT color FROM people WHERE id = ?", (i,)).fetchone()[0] for i in ids]
    assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in got)
    assert len(set(got + [a["color"]])) == 4


def test_list_people(conn):
    db.add_person(conn, "Zed")
    db.add_person(conn, "Amy")
    assert [r["name"] for r in db.list_people(conn)] == ["House", "Amy", "Zed"]
    assert [r["name"] for r in db.list_people(conn, include_house=False)] == ["Amy", "Zed"]


def test_purpose_lookups(conn):
    rent = db.get_purpose(conn, "Rent")
    assert rent is not None and rent["excluded"] == 0
    assert db.get_purpose_id(conn, "Rent") == rent["id"]
    assert db.get_purpose(conn, "Nope") is None
    with pytest.raises(KeyError):
        db.get_purpose_id(conn, "Nope")
    assert db.get_transfer_purpose_id(conn) == db.get_purpose(conn, "Transfer")["id"]
    new_id = db.add_purpose(conn, "Gardening")
    assert db.get_purpose_id(conn, "Gardening") == new_id


# --- v5: rooms ------------------------------------------------------------------


def _columns(conn, table):
    return {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def test_v5_rooms_schema(conn):
    assert "color" in _columns(conn, "people")
    col = _columns(conn, "purposes")["split_by_rooms"]
    assert col["notnull"] == 1 and col["dflt_value"] == "0"
    assert set(_columns(conn, "room_assignments")) == {
        "id", "room_id", "person_id", "start_date", "end_date", "seeded",
    }
    # A fresh DB has no rooms; v6 dropped the old active months.
    assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
    assert not {"active_from", "active_until"} & set(_columns(conn, "people"))


def test_fresh_db_rent_splits_by_rooms(conn):
    assert [p["name"] for p in db.list_purposes(conn) if p["split_by_rooms"]] == ["Rent"]
    assert conn.execute("SELECT COUNT(*) FROM old_active_months").fetchone()[0] == 0


def test_v5_rooms_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO rooms (name, percent) VALUES ('A', 0)")
    room = conn.execute("INSERT INTO rooms (name, percent) VALUES ('A', 100)").lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO rooms (name, percent) VALUES ('A', 5)")
    pid = db.add_person(conn, "Ann")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO room_assignments (room_id, person_id, start_date, end_date)"
            " VALUES (?, ?, '2026-02-01', '2026-01-31')", (room, pid),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO room_assignments (room_id, person_id, start_date)"
            " VALUES (?, 999, '2026-02-01')", (room,),
        )
    conn.execute(
        "INSERT INTO room_assignments (room_id, person_id, start_date, end_date)"
        " VALUES (?, ?, '2026-02-01', '2026-02-01')", (room, pid),
    )
    assert conn.execute("SELECT seeded FROM room_assignments").fetchone()[0] == 0


def _old_db(path, version):
    """A DB migrated only through ``version`` (no later migrations)."""
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    db.migrate(raw, upto=version)
    assert db.schema_version(raw) == version
    return raw


def test_v5_migrates_existing_v4_db(tmp_path):
    path = tmp_path / "old.db"
    raw = _old_db(path, 4)
    raw.execute("INSERT INTO people (name, active_from) VALUES ('Ann', '2026-01')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    assert db.get_person_by_name(conn, "Ann")["color"]
    assert TABLES <= _tables(conn)
    conn.close()


# --- v6: rooms seeded from the Rent default split and old active months -----------


def _seed_v5(raw, people, rent_split, txn_dates=("2026-01-15",)):
    """people: {name: (active_from, active_until)}; rent_split: {name: weight}."""
    ids = {}
    for name, (start, end) in people.items():
        ids[name] = raw.execute(
            "INSERT INTO people (name, active_from, active_until) VALUES (?, ?, ?)",
            (name, start, end),
        ).lastrowid
    ids["House"] = raw.execute("SELECT id FROM people WHERE is_house = 1").fetchone()[0]
    rent = raw.execute("SELECT id FROM purposes WHERE name = 'Rent'").fetchone()[0]
    water = raw.execute("SELECT id FROM purposes WHERE name = 'Water'").fetchone()[0]
    for name, weight in rent_split.items():
        raw.execute(
            "INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, ?)",
            (rent, ids[name], weight),
        )
    raw.execute(
        "INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, 1)",
        (water, ids[next(iter(people))]),
    )
    for i, d in enumerate(txn_dates):
        tid = raw.execute(
            "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
            " purpose_id, split_source) VALUES (?, ?, '12:00:00', -1000, 'Withdrawal', 'R', ?,"
            " 'default')",
            (f"t{i}", d, rent),
        ).lastrowid
        raw.execute(
            "INSERT INTO splits (transaction_id, person_id, weight, share_cents)"
            " VALUES (?, ?, 1, -1000)",
            (tid, ids[next(iter(people))]),
        )
    raw.commit()
    return ids


def _splits(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM splits ORDER BY transaction_id")]


def test_v6_seeds_rooms_from_rent_split_and_active_months(tmp_path):
    path = tmp_path / "v5.db"
    raw = _old_db(path, 5)
    ids = _seed_v5(
        raw,
        {
            "Ann": (None, None),            # always: earliest transaction month
            "Bob": ("2025-11", "2026-02"),  # moved out
            "Moana": ("2026-07", None),     # arrived later
            "John": (None, "2026-02"),      # not in the Rent split
            "Zed": (None, None),            # not in the Rent split, no months
        },
        {"Ann": 50, "Bob": 43.375, "Moana": 6.625},
        txn_dates=("2026-03-02", "2026-01-31"),
    )
    raw.execute("UPDATE transactions SET applies_month = '2025-12' WHERE fingerprint = 't0'")
    raw.commit()
    before = _splits(raw)
    raw.close()

    conn = db.connect(path)
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    assert not {"active_from", "active_until"} & set(_columns(conn, "people"))

    got = {
        r["person_name"]: (r["room_name"], r["start_date"], r["end_date"], r["seeded"])
        for r in rooms.list_assignments(conn)
    }
    assert got == {
        # Earliest month with transactions counts applies_month too.
        "Ann": ("Ann's room", "2025-12-01", None, 1),
        "Bob": ("Bob's room", "2025-11-01", "2026-02-28", 1),
        "Moana": ("Moana's room", "2026-07-01", None, 1),
    }
    assert {r["name"]: r["percent"] for r in rooms.list_rooms(conn)} == {
        "Ann's room": 50, "Bob's room": 43.375, "Moana's room": 6.625,
    }
    assert rooms.percent_error(conn) is None

    rent = db.get_purpose(conn, "Rent")
    assert rent["split_by_rooms"] == 1
    assert [p["name"] for p in db.list_purposes(conn) if p["split_by_rooms"]] == ["Rent"]
    assert conn.execute(
        "SELECT COUNT(*) FROM purpose_splits WHERE purpose_id = ?", (rent["id"],)
    ).fetchone()[0] == 0
    # Other defaults and every stored split are untouched.
    assert conn.execute("SELECT COUNT(*) FROM purpose_splits").fetchone()[0] == 1
    assert _splits(conn) == before

    # Everyone (the House too) has a distinct color.
    colors_ = [p["color"] for p in db.list_people(conn)]
    assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in colors_)
    assert len(set(colors_)) == len(colors_)

    # People without a room keep their old months for the "Not in a room" list
    # and are not in the house.
    assert [(r["name"], r["active_from"], r["active_until"]) for r in people.not_in_a_room(conn)] == [
        ("John", None, "2026-02"), ("Zed", None, None),
    ]
    assert [p["name"] for p in db.active_people(conn, "2026-02")] == ["Ann", "Bob"]
    assert [p["name"] for p in db.active_people(conn, "2026-07")] == ["Ann", "Moana"]
    # Placing someone takes them off the list.
    rooms.add_assignment(conn, rooms.add_room(conn, "Attic", 1), ids["John"], "2026-01-01")
    assert [r["name"] for r in people.not_in_a_room(conn)] == ["Zed"]
    conn.close()


def test_v6_copies_weights_not_totalling_100_without_rescaling(tmp_path):
    path = tmp_path / "v5.db"
    raw = _old_db(path, 5)
    _seed_v5(raw, {"Ann": (None, None), "Bob": (None, None)}, {"Ann": 1, "Bob": 3})
    raw.close()
    conn = db.connect(path)
    assert {r["name"]: r["percent"] for r in rooms.list_rooms(conn)} == {
        "Ann's room": 1, "Bob's room": 3,
    }
    assert rooms.percent_error(conn) is not None
    conn.close()


def test_v6_house_in_rent_split_gets_a_vacant_room(tmp_path):
    path = tmp_path / "v5.db"
    raw = _old_db(path, 5)
    _seed_v5(raw, {"Ann": (None, None)}, {"Ann": 90, "House": 10})
    raw.close()
    conn = db.connect(path)
    assert {r["name"]: r["percent"] for r in rooms.list_rooms(conn)} == {
        "Ann's room": 90, "House's room": 10,
    }
    assert [r["person_name"] for r in rooms.list_assignments(conn)] == ["Ann"]
    assert people.not_in_a_room(conn) == []
    conn.close()


def test_v6_without_transactions_or_rent_split(tmp_path):
    path = tmp_path / "v5.db"
    raw = _old_db(path, 5)
    raw.execute("INSERT INTO people (name, active_from) VALUES ('Ann', '2026-03')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
    assert [(r["name"], r["active_from"]) for r in people.not_in_a_room(conn)] == [("Ann", "2026-03")]
    assert db.get_purpose(conn, "Rent")["split_by_rooms"] == 1
    conn.close()


def test_v6_moved_out_before_any_transaction(tmp_path):
    path = tmp_path / "v5.db"
    raw = _old_db(path, 5)
    _seed_v5(raw, {"Ann": (None, "2025-06")}, {"Ann": 100}, txn_dates=("2026-01-10",))
    raw.close()
    conn = db.connect(path)
    (a,) = rooms.list_assignments(conn)
    assert (a["start_date"], a["end_date"], a["seeded"]) == ("2025-06-01", "2025-06-30", 1)
    conn.close()


def test_v6_failure_rolls_back_with_version(tmp_path, monkeypatch):
    path = tmp_path / "v5.db"
    raw = _old_db(path, 5)
    _seed_v5(raw, {"Ann": (None, None)}, {"Ann": 100})
    raw.close()

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(db.colors, "generate_colors", boom)
    with pytest.raises(RuntimeError):
        db.connect(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 5
    assert check.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0
    assert "active_from" in {r[1] for r in check.execute("PRAGMA table_info(people)")}
    check.close()
