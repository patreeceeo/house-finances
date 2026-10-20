"""Balances: paid in, shares, net, running balance, rows, drill-down, warning.

All data is synthetic.
"""

from pathlib import Path

import pytest

from housefin import balances, db, splits

ROOT = Path(__file__).resolve().parent.parent
_n = 0


def tx(conn, date, cents, *, person=None, purpose=None, weights=None, applies_month=None,
       description="TEST"):
    global _n
    _n += 1
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " person_id, purpose_id, applies_month)"
        " VALUES (?, ?, '12:00:00', ?, ?, ?, ?, ?, ?)",
        (f"fp{_n}", date, cents, "Deposit" if cents > 0 else "Withdrawal", description,
         person, purpose, applies_month),
    )
    conn.commit()
    if weights:
        splits.set_transaction_split(conn, cur.lastrowid, weights)
    return cur.lastrowid


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    """Ann and Bob always active; Cy moved out after 2026-01."""
    ids = {
        "ann": db.add_person(conn, "Ann"),
        "bob": db.add_person(conn, "Bob"),
        "cy": db.add_person(conn, "Cy", active_until="2026-01"),
        "house": db.get_house_id(conn),
        "rent": db.get_purpose_id(conn, "Rent"),
        "costco": db.get_purpose_id(conn, "Costco"),
        "transfer": db.get_transfer_purpose_id(conn),
    }
    return ids


def by_name(rows):
    return {r.name: r for r in rows}


def test_paid_share_net_and_running_carry_forward(conn, house):
    h = house
    # Jan: rent 300 split evenly among Ann, Bob, Cy; Ann pays 100, Bob 50.
    tx(conn, "2026-01-01", -30000, purpose=h["rent"],
       weights=[(h["ann"], 1), (h["bob"], 1), (h["cy"], 1)])
    tx(conn, "2026-01-05", 10000, person=h["ann"])
    tx(conn, "2026-01-06", 5000, person=h["bob"])
    # Feb: rent 200 Ann/Bob; Ann pays 150; refund of 20 to Ann+Bob; transfer ignored.
    tx(conn, "2026-02-01", -20000, purpose=h["rent"], weights=[(h["ann"], 1), (h["bob"], 1)])
    tx(conn, "2026-02-03", 15000, person=h["ann"])
    tx(conn, "2026-02-10", 2000, purpose=h["costco"], weights=[(h["ann"], 1), (h["bob"], 1)])
    tx(conn, "2026-02-11", -99999, purpose=h["transfer"])
    tx(conn, "2026-02-12", 99999, purpose=h["transfer"], person=h["ann"])

    feb = by_name(balances.balances(conn, ["2026-02"]))
    assert set(feb) == {"Ann", "Bob", "Cy"}  # Cy moved out with balance -100
    ann = feb["Ann"]
    assert ann.paid_in_cents == 15000
    assert ann.share_cents == 10000 - 1000
    assert ann.net_cents == 6000
    # Jan net 0 (100 - 100) carried forward + Feb 60
    assert ann.running_cents == 6000
    bob = feb["Bob"]
    assert bob.net_cents == 0 - 9000
    assert bob.running_cents == (5000 - 10000) + (0 - 9000)
    assert feb["Cy"].moved_out and feb["Cy"].running_cents == -10000
    assert feb["Cy"].paid_in_cents == 0 and feb["Cy"].share_cents == 0

    jan = by_name(balances.balances(conn, ["2026-01"]))
    assert jan["Bob"].running_cents == jan["Bob"].net_cents == -5000
    assert not jan["Cy"].moved_out

    # All: net == running balance; House never shown.
    every = balances.balances(conn)
    assert "House" not in {r.name for r in every}
    for r in every:
        assert r.net_cents == r.running_cents


def test_moved_out_zero_balance_disappears(conn, house):
    h = house
    tx(conn, "2026-01-01", -9000, purpose=h["rent"],
       weights=[(h["ann"], 1), (h["bob"], 1), (h["cy"], 1)])
    tx(conn, "2026-01-02", 3000, person=h["cy"])
    tx(conn, "2026-02-01", -100, purpose=h["rent"], weights=[(h["ann"], 1)])
    names = {r.name for r in balances.balances(conn)}
    assert names == {"Ann", "Bob"}
    assert "Cy" not in {r.name for r in balances.balances(conn, ["2026-02"])}
    # In January Cy was active, so still shown there (not moved out yet).
    jan = by_name(balances.balances(conn, ["2026-01"]))
    assert jan["Cy"].net_cents == 0 and not jan["Cy"].moved_out


def test_people_not_yet_active_are_hidden(conn, house):
    db.add_person(conn, "Dee", active_from="2026-03")
    tx(conn, "2026-01-01", 100, person=house["ann"])
    tx(conn, "2026-03-01", 100, person=house["ann"])
    assert "Dee" not in {r.name for r in balances.balances(conn, ["2026-01"])}
    assert "Dee" in {r.name for r in balances.balances(conn, ["2026-03"])}
    assert "Dee" in {r.name for r in balances.balances(conn)}


def test_applies_month_and_multi_month_selection(conn, house):
    h = house
    tx(conn, "2026-01-30", 1000, person=h["ann"], applies_month="2026-02")
    tx(conn, "2026-03-01", 500, person=h["ann"])
    tx(conn, "2026-04-01", 700, person=h["ann"])
    ann = by_name(balances.balances(conn, ["2026-03", "2026-02"]))["Ann"]
    assert ann.paid_in_cents == 1500
    assert ann.running_cents == 1500  # April is after the selection
    assert balances.paid_in_totals(conn, ["2026-01"]) == {}


def test_house_share_absorbed(conn, house):
    h = house
    tx(conn, "2026-01-01", -1000, purpose=h["costco"], weights=[(h["house"], 1), (h["ann"], 1)])
    rows = by_name(balances.balances(conn))
    assert rows["Ann"].share_cents == 500
    assert "House" not in rows


def test_payout_to_housemate(conn, house):
    h = house
    tx(conn, "2026-01-01", 5000, person=h["bob"])
    tx(conn, "2026-01-02", -5000, purpose=db.get_purpose_id(conn, "Other"),
       weights=[(h["bob"], 1)])
    assert by_name(balances.balances(conn))["Bob"].running_cents == 0


def test_drill_down(conn, house):
    h = house
    d1 = tx(conn, "2026-01-05", 10000, person=h["ann"], description="DEP A")
    d2 = tx(conn, "2026-02-05", 2000, person=h["ann"], description="DEP B")
    tx(conn, "2026-02-06", 99, person=h["ann"], purpose=h["transfer"])
    w1 = tx(conn, "2026-01-01", -30000, purpose=h["rent"], weights=[(h["ann"], 2), (h["bob"], 1)])
    r1 = tx(conn, "2026-02-10", 300, purpose=h["costco"], weights=[(h["ann"], 1), (h["bob"], 2)])

    assert [r["id"] for r in balances.paid_in_transactions(conn, h["ann"])] == [d2, d1]
    assert [r["id"] for r in balances.paid_in_transactions(conn, h["ann"], ["2026-01"])] == [d1]
    assert [r["id"] for r in balances.paid_in_transactions(conn, h["ann"], through="2026-01")] == [d1]

    shares = balances.share_transactions(conn, h["ann"])
    assert [(r["id"], r["share_cents"], r["purpose"]) for r in shares] == [
        (r1, -100, "Costco"),
        (w1, 20000, "Rent"),
    ]
    assert [r["id"] for r in balances.share_transactions(conn, h["bob"], ["2026-02"])] == [r1]
    # Drill-down totals agree with the balance row.
    ann = by_name(balances.balances(conn, ["2026-02"]))["Ann"]
    assert sum(r["amount_cents"] for r in balances.paid_in_transactions(conn, h["ann"], ["2026-02"])) == ann.paid_in_cents
    assert sum(r["share_cents"] for r in balances.share_transactions(conn, h["ann"], ["2026-02"])) == ann.share_cents


def test_uncategorized_withdrawals(conn, house):
    h = house
    tx(conn, "2026-01-01", -1000)  # no purpose
    tx(conn, "2026-01-02", -250, purpose=h["rent"])  # purpose but no split
    tx(conn, "2026-01-03", -5000, purpose=h["transfer"])  # transfer: fine
    tx(conn, "2026-01-04", -700, purpose=h["rent"], weights=[(h["ann"], 1)])  # fine
    tx(conn, "2026-01-05", 400)  # uncategorized deposit: not a withdrawal
    tx(conn, "2026-02-01", -30)
    assert balances.uncategorized_withdrawals(conn, ["2026-01"]) == (2, -1250)
    assert balances.uncategorized_withdrawals(conn) == (3, -1280)
    assert balances.uncategorized_withdrawals(conn, ["2026-03"]) == (0, 0)


def test_reference_month(conn, house):
    assert balances.reference_month(conn, ["2026-01", "2026-03"]) == "2026-03"
    tx(conn, "2026-05-01", 100, person=house["ann"])
    assert balances.reference_month(conn, None) == "2026-05"


# --- page --------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    c = db.connect(path)
    ann = db.add_person(c, "Ann")
    cy = db.add_person(c, "Cy", active_until="2026-01")
    rent = db.get_purpose_id(c, "Rent")
    tx(c, "2026-01-01", -2000, purpose=rent, weights=[(ann, 1), (cy, 1)])
    tx(c, "2026-01-02", 500, person=ann)
    tx(c, "2026-02-01", -123)  # uncategorized
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path


def test_page_renders_table_and_warning(app_db):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "pages/6_Balances.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("1 withdrawal" in w.value and "$1.23" in w.value for w in at.warning)
    df = at.dataframe[0].value
    assert list(df["Person"]) == ["Ann", "Cy (moved out)"]
    assert list(df["Running balance ($)"]) == [-5.0, -10.0]


def test_page_drill_down(app_db):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "pages/6_Balances.py"), default_timeout=30)
    at.session_state["balances_table"] = {
        "selection": {"rows": [], "columns": [], "cells": [[0, "Running balance ($)"]]}
    }
    at.run()
    assert not at.exception, at.exception
    assert len(at.dataframe) == 3  # balances + deposits + shares
    assert list(at.dataframe[1].value["Amount ($)"]) == [5.0]
    assert list(at.dataframe[2].value["Share ($)"]) == [10.0]
