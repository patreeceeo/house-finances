from pathlib import Path

import pytest

from helpers import add_resident, house_id, purpose_id

from housefin import db, purposes, rent, splits
from housefin.purposes import PurposeError

ROOT = Path(__file__).resolve().parent.parent
PAGE = str(ROOT / "pages" / "4_Purposes.py")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _txn(conn, purpose_id, amount=-1000, date="2026-09-05"):
    with conn:
        cur = conn.execute(
            "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
            " purpose_id) VALUES (?, ?, '00:00:00', ?, 'Withdrawal', 'X', ?)",
            (f"fp{amount}{date}{purpose_id}", date, amount, purpose_id),
        )
    return cur.lastrowid


# --- add / rename / delete -------------------------------------------------------


def test_add_and_name_validation(conn):
    pid = purposes.add(conn, "  Gas  ")
    assert db.get_purpose(conn, "Gas")["id"] == pid
    assert db.get_purpose(conn, "Gas")["excluded"] == 0
    with pytest.raises(PurposeError, match="already"):
        purposes.add(conn, "gas")
    with pytest.raises(PurposeError, match="name"):
        purposes.add(conn, "   ")


def test_rename(conn):
    pid = purpose_id(conn, "Other")
    purposes.rename(conn, pid, "Misc")
    assert db.get_purpose(conn, "Misc")["id"] == pid
    purposes.rename(conn, pid, "misc")  # case change of itself is fine
    with pytest.raises(PurposeError, match="already"):
        purposes.rename(conn, pid, "Rent")
    with pytest.raises(PurposeError, match="no longer exists"):
        purposes.rename(conn, 9999, "Y")


def test_transfer_is_protected(conn):
    tid = purpose_id(conn, "Transfer")
    with pytest.raises(PurposeError, match="renamed"):
        purposes.rename(conn, tid, "Moves")
    with pytest.raises(PurposeError, match="deleted"):
        purposes.delete(conn, tid)
    with pytest.raises(PurposeError, match="no split"):
        purposes.save_default_split(conn, tid, {house_id(conn): 1})
    assert purpose_id(conn, "Transfer") == tid
    row = next(p for p in purposes.list_with_usage(conn) if p["id"] == tid)
    assert row["protected"] and not row["deletable"]


def test_delete_unused_removes_default_split(conn):
    pid = purposes.add(conn, "Gas")
    a = add_resident(conn, "Alice")
    splits.set_purpose_split(conn, pid, [(a, 1)])
    purposes.delete(conn, pid)
    assert db.get_purpose(conn, "Gas") is None
    assert conn.execute("SELECT COUNT(*) FROM purpose_splits").fetchone()[0] == 0


def test_delete_refused_when_used_by_transactions(conn):
    pid = purpose_id(conn, "Water")
    add_resident(conn, "Alice")
    t = _txn(conn, pid)
    splits.apply_default_split(conn, [t])
    assert purposes.usage(conn, pid) == {"transactions": 1, "rules": 0, "splits": 1}
    with pytest.raises(PurposeError, match="1 transaction"):
        purposes.delete(conn, pid)
    assert db.get_purpose(conn, "Water") is not None


def test_delete_refused_when_used_by_rule(conn):
    pid = purpose_id(conn, "Costco")
    with conn:
        conn.execute(
            "INSERT INTO rules (pattern, applies_to, purpose_id) VALUES ('COSTCO', 'withdrawal', ?)",
            (pid,),
        )
    with pytest.raises(PurposeError, match="1 rule"):
        purposes.delete(conn, pid)
    row = next(p for p in purposes.list_with_usage(conn) if p["id"] == pid)
    assert not row["deletable"]


def test_describe_usage():
    assert purposes.describe_usage({"transactions": 0, "rules": 0, "splits": 0}) == ""
    assert purposes.describe_usage({"transactions": 2, "rules": 1, "splits": 4}) == (
        "2 transactions, 1 rule"
    )


# --- default splits ----------------------------------------------------------------


def test_candidates_are_active_people_plus_house(conn):
    a = add_resident(conn, "Alice")
    add_resident(conn, "Gone", end="2026-01")
    add_resident(conn, "Later", "2027-01")
    ids = [p["id"] for p in purposes.split_candidates(conn, "2026-10")]
    assert ids == [house_id(conn), a]
    assert [p["id"] for p in purposes.even_split_people(conn, "2026-10")] == [a]


def test_save_default_split_keeps_inactive_and_drops_zeros(conn):
    pid = purpose_id(conn, "Water")
    house = house_id(conn)
    a = add_resident(conn, "Alice")
    b = add_resident(conn, "Bob")
    gone = add_resident(conn, "Gone", end="2026-03")
    splits.set_purpose_split(conn, pid, [(a, 1), (gone, 2)])
    purposes.save_default_split(conn, pid, {house: 0, a: 35, b: 30}, month="2026-10")
    assert sorted(splits.get_purpose_split(conn, pid)) == sorted([(a, 35.0), (b, 30.0), (gone, 2.0)])
    with pytest.raises(PurposeError, match="current housemates"):
        purposes.save_default_split(conn, pid, {gone: 1}, month="2026-10")
    with pytest.raises(PurposeError, match="negative"):
        purposes.save_default_split(conn, pid, {a: -1}, month="2026-10")


def test_default_edit_only_affects_future_withdrawals(conn):
    pid = purpose_id(conn, "Internet")  # Rent splits by rooms
    a = add_resident(conn, "Alice")
    b = add_resident(conn, "Bob")
    old = _txn(conn, pid, -400000)
    splits.apply_default_split(conn, [old])  # even split
    purposes.save_default_split(conn, pid, {a: 3, b: 1}, month="2026-09")
    shares = {r["person_id"]: r["share_cents"] for r in splits.get_transaction_split(conn, old)}
    assert shares == {a: 200000, b: 200000}
    new = _txn(conn, pid, -400000, date="2026-10-01")
    splits.apply_default_split(conn, [new])
    shares = {r["person_id"]: r["share_cents"] for r in splits.get_transaction_split(conn, new)}
    assert shares == {a: 300000, b: 100000}

    purposes.clear_default_split(conn, pid)
    assert splits.get_purpose_split(conn, pid) == []


def test_split_by_rooms_purpose_has_no_default_split(conn):
    rent_id = purpose_id(conn, "Rent")
    a = add_resident(conn, "Alice")
    with pytest.raises(PurposeError, match="split by rooms.*Rooms page"):
        purposes.save_default_split(conn, rent_id, {a: 100})
    with pytest.raises(PurposeError, match="split by rooms"):
        purposes.clear_default_split(conn, rent_id)
    assert splits.get_purpose_split(conn, rent_id) == []
    row = next(p for p in purposes.list_with_usage(conn) if p["id"] == rent_id)
    assert row["split_by_rooms"] and not row["deletable"]
    # Renaming is fine (the flag, not the name, marks it); deleting is refused.
    purposes.rename(conn, rent_id, "House rent")
    assert rent.is_split_by_rooms(conn, rent_id)
    with pytest.raises(PurposeError, match="split by rooms and cannot be deleted"):
        purposes.delete(conn, rent_id)
    assert not any(p["split_by_rooms"] for p in purposes.list_with_usage(conn) if p["id"] != rent_id)


# --- page (AppTest) ----------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    c = db.connect(path)
    add_resident(c, "Alice")
    add_resident(c, "Bob")
    add_resident(c, "Gone", end="2020-01")
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path


def _app():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(PAGE, default_timeout=30).run()


def test_page_renders(app_db):
    at = _app()
    assert not at.exception
    labels = [e.label for e in at.expander]
    assert "**Rent** — split by rooms" in labels
    assert any(l.startswith("**Water**") and "even split" in l for l in labels)
    assert any(l.startswith("**Transfer**") for l in labels)
    assert any("even split among active housemates (Alice, Bob)" in i.value for i in at.info)
    # Rent shows "Split by rooms" and a pointer to the Rooms page, no editor.
    assert any("Rooms page" in i.value and "vacant" in i.value for i in at.info)
    c = db.connect(app_db)
    rent_id = purpose_id(c, "Rent")
    c.close()
    assert not [n for n in at.number_input if n.key.startswith(f"purpose_w_{rent_id}_")]
    assert at.button(key=f"purpose_delete_{rent_id}").disabled
    # Only active people and the House are offered: 3 weight inputs per
    # purpose with a default split (all but Rent and Transfer).
    assert len(at.number_input) == 3 * 7
    assert not any("Gone" in n.label for n in at.number_input)
    # Transfer has no rename/delete/split controls.
    assert len([b for b in at.button if b.label == "Rename"]) == 8


def test_page_add_rename_and_split(app_db):
    at = _app()
    at.text_input(key="purpose_new_name").input("Gas")
    at.button[0].click().run()  # Add purpose
    assert not at.exception
    assert any("Added Gas" in s.value for s in at.success)

    c = db.connect(app_db)
    gas = purpose_id(c, "Gas")
    alice = db.get_person_by_name(c, "Alice")["id"]
    bob = db.get_person_by_name(c, "Bob")["id"]

    at.number_input(key=f"purpose_w_{gas}_0_{alice}").set_value(75)
    at.number_input(key=f"purpose_w_{gas}_0_{bob}").set_value(15)
    at.run()
    assert any("10% is still unassigned" in w.value for w in at.warning)
    assert at.button(key=f"purpose_save_{gas}").disabled
    at.number_input(key=f"purpose_w_{gas}_0_{bob}").set_value(25)
    at.run()
    assert not at.warning
    at.button(key=f"purpose_save_{gas}").click().run()
    assert not at.exception
    assert sorted(splits.get_purpose_split(c, gas)) == sorted([(alice, 75.0), (bob, 25.0)])
    assert any(e.label == "**Gas** — default: Alice 75%, Bob 25%" for e in at.expander)

    at.text_input(key=f"purpose_name_{gas}").input("Fuel")
    at.button(key=f"purpose_rename_{gas}").click().run()
    assert db.get_purpose(c, "Fuel")["id"] == gas

    at.button(key=f"purpose_clear_{gas}").click().run()
    assert splits.get_purpose_split(c, gas) == []

    at.button(key=f"purpose_delete_{gas}").click().run()
    assert db.get_purpose(c, "Fuel") is None
    c.close()


def test_page_split_percents_are_exact(app_db):
    c = db.connect(app_db)
    internet, water = purpose_id(c, "Internet"), purpose_id(c, "Water")
    alice = db.get_person_by_name(c, "Alice")["id"]
    bob = db.get_person_by_name(c, "Bob")["id"]
    house = house_id(c)
    splits.set_purpose_split(c, water, [(alice, 1), (bob, 1), (house, 1)])
    at = _app()

    at.number_input(key=f"purpose_w_{internet}_0_{alice}").set_value(12.125)
    at.number_input(key=f"purpose_w_{internet}_0_{bob}").set_value(87.875)
    at.run()
    assert not at.warning
    at.button(key=f"purpose_save_{internet}").click().run()
    assert sorted(splits.get_purpose_split(c, internet)) == sorted([(alice, 12.125), (bob, 87.875)])
    assert any(e.label == "**Internet** — default: Alice 12.125%, Bob 87.875%" for e in at.expander)

    # Thirds are shown rounded; saving them unchanged keeps the exact 1/1/1.
    at.button(key=f"purpose_save_{water}").click().run()
    assert not at.exception
    assert sorted(splits.get_purpose_split(c, water)) == sorted([(alice, 1.0), (bob, 1.0), (house, 1.0)])
    c.close()


def test_page_refuses_duplicate_and_disables_delete_when_used(app_db):
    c = db.connect(app_db)
    water = purpose_id(c, "Water")
    _txn(c, water)
    c.close()
    at = _app()
    assert at.button(key=f"purpose_delete_{water}").disabled
    at.text_input(key="purpose_new_name").input("water")
    at.button[0].click().run()
    assert any("already a purpose" in e.value for e in at.error)
