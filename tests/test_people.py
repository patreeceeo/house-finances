import datetime as dt
from pathlib import Path

import pytest

from housefin import db, people
from housefin.people import PersonError

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _tx(conn, n, amount=-100, person_id=None, date="2026-03-10"):
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description, person_id)"
        " VALUES (?, ?, '00:00:00', ?, 'X', 'synthetic', ?)",
        (f"fp{n}", date, amount, person_id),
    )
    conn.commit()
    return cur.lastrowid


def names(rows):
    return [r["name"] for r in rows]


# --- months ---------------------------------------------------------------------


def test_month_helpers():
    assert people.add_months("2026-12", 1) == "2027-01"
    assert people.add_months("2026-01", -1) == "2025-12"
    assert people.month_range("2026-11", "2027-02") == ["2026-11", "2026-12", "2027-01", "2027-02"]
    assert people.month_range("2026-05", "2026-04") == []
    assert people.current_month(dt.date(2026, 10, 20)) == "2026-10"
    assert people.check_month(None) is None
    for bad in ("2026-13", "2026-1", "Oct 2026", ""):
        with pytest.raises(PersonError):
            people.check_month(bad)


def test_month_choices(conn):
    _tx(conn, 1, date="2020-06-01")
    ms = people.month_choices(conn, today=dt.date(2026, 10, 20), extra=("2019-01", None))
    assert ms[0] == "2027-10" and ms == sorted(ms, reverse=True)
    assert "2020-06" in ms and "2019-01" in ms and "2026-10" in ms


# --- add / rename -----------------------------------------------------------------


def test_add_person_not_in_remembered_splits(conn):
    rent = db.get_purpose_id(conn, "Rent")
    a = people.add_person(conn, "  Alice   Ash ", "2026-01")
    conn.execute("INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, 1)", (rent, a))
    b = people.add_person(conn, "Bob", "2026-04")
    assert people.get_person(conn, a)["name"] == "Alice Ash"
    assert people.get_person(conn, b)["active_from"] == "2026-04"
    assert people.usage(conn, b)["purpose_splits"] == 0


def test_add_person_validation(conn):
    people.add_person(conn, "Alice")
    with pytest.raises(PersonError):
        people.add_person(conn, "alice")
    with pytest.raises(PersonError):
        people.add_person(conn, "   ")
    with pytest.raises(PersonError):
        people.add_person(conn, "Carol", "2026/01")


def test_rename_history_follows(conn):
    a = people.add_person(conn, "Alice")
    tx = _tx(conn, 1, amount=500, person_id=a)
    people.rename_person(conn, a, "Alicia")
    row = conn.execute(
        "SELECT p.name FROM transactions t JOIN people p ON p.id = t.person_id WHERE t.id = ?", (tx,)
    ).fetchone()
    assert row["name"] == "Alicia"
    people.rename_person(conn, a, "Alicia")  # no-op rename of self is fine
    people.add_person(conn, "Bob")
    with pytest.raises(PersonError):
        people.rename_person(conn, a, "BOB")


def test_house_renamable(conn):
    house = db.get_house_id(conn)
    people.rename_person(conn, house, "Our House")
    assert people.get_person(conn, house)["name"] == "Our House"
    assert people.get_person(conn, house)["is_house"] == 1


# --- move out -----------------------------------------------------------------------


def test_move_out_leaves_dropdowns_after_month(conn):
    a = people.add_person(conn, "Alice", "2026-01")
    b = people.add_person(conn, "Bob", "2026-01")
    people.move_out(conn, a, "2026-05")
    assert names(db.active_people(conn, "2026-05")) == ["Alice", "Bob"]
    assert names(db.active_people(conn, "2026-06")) == ["Bob"]
    assert people.status(people.get_person(conn, a), "2026-06") == "Moved out"
    assert people.status(people.get_person(conn, a), "2026-05") == "Active"
    # Still listed (history intact) and can come back.
    assert "Alice" in names(db.list_people(conn))
    people.clear_move_out(conn, a)
    assert names(db.active_people(conn, "2027-01")) == ["Alice", "Bob"]
    assert b


def test_move_out_validation(conn):
    a = people.add_person(conn, "Alice", "2026-04")
    with pytest.raises(PersonError):
        people.move_out(conn, a, "2026-03")
    with pytest.raises(PersonError):
        people.move_out(conn, a, None)
    with pytest.raises(PersonError):
        people.set_active_range(conn, a, "2026-06", "2026-05")
    people.set_active_range(conn, a, "2026-02", "2026-08")
    p = people.get_person(conn, a)
    assert (p["active_from"], p["active_until"]) == ("2026-02", "2026-08")
    people.set_active_range(conn, a, None, None)
    p = people.get_person(conn, a)
    assert (p["active_from"], p["active_until"]) == (None, None)
    people.move_out(conn, a, "2026-09")
    with pytest.raises(PersonError):
        people.set_active_from(conn, a, "2026-10")
    assert people.status(people.get_person(conn, people.add_person(conn, "Zed", "2027-01")), "2026-10") == "Not yet active"


def test_house_cannot_move_out_or_delete(conn):
    house = db.get_house_id(conn)
    with pytest.raises(PersonError):
        people.move_out(conn, house, "2026-05")
    with pytest.raises(PersonError):
        people.set_active_range(conn, house, None, "2026-05")
    with pytest.raises(PersonError):
        people.set_active_from(conn, house, "2026-05")
    assert not people.can_delete(conn, house)
    with pytest.raises(PersonError):
        people.delete_person(conn, house)
    assert people.status(people.get_person(conn, house), "2026-05") == "House"


# --- delete ----------------------------------------------------------------------------


def test_delete_unused_person(conn):
    rent = db.get_purpose_id(conn, "Rent")
    a = people.add_person(conn, "Alice")
    b = people.add_person(conn, "Bob")
    conn.executemany(
        "INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, ?)",
        [(rent, a, 1), (rent, b, 2)],
    )
    conn.commit()
    assert people.can_delete(conn, a)
    people.delete_person(conn, a)
    assert "Alice" not in names(db.list_people(conn))
    left = conn.execute("SELECT person_id, weight FROM purpose_splits").fetchall()
    assert [(r["person_id"], r["weight"]) for r in left] == [(b, 2)]


def test_delete_blocked_by_tagged_transaction(conn):
    a = people.add_person(conn, "Alice")
    _tx(conn, 1, amount=500, person_id=a)
    assert not people.can_delete(conn, a)
    assert "1 transaction" in people.delete_blocker(conn, a)
    with pytest.raises(PersonError):
        people.delete_person(conn, a)


def test_delete_blocked_by_split(conn):
    a = people.add_person(conn, "Alice")
    tx = _tx(conn, 1)
    conn.execute(
        "INSERT INTO splits (transaction_id, person_id, weight, share_cents) VALUES (?, ?, 1, -100)",
        (tx, a),
    )
    conn.commit()
    assert not people.can_delete(conn, a)


def test_delete_blocked_by_rule(conn):
    a = people.add_person(conn, "Alice")
    conn.execute(
        "INSERT INTO rules (pattern, applies_to, person_id) VALUES ('ZELLE FROM A', 'deposit', ?)", (a,)
    )
    conn.commit()
    assert "1 rule" in people.delete_blocker(conn, a)
    with pytest.raises(PersonError):
        people.delete_person(conn, a)


def test_unknown_person(conn):
    with pytest.raises(PersonError):
        people.get_person(conn, 9999)


# --- page ---------------------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    c = db.connect(path)
    a = people.add_person(c, "Alice", "2026-01")
    _tx(c, 1, amount=500, person_id=a)
    people.add_person(c, "Bob", "2026-01")
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path


def _app():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(ROOT / "pages/3_People.py"), default_timeout=30).run()


def test_page_lists_people(app_db):
    at = _app()
    assert not at.exception, at.exception
    df = at.dataframe[0].value
    assert list(df["Name"]) == ["House", "Alice", "Bob"]
    assert list(df["Status"]) == ["House", "Active", "Active"]


def test_page_add_and_move_out_and_delete(app_db):
    at = _app()
    at.text_input[0].set_value("Carol")
    at.button[0].click().run()  # Add person
    assert not at.exception, at.exception
    c = db.connect(app_db)
    assert db.get_person_by_name(c, "Carol") is not None

    # House: rename only.
    assert any("not moved out or deleted" in i.value for i in at.info)

    # Bob: move out.
    bob = db.get_person_by_name(c, "Bob")["id"]
    at.selectbox(key="people_edit_pid").set_value(bob).run()
    assert not at.exception, at.exception
    until = [s for s in at.selectbox if s.label.startswith("Active until")][0]
    until.set_value("2026-05")
    [b for b in at.button if b.label == "Save dates"][0].click().run()
    assert not at.exception, at.exception
    assert db.get_person_by_name(c, "Bob")["active_until"] == "2026-05"
    assert "Bob" not in names(db.active_people(c, "2026-06"))

    # Bob has nothing tagged: delete.
    at.checkbox(key=f"del_confirm_{bob}").check().run()
    at.button(key=f"del_{bob}").click().run()
    assert not at.exception, at.exception
    assert db.get_person_by_name(c, "Bob") is None

    # Alice has a transaction: delete blocked.
    alice = db.get_person_by_name(c, "Alice")["id"]
    at.selectbox(key="people_edit_pid").set_value(alice).run()
    assert any("move them out instead" in x.value for x in at.caption)
    c.close()
