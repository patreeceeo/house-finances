"""Match deposits to shares: serialization and edit events (housefin/matching_ui.py)
and AppTest tests of the section on pages/6_Balances.py. Synthetic data only."""

from pathlib import Path

import pytest

from helpers import add_resident, purpose_id

from housefin import db, matching, splits
from housefin import matching_ui as mu

ROOT = Path(__file__).resolve().parent.parent
PAGE = str(ROOT / "pages" / "6_Balances.py")

_n = 0


def tx(conn, date, cents, *, person=None, purpose=None, weights=None, description="TEST"):
    global _n
    _n += 1
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " person_id, purpose_id) VALUES (?, ?, '12:00:00', ?, ?, ?, ?, ?)",
        (f"ui{_n}", date, cents, "Deposit" if cents > 0 else "Withdrawal", description,
         person, purpose),
    )
    conn.commit()
    if weights:
        splits.set_transaction_split(conn, cur.lastrowid, weights)
    return cur.lastrowid


def _house(c):
    """Ann and Bob split rent 1:1. Ann: Jan paid exactly, Feb short by 100,
    Mar paid 1100 (suggested: Jan balanced; Feb+Mar balanced together)."""
    h = {"ann": add_resident(c, "Ann"), "bob": add_resident(c, "Bob")}
    rent = purpose_id(c, "Rent")
    split = [(h["ann"], 1), (h["bob"], 1)]
    for m, paid in (("01", 1000), ("02", 900), ("03", 1100)):
        h[f"rent_{m}"] = tx(c, f"2026-{m}-01", -2000, purpose=rent, weights=split,
                            description="Oak St rent")
        h[f"dep_{m}"] = tx(c, f"2026-{m}-03", paid, person=h["ann"], description="Zelle")
    return h


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    return _house(conn)


def _groups(conn, h, months=None):
    return matching.groups_for(conn, h["ann"], months)


def _event(**kw):
    global _n
    _n += 1
    return {"id": f"e{_n}", **kw}


# --- serialization -------------------------------------------------------------------


def test_group_data(conn, house):
    h = house
    groups = _groups(conn, h)
    data = mu.group_data(groups)
    assert [g["title"] for g in data] == ["Jan 2026", "Feb 2026 – Mar 2026"]
    jan, febmar = data
    assert jan["key"] == groups[0].key and not jan["kept"]
    assert (jan["paid"], jan["shares_neg"], jan["diff"], jan["tone"]) == (
        "$10.00", "-$10.00", "$0.00", "balanced"
    )
    assert jan["summary"] == "1 share, 1 deposit · $10.00 · balanced"
    assert jan["next"] == febmar["key"] and febmar["next"] is None
    assert not jan["multi_month"] and febmar["multi_month"]
    assert febmar["counts"] == "2 shares, 2 deposits"
    assert febmar["option"] == "Feb 2026 – Mar 2026 (suggested) · $0.00"
    share = jan["share_items"][0]
    assert share == {
        "key": f"{h['rent_01']}-share", "tid": h["rent_01"], "side": "share",
        "date": "2026-01-01", "what": "Rent", "detail": "Oak St rent",
        "amount": "$10.00", "of": "of $20.00", "cents": 1000,
    }
    paid = jan["paid_items"][0]
    assert (paid["key"], paid["what"], paid["detail"], paid["of"], paid["amount"]) == (
        f"{h['dep_01']}-paid", "Zelle", "", "", "$10.00"
    )


def test_dates_and_months_follow_locale(conn, house):
    jan = mu.group_data(_groups(conn, house), locale="en-GB")[0]
    assert jan["share_items"][0]["date"] == "01/01/2026"
    assert jan["title"] == "Jan 2026"
    us = mu.group_data(_groups(conn, house), locale="en-US")[0]
    assert us["paid_items"][0]["date"] == "01/03/2026"


def test_owed_credit_refund_and_paid_ahead(conn, house):
    h = house
    tx(conn, "2026-04-01", -2000, purpose=purpose_id(conn, "Rent"),
       weights=[(h["ann"], 1), (h["bob"], 1)])
    tx(conn, "2026-04-10", 300, purpose=purpose_id(conn, "Costco"),
       weights=[(h["ann"], 1), (h["bob"], 1)], description="COSTCO refund")
    data = mu.group_data(_groups(conn, h))
    apr = data[-1]
    assert (apr["title"], apr["diff"], apr["tone"]) == ("Apr 2026", "-$8.50", "owed")
    refund = apr["share_items"][1]
    assert (refund["what"], refund["amount"], refund["of"]) == (
        "Costco refund", "-$1.50", "of -$3.00"
    )
    tx(conn, "2026-05-01", 5000, person=h["ann"])
    ahead = mu.group_data(_groups(conn, h))[-1]
    assert (ahead["title"], ahead["tone"], ahead["diff"]) == ("Apr 2026", "credit", "+$41.50")


def test_equation_and_component_data(conn, house):
    h = house
    groups = _groups(conn, h)
    running = matching.running_balance_cents(conn, h["ann"])
    data = mu.component_data(h["ann"], "Ann", groups, running)
    assert data["person"] == h["ann"] and data["running"] == "$0.00"
    assert data["last_event"] is None
    assert mu.component_data(h["ann"], "Ann", groups, running, last_event="e1")["last_event"] == "e1"
    assert data["equation"] == {
        "terms": [], "balanced": 2, "total": "$0.00", "total_tone": "balanced",
        "running": "$0.00", "ok": True,
    }
    assert data["can_keep_balanced"]
    # Splitting Feb+Mar leaves Feb owed and Mar in credit, still summing to 0.
    matching.split_by_month(conn, h["ann"], None, groups[1].key)
    groups = _groups(conn, h)
    eq = mu.equation_data(groups, running)
    assert [(t["label"], t["text"], t["tone"]) for t in eq["terms"]] == [
        ("Feb 2026", "-$1.00", "owed"), ("Mar 2026", "$1.00", "credit"),
    ]
    assert eq["balanced"] == 1 and eq["ok"]
    assert not mu.equation_data(groups, running + 1)["ok"]


def test_changed_since_kept_and_later_items(conn, house):
    h = house
    gid = matching.keep(conn, h["ann"], None, _groups(conn, h)[1].key)
    conn.execute("UPDATE transactions SET amount_cents = 950 WHERE id = ?", (h["dep_02"],))
    conn.commit()
    g = mu.group_data(_groups(conn, h))[1]
    assert g["key"] == f"k:{gid}" and g["kept"]
    assert g["changed"] == "Changed since kept: deposits were $20.00, shares were $20.00"
    assert g["later"] is None
    feb = mu.group_data(_groups(conn, h, ["2026-02"]))[1]
    assert feb["later"] == (
        "Also has 2 items after the latest selected month, not shown or counted here."
    )


# --- edit events ---------------------------------------------------------------------


def test_apply_keep_and_unkeep(conn, house):
    h = house
    jan = _groups(conn, h)[0]
    assert mu.apply_edit(conn, h["ann"], None, _event(person=h["ann"], action="keep",
                                                      group=jan.key)) is None
    kept = _groups(conn, h)[0]
    assert kept.kept
    assert mu.apply_edit(conn, h["ann"], None, _event(person=h["ann"], action="unkeep",
                                                      group=kept.key)) is None
    assert not _groups(conn, h)[0].kept


def test_apply_keep_all_balanced(conn, house):
    h = house
    assert mu.apply_edit(conn, h["ann"], None,
                         _event(person=h["ann"], action="keep_all_balanced")) is None
    assert all(g.kept for g in _groups(conn, h))


def test_apply_move_new_and_into_group(conn, house):
    h = house
    jan, febmar = _groups(conn, h)
    ev = _event(person=h["ann"], action="move", items=[[h["dep_03"], "paid"]], target="new")
    assert mu.apply_edit(conn, h["ann"], None, ev) is None
    groups = _groups(conn, h)
    alone = [g for g in groups if [i.key for i in g.items] == [(h["dep_03"], "paid")]]
    assert alone and alone[0].kept
    ev = _event(person=h["ann"], action="move", items=[[h["dep_03"], "paid"]],
                target=jan.key)
    assert mu.apply_edit(conn, h["ann"], None, ev) is None
    assert (h["dep_03"], "paid") in [i.key for i in _groups(conn, h)[0].items]
    check = matching.check_groups(conn, h["ann"], None, _groups(conn, h))
    assert check.ok


def test_apply_split_merge_note(conn, house):
    h = house
    febmar = _groups(conn, h)[1]
    assert mu.apply_edit(conn, h["ann"], None,
                         _event(person=h["ann"], action="split", group=febmar.key)) is None
    jan, feb, mar = _groups(conn, h)
    assert (feb.months, mar.months) == (("2026-02",), ("2026-03",))
    assert mu.apply_edit(conn, h["ann"], None, _event(
        person=h["ann"], action="note", group=feb.key, note="  short by $1  ")) is None
    assert _groups(conn, h)[1].note == "short by $1"
    assert mu.apply_edit(conn, h["ann"], None, _event(
        person=h["ann"], action="merge", group=feb.key, other=mar.key)) is None
    groups = _groups(conn, h)
    assert len(groups) == 2 and groups[1].months == ("2026-02", "2026-03")
    assert groups[1].note == "short by $1"


def test_stale_events_save_nothing(conn, house):
    h = house
    jan = _groups(conn, h)[0]
    # A suggestion key from before an edit that changed it.
    matching.move(conn, h["ann"], None, [(h["dep_02"], "paid")], jan.key)
    count = conn.execute("SELECT COUNT(*) FROM match_group_items").fetchone()[0]
    for ev in (
        _event(person=h["ann"], action="keep", group=jan.key),
        _event(person=h["ann"], action="move", items=[[999999, "paid"]], target="new"),
        _event(person=h["ann"], action="split", group="k:999999"),
        _event(person=h["bob"], action="keep_all_balanced"),  # drawn for another person
    ):
        assert mu.apply_edit(conn, h["ann"], None, ev) == mu.STALE
    assert conn.execute("SELECT COUNT(*) FROM match_group_items").fetchone()[0] == count


def test_refusals_are_reported(conn, house):
    h = house
    jan = _groups(conn, h)[0]
    msg = mu.apply_edit(conn, h["ann"], None, _event(person=h["ann"], action="unkeep",
                                                     group=jan.key))
    assert msg == "Only a kept group can go back to being a suggestion. Nothing was saved."
    msg = mu.apply_edit(conn, h["ann"], None, _event(person=h["ann"], action="frobnicate"))
    assert msg == "Unknown edit 'frobnicate'; nothing was saved."
    ev = _event(person=h["ann"], action="move", items=[[h["dep_03"], "paid"]],
                target=_groups(conn, h)[1].key)
    msg = mu.apply_edit(conn, h["ann"], None, ev)
    assert msg == "Those items are already in that group. Nothing was saved."
    assert conn.execute("SELECT COUNT(*) FROM match_groups").fetchone()[0] == 0


def test_take_event_once():
    seen = {}
    ev = {"id": "a", "action": "keep"}
    assert mu.take_event(seen, "k", ev)
    assert not mu.take_event(seen, "k", ev)  # the same event again
    assert mu.take_event(seen, "k", {"id": "b"})
    assert not mu.take_event(seen, "k", None)
    assert not mu.take_event(seen, "k", {"action": "keep"})  # no id


def test_edit_callback_applies_once_and_flashes(conn, house, monkeypatch):
    import streamlit as st

    h = house
    state = {}
    monkeypatch.setattr(st, "session_state", state)
    callback = mu._edit_callback(conn, h["ann"], None, "mg")
    jan = _groups(conn, h)[0]
    state["mg"] = {"edit": _event(person=h["ann"], action="keep", group=jan.key)}
    callback()
    assert _groups(conn, h)[0].kept and "_mg_flash" not in state
    callback()  # the same event, seen again: not re-applied (would be stale now)
    assert "_mg_flash" not in state
    state["mg"] = {"edit": _event(person=h["ann"], action="keep", group=jan.key)}
    callback()  # a new event naming the suggestion that is now kept
    assert state["_mg_flash"] == ("warning", mu.STALE)


# --- page (AppTest) ------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "balances.db"
    c = db.connect(path)
    ids = _house(c)
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return dict(path=path, **ids)


def _app():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(PAGE, default_timeout=30).run()


def test_section_renders_with_picker(app_db):
    at = _app()
    assert not at.exception, at.exception
    assert any(s.value == "Match deposits to shares" for s in at.subheader)
    picker = at.selectbox(key="match_person")
    assert picker.value is None
    assert picker.options == ["Ann", "Bob"]
    picker.select(app_db["ann"]).run()
    assert not at.exception, at.exception
    assert not at.error
    # AppTest doesn't run the component's JS; check the data it was given.
    data = _component_data(at)
    assert (data["person"], data["name"]) == (app_db["ann"], "Ann")
    assert [g["title"] for g in data["groups"]] == ["Jan 2026", "Feb 2026 – Mar 2026"]
    assert data["equation"]["ok"]


def _component_data(at):
    import json

    (el,) = [n for n in at.main.children.values() if getattr(n, "type", None) == "bidi_component"]
    return json.loads(el.proto.json)


def test_clicking_a_row_preselects_the_person(app_db):
    at = _app()
    at.session_state["balances_table"] = {
        "selection": {"rows": [], "columns": [], "cells": [[1, "Running balance ($)"]]}
    }
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox(key="match_person").value == app_db["bob"]
    assert any(s.value.startswith("Bob: running balance") for s in at.subheader)


def test_moved_out_person_in_picker(app_db):
    c = db.connect(app_db["path"])
    cy = add_resident(c, "Cy", "2025-01", "2025-06")
    tx(c, "2025-03-03", 500, person=cy)
    c.close()
    at = _app()
    picker = at.selectbox(key="match_person")
    assert "Cy (moved out)" in picker.options
    picker.select(cy).run()
    assert not at.exception, at.exception
    assert not at.error


def test_invariant_error_instead_of_groups(app_db, monkeypatch):
    real = matching.check_groups

    def off_by_one(conn, person_id, months, groups):
        check = real(conn, person_id, months, groups)
        return matching.Check(check.groups_cents + 1, check.running_cents)

    monkeypatch.setattr(matching, "check_groups", off_by_one)
    at = _app()
    at.selectbox(key="match_person").select(app_db["ann"]).run()
    assert not at.exception, at.exception
    assert len(at.error) == 1
    body = at.error[0].value
    assert "add up to \\$0.01" in body and "Running balance is \\$0.00" in body
    assert "off by \\$0.01" in body
    assert not [n for n in at.main.children.values() if getattr(n, "type", None) == "bidi_component"]
