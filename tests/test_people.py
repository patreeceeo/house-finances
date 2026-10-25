from pathlib import Path

import pytest

from helpers import house_id, move_in, purpose_id

from housefin import dates, db, fmt, people, rooms
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


# --- add / rename -----------------------------------------------------------------


def test_add_person_not_in_remembered_splits(conn):
    rent = purpose_id(conn, "Rent")
    a = people.add_person(conn, "  Alice   Ash ")
    conn.execute("INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, 1)", (rent, a))
    b = people.add_person(conn, "Bob")
    assert people.get_person(conn, a)["name"] == "Alice Ash"
    assert people.usage(conn, b)["room_assignments"] == 0
    assert people.status(conn, b, "2026-04") == "Not in a room"
    assert people.usage(conn, b)["purpose_splits"] == 0


def test_add_person_validation(conn):
    people.add_person(conn, "Alice")
    with pytest.raises(PersonError):
        people.add_person(conn, "alice")
    with pytest.raises(PersonError):
        people.add_person(conn, "   ")


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
    house = house_id(conn)
    people.rename_person(conn, house, "Our House")
    assert people.get_person(conn, house)["name"] == "Our House"
    assert people.get_person(conn, house)["is_house"] == 1


# --- status, rooms, colors ------------------------------------------------------------


def test_status_from_room_assignments(conn):
    a = people.add_person(conn, "Alice")
    room = rooms.add_room(conn, "Front", 50)
    rooms.add_assignment(conn, room, a, "2026-01-20", "2026-05-03")
    assert people.status(conn, a, "2025-12") == "Not yet arrived"
    assert people.status(conn, a, "2026-01") == "In the house"
    assert people.status(conn, people.get_person(conn, a), "2026-05") == "In the house"
    assert people.status(conn, a, "2026-06") == "Moved out"
    assert people.moved_out(conn, a, "2026-06") and not people.moved_out(conn, a, "2026-05")
    # Back again later: away in between, then in the house.
    rooms.add_assignment(conn, room, a, "2026-09-01")
    assert people.status(conn, a, "2026-07") == "Away"
    assert people.status(conn, a, "2027-03") == "In the house"
    assert names(db.people_in_house(conn, "2026-06")) == []
    assert names(db.people_in_house(conn, "2026-05")) == ["Alice"]
    # History stays listed.
    assert "Alice" in names(db.list_people(conn))


def test_current_room(conn):
    a = people.add_person(conn, "Alice")
    front = rooms.add_room(conn, "Front", 50)
    rooms.add_assignment(conn, front, a, "2026-01-01", "2026-01-31")
    assert people.current_room(conn, a, "2026-01-31")["name"] == "Front"
    assert people.current_room(conn, a, "2026-02-01") is None


def test_new_color_differs(conn):
    a = people.add_person(conn, "Alice")
    old = people.get_person(conn, a)["color"]
    new = people.new_color(conn, a)
    assert new != old and people.get_person(conn, a)["color"] == new


def test_not_in_a_room_list(conn):
    a = people.add_person(conn, "Alice")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO old_active_months (person_id, active_from, active_until)"
            " VALUES (?, '2025-01', '2025-06')", (a,),
        )
    assert [tuple(r)[1:] for r in people.not_in_a_room(conn)] == [
        ("Alice", people.get_person(conn, a)["color"], "2025-01", "2025-06")
    ]
    people.dismiss_not_in_a_room(conn, a)
    assert people.not_in_a_room(conn) == []


def test_house_cannot_be_deleted(conn):
    house = house_id(conn)
    assert people.delete_blocker(conn, house)
    with pytest.raises(PersonError):
        people.delete_person(conn, house)
    assert people.status(conn, house, "2026-05") == "House"


# --- delete ----------------------------------------------------------------------------


def test_delete_unused_person(conn):
    rent = purpose_id(conn, "Rent")
    a = people.add_person(conn, "Alice")
    b = people.add_person(conn, "Bob")
    conn.executemany(
        "INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, ?)",
        [(rent, a, 1), (rent, b, 2)],
    )
    conn.commit()
    assert people.delete_blocker(conn, a) is None
    people.delete_person(conn, a)
    assert "Alice" not in names(db.list_people(conn))
    left = conn.execute("SELECT person_id, weight FROM purpose_splits").fetchall()
    assert [(r["person_id"], r["weight"]) for r in left] == [(b, 2)]


def test_delete_blocked_by_tagged_transaction(conn):
    a = people.add_person(conn, "Alice")
    _tx(conn, 1, amount=500, person_id=a)
    assert people.delete_blocker(conn, a)
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
    assert people.delete_blocker(conn, a)


def test_delete_blocked_by_rule(conn):
    a = people.add_person(conn, "Alice")
    conn.execute(
        "INSERT INTO rules (pattern, applies_to, person_id) VALUES ('ZELLE FROM A', 'deposit', ?)", (a,)
    )
    conn.commit()
    assert "1 rule" in people.delete_blocker(conn, a)
    with pytest.raises(PersonError):
        people.delete_person(conn, a)


def test_delete_blocked_by_room_assignment(conn):
    a = people.add_person(conn, "Alice")
    move_in(conn, a)
    assert "1 room assignment" in people.delete_blocker(conn, a)


def test_unknown_person(conn):
    with pytest.raises(PersonError):
        people.get_person(conn, 9999)


# --- page ---------------------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    c = db.connect(path)
    a = people.add_person(c, "Alice")
    move_in(c, a, "2026-01")
    _tx(c, 1, amount=500, person_id=a)
    people.add_person(c, "Bob")
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
    status = f"Status in {fmt.month(dates.current_month())}"
    assert list(df.columns[:4]) == ["Name", "Color", status, "Room now"]
    assert list(df["Name"]) == ["House", "Alice", "Bob"]
    assert list(df[status]) == ["House", "In the house", "Not in a room"]
    assert list(df["Room now"]) == ["", "Alice's room", ""]
    c = db.connect(app_db)
    assert list(df["Color"]) == [p["color"] for p in db.list_people(c)]
    c.close()
    assert all(c.startswith("#") for c in df["Color"])
    # No active-month or move-out controls anywhere.
    labels = [w.label for w in (*at.text_input, *at.selectbox, *at.button)]
    assert not any("active" in l.lower() or "move" in l.lower() for l in labels)
    # Statuses other than "In the house" are explained in plain words.
    assert any("**Not in a room**: has never been put in a room" in x.value for x in at.caption)


def test_page_status_away_moved_out_not_yet_arrived(app_db):
    now = dates.current_month()
    c = db.connect(app_db)
    for name, start, end in (
        ("Gone", "2020-01", dates.add_months(now, -2)),
        ("Later", dates.add_months(now, 2), None),
    ):
        move_in(c, people.add_person(c, name), start, end)
    away = people.add_person(c, "Traveller")
    move_in(c, away, "2020-01", dates.add_months(now, -1))
    move_in(c, away, dates.add_months(now, 1), None)
    c.close()
    at = _app()
    assert not at.exception, at.exception
    df = at.dataframe[0].value
    status = dict(zip(df["Name"], df[f"Status in {fmt.month(now)}"]))
    assert status["Gone"] == "Moved out"
    assert status["Later"] == "Not yet arrived"
    assert status["Traveller"] == "Away"
    text = " ".join(x.value for x in at.caption)
    for s in ("Moved out", "Not yet arrived", "Away", "Not in a room"):
        assert f"**{s}**" in text
    assert "has one after it, but none this month" in text


def test_page_color_swatch_has_readable_text(app_db):
    from housefin import colors

    at = _app()
    styles = at.dataframe[0].proto.arrow_data.styler.styles
    c = db.connect(app_db)
    for p in db.list_people(c):
        bg = p["color"]
        assert f"background-color: {bg}; color: {colors.text_color_for(bg)}" in styles
    c.close()
