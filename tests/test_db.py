import sqlite3

import pytest

from housefin import db

TABLES = {
    "transactions",
    "people",
    "purposes",
    "rules",
    "splits",
    "purpose_splits",
    "imports",
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
    db.add_person(c1, "Alice", "2026-01")
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


def test_active_people(conn):
    db.add_person(conn, "Alice", "2026-01")
    db.add_person(conn, "Bob", "2026-03", "2026-05")
    db.add_person(conn, "Cara")  # no bounds: always active

    def names(month, **kw):
        return [r["name"] for r in db.active_people(conn, month, **kw)]

    assert names("2025-12") == ["Cara"]
    assert names("2026-03") == ["Alice", "Bob", "Cara"]
    assert names("2026-05") == ["Alice", "Bob", "Cara"]  # active_until is inclusive
    assert names("2026-06") == ["Alice", "Cara"]
    assert names("2026-06", include_house=True) == ["House", "Alice", "Cara"]


def test_is_active_matches_query(conn):
    db.add_person(conn, "Bob", "2026-03", "2026-05")
    bob = db.get_person_by_name(conn, "Bob")
    assert not db.is_active(bob, "2026-02")
    assert db.is_active(bob, "2026-03")
    assert db.is_active(bob, "2026-05")
    assert not db.is_active(bob, "2026-06")


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
