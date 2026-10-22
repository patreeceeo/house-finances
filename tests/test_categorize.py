"""Categorize data layer and page (synthetic data only)."""

from pathlib import Path

import pytest

from housefin import categorize as cat
from housefin import db, rules, splits

ROOT = Path(__file__).resolve().parent.parent

_n = 0


def add_tx(conn, date, amount_cents, description, time="12:00:00", applies_month=None):
    global _n
    _n += 1
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " applies_month) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"fp{_n}", date, time, amount_cents, "Deposit" if amount_cents > 0 else "Withdrawal",
         description, applies_month),
    )
    conn.commit()
    return cur.lastrowid


def fill(conn):
    """Housemates Alice/Bob/Cara (Cara moved out after 2026-01) plus rows."""
    people = {
        "Alice": db.add_person(conn, "Alice"),
        "Bob": db.add_person(conn, "Bob"),
        "Cara": db.add_person(conn, "Cara", active_until="2026-01"),
    }
    tx = {
        "rent": add_tx(conn, "2026-02-01", -300000, "LANDLORD CO PAYMENT"),
        "rent_old": add_tx(conn, "2026-01-01", -300000, "LANDLORD CO PAYMENT"),
        "net1": add_tx(conn, "2026-02-05", -8999, "COMCAST / XFINITY 1234"),
        "net2": add_tx(conn, "2026-01-05", -8999, "COMCAST / XFINITY 9876"),
        "zelle": add_tx(conn, "2026-02-03", 150000, "Zelle payment from ALICE A"),
        "zelle2": add_tx(conn, "2026-01-03", 150000, "Zelle payment from ALICE A"),
        "venmo": add_tx(conn, "2026-02-04", 5000, "VENMO CASHOUT 555"),
        "refund": add_tx(conn, "2026-02-06", 2500, "STORE REFUND"),
    }
    return people, tx


@pytest.fixture
def conn():
    return db.connect(":memory:")


@pytest.fixture
def data(conn):
    people, tx = fill(conn)
    return conn, people, tx


def tags(conn, tid):
    return conn.execute(
        "SELECT person_id, purpose_id, category_source, applies_month FROM transactions WHERE id = ?",
        (tid,),
    ).fetchone()


def split_of(conn, tid):
    return {r["person_id"]: (r["weight"], r["share_cents"]) for r in splits.get_transaction_split(conn, tid)}


# --- listing -----------------------------------------------------------------


def test_list_filters(data):
    conn, people, tx = data
    rows = cat.list_transactions(conn)
    assert len(rows) == 8
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates, reverse=True)

    assert {r["id"] for r in cat.list_transactions(conn, kind=cat.DEPOSITS)} == {
        tx["zelle"], tx["zelle2"], tx["venmo"], tx["refund"]
    }
    assert len(cat.list_transactions(conn, kind=cat.WITHDRAWALS)) == 4
    assert {r["id"] for r in cat.list_transactions(conn, months=["2026-01"])} == {
        tx["rent_old"], tx["net2"], tx["zelle2"]
    }
    assert {r["id"] for r in cat.list_transactions(conn, search="xfinity")} == {tx["net1"], tx["net2"]}
    assert cat.list_transactions(conn, search="100%") == []

    cat.save_person(conn, tx["zelle"], people["Alice"])
    assert tx["zelle"] not in {r["id"] for r in cat.list_transactions(conn)}
    all_rows = {r["id"]: r for r in cat.list_transactions(conn, status=cat.ALL)}
    assert all_rows[tx["zelle"]]["person_name"] == "Alice"
    # search also finds person names
    assert {r["id"] for r in cat.list_transactions(conn, status=cat.ALL, search="alice")} == {
        tx["zelle"], tx["zelle2"]
    }


def test_list_month_uses_applies_month(data):
    conn, people, tx = data
    cat.set_applies_month(conn, tx["rent_old"], "2026-02")
    assert tx["rent_old"] in {r["id"] for r in cat.list_transactions(conn, months=["2026-02"])}
    cat.set_applies_month(conn, tx["rent_old"], "")
    assert tags(conn, tx["rent_old"])["applies_month"] is None
    with pytest.raises(ValueError):
        cat.set_applies_month(conn, tx["rent_old"], "2026-13")


# --- saving ------------------------------------------------------------------


def test_save_person_manual_and_remember(data):
    conn, people, tx = data
    res = cat.save_person(conn, tx["zelle"], people["Alice"], remember=True)
    assert tuple(tags(conn, tx["zelle"]))[:3] == (people["Alice"], None, "manual")
    assert res.rule_id is not None and res.rule_tagged == 1
    assert tuple(tags(conn, tx["zelle2"]))[:3] == (people["Alice"], None, "rule")


def test_save_withdrawal_default_split_and_remember(data):
    conn, people, tx = data
    rent = db.get_purpose_id(conn, "Rent")
    res = cat.save_withdrawal(conn, tx["rent"], rent, remember=False)
    assert res.rule_id is None
    # even split among people active in Feb (Cara moved out), House excluded
    assert split_of(conn, tx["rent"]) == {people["Alice"]: (1.0, 150000), people["Bob"]: (1.0, 150000)}
    assert tags(conn, tx["rent_old"])["purpose_id"] is None


def test_save_withdrawal_custom_split_remembered_for_rule_tagged(data):
    conn, people, tx = data
    rent = db.get_purpose_id(conn, "Rent")
    weights = [(people["Alice"], 60), (people["Bob"], 40), (people["Cara"], 0)]
    res = cat.save_withdrawal(
        conn, tx["rent"], rent, weights, remember_split=True, remember=True
    )
    assert split_of(conn, tx["rent"]) == {people["Alice"]: (60, 180000), people["Bob"]: (40, 120000)}
    assert splits.get_purpose_split(conn, rent) == [(people["Alice"], 60), (people["Bob"], 40)]
    # the rule tagged the January rent and it got the just-remembered default
    assert res.rule_tagged == 1 and res.rule_tagged_ids == [tx["rent_old"]]
    assert tuple(tags(conn, tx["rent_old"]))[:3] == (None, rent, "rule")
    assert split_of(conn, tx["rent_old"]) == {people["Alice"]: (60, 180000), people["Bob"]: (40, 120000)}


def test_split_not_remembered_by_default(data):
    conn, people, tx = data
    rent = db.get_purpose_id(conn, "Rent")
    cat.save_withdrawal(conn, tx["rent"], rent, [(people["Alice"], 1)])
    assert splits.get_purpose_split(conn, rent) == []


def test_contains_rule_from_shortened_pattern(data):
    conn, people, tx = data
    internet = db.get_purpose_id(conn, "Internet")
    prev = cat.preview_rule(conn, tx["net1"], "comcast / xfinity")
    assert prev.match_type == rules.CONTAINS and prev.total == 2 and prev.will_tag == 1
    full = cat.preview_rule(conn, tx["net1"], "COMCAST / XFINITY 1234")
    assert full.match_type == rules.EXACT and full.total == 1 and full.will_tag == 0
    res = cat.save_withdrawal(conn, tx["net1"], internet, remember=True, pattern="comcast / xfinity")
    assert res.rule_tagged == 1
    assert tags(conn, tx["net2"])["purpose_id"] == internet


def test_refund_negative_shares_and_rule(data):
    conn, people, tx = data
    other = db.get_purpose_id(conn, "Other")
    cat.save_refund(conn, tx["refund"], other, [(people["Alice"], 1), (people["Bob"], 1)], remember=True)
    assert tuple(tags(conn, tx["refund"]))[:3] == (None, other, "manual")
    assert split_of(conn, tx["refund"]) == {people["Alice"]: (1, -1250), people["Bob"]: (1, -1250)}
    rule = rules.all_rules(conn)[-1]
    assert (rule.applies_to, rule.purpose_id, rule.person_id) == ("deposit", other, None)
    # switching it back to a plain deposit drops the split
    cat.save_person(conn, tx["refund"], people["Bob"])
    assert split_of(conn, tx["refund"]) == {}


def test_transfer_has_no_split(data):
    conn, people, tx = data
    rent = db.get_purpose_id(conn, "Rent")
    cat.save_withdrawal(conn, tx["rent"], rent)
    assert split_of(conn, tx["rent"])
    cat.save_withdrawal(conn, tx["rent"], db.get_transfer_purpose_id(conn), [(people["Alice"], 1)])
    assert split_of(conn, tx["rent"]) == {}


def test_remember_allowed_for_venmo(data):
    conn, people, tx = data
    cat.save_person(conn, tx["venmo"], people["Alice"], remember=True)
    assert tags(conn, tx["venmo"])["category_source"] == "manual"
    assert rules.find_best_rule(conn, "VENMO CASHOUT 555", 5000) is not None


def test_withdrawal_to_person(data):
    conn, people, tx = data
    rent = db.get_purpose_id(conn, "Rent")
    cat.save_withdrawal(conn, tx["rent"], rent)
    assert split_of(conn, tx["rent"])
    result = cat.save_person(conn, tx["rent"], people["Bob"], remember=True)
    assert tuple(tags(conn, tx["rent"]))[:3] == (people["Bob"], None, "manual")
    assert split_of(conn, tx["rent"]) == {}
    # The rule is a withdrawal rule and tags the matching uncategorized withdrawal.
    assert result.rule_tagged_ids == [tx["rent_old"]]
    assert tuple(tags(conn, tx["rent_old"]))[:3] == (people["Bob"], None, "rule")


def test_validation(data):
    conn, people, tx = data
    with pytest.raises(ValueError):
        cat.save_person(conn, tx["rent"], 9999)
    with pytest.raises(ValueError):
        cat.save_withdrawal(conn, tx["zelle"], db.get_purpose_id(conn, "Rent"))
    with pytest.raises(ValueError):
        cat.save_refund(conn, tx["rent"], db.get_purpose_id(conn, "Rent"))
    with pytest.raises(ValueError):
        cat.save_withdrawal(conn, tx["rent"], 9999)


def test_manual_tags_survive_rule_changes(data):
    conn, people, tx = data
    rent, other = db.get_purpose_id(conn, "Rent"), db.get_purpose_id(conn, "Other")
    cat.save_withdrawal(conn, tx["rent"], rent, remember=True)
    rule_id = rules.all_rules(conn)[-1].id
    cat.save_withdrawal(conn, tx["rent_old"], other)  # manual override
    rules.update_rule(conn, rule_id, purpose_id=db.get_purpose_id(conn, "Water"), reapply=True)
    assert tags(conn, tx["rent"])["purpose_id"] == rent
    assert tags(conn, tx["rent_old"])["purpose_id"] == other
    rules.apply_rules(conn, [tx["rent"], tx["rent_old"]])
    assert tags(conn, tx["rent_old"])["purpose_id"] == other


def test_applies_month_drives_default_split(data):
    conn, people, tx = data
    rent = db.get_purpose_id(conn, "Rent")
    # Feb rent counted toward January, when Cara was still in
    cat.save_withdrawal(conn, tx["rent"], rent, applies_month="2026-01")
    assert tags(conn, tx["rent"])["applies_month"] == "2026-01"
    assert set(split_of(conn, tx["rent"])) == {people["Alice"], people["Bob"], people["Cara"]}


def test_clear_tags(data):
    conn, people, tx = data
    cat.save_withdrawal(conn, tx["rent"], db.get_purpose_id(conn, "Rent"), applies_month="2026-03")
    cat.clear_tags(conn, tx["rent"])
    assert tuple(tags(conn, tx["rent"])) == (None, None, None, "2026-03")
    assert split_of(conn, tx["rent"]) == {}


def test_split_editor_helpers(data):
    conn, people, tx = data
    rent = db.get_purpose_id(conn, "Rent")
    feb = [p["name"] for p in cat.split_candidates(conn, "2026-02")]
    assert feb == ["Alice", "Bob", "House"]
    with_cara = [p["name"] for p in cat.split_candidates(conn, "2026-02", [(people["Cara"], 1)])]
    assert with_cara == ["Alice", "Bob", "Cara", "House"]

    txn = cat.get_transaction(conn, tx["rent"])
    assert cat.initial_weights(conn, txn, None) == [(people["Alice"], 1.0), (people["Bob"], 1.0)]
    cat.save_withdrawal(conn, tx["rent"], rent, [(people["Alice"], 3), (people["Bob"], 1)])
    txn = cat.get_transaction(conn, tx["rent"])
    assert dict(cat.initial_weights(conn, txn, rent)) == {people["Alice"]: 3, people["Bob"]: 1}
    other = db.get_purpose_id(conn, "Other")
    assert cat.initial_weights(conn, txn, other) == [(people["Alice"], 1.0), (people["Bob"], 1.0)]


# --- page (AppTest) ----------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    c = db.connect(path)
    people, tx = fill(c)
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path, people, tx


def run_page():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "pages" / "1_Categorize.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def select_row(at, tid):
    at.selectbox(key="cat_selected").set_value(tid).run()
    assert not at.exception, at.exception


def test_page_withdrawal_with_split_and_remember(app_db):
    path, people, tx = app_db
    at = run_page()
    assert at.selectbox(key="cat_selected").value == tx["refund"]  # newest first
    select_row(at, tx["rent"])
    k = f"cat_{tx['rent']}"
    conn = db.connect(path)
    rent = db.get_purpose_id(conn, "Rent")
    at.selectbox(key=f"{k}_purpose").set_value(rent).run()
    # even split shown; edit it
    assert at.number_input(key=f"{k}_w_{rent}_2026-02_{people['Alice']}").value == 50.0
    at.number_input(key=f"{k}_w_{rent}_2026-02_{people['Alice']}").set_value(75.0).run()
    assert any("take off 25%" in w.value for w in at.warning)
    at.number_input(key=f"{k}_w_{rent}_2026-02_{people['Bob']}").set_value(25.0).run()
    assert not at.warning
    assert at.checkbox(key=f"{k}_remember_split").value is True
    assert at.checkbox(key=f"{k}_remember").value is True
    assert at.text_input(key=f"{k}_pattern").value == "LANDLORD CO PAYMENT"
    assert any("matches 2 existing withdrawals; 1 other" in c.value for c in at.caption)
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    assert any("1 other transaction(s) updated" in s.value for s in at.success)

    conn = db.connect(path)
    assert tuple(tags(conn, tx["rent"]))[:3] == (None, rent, "manual")
    assert split_of(conn, tx["rent"]) == {people["Alice"]: (75.0, 225000), people["Bob"]: (25.0, 75000)}
    assert tags(conn, tx["rent_old"])["category_source"] == "rule"
    assert dict(splits.get_purpose_split(conn, rent)) == {people["Alice"]: 75.0, people["Bob"]: 25.0}
    # both rent rows left the uncategorized list
    assert len(at.selectbox(key="cat_selected").options) == 6


def test_page_split_percents_are_exact(app_db):
    path, people, tx = app_db
    at = run_page()
    conn = db.connect(path)
    rent = db.get_purpose_id(conn, "Rent")
    A, B, C = people["Alice"], people["Bob"], people["Cara"]

    # An even three-way split is shown rounded but saved as exact thirds.
    select_row(at, tx["rent_old"])
    k = f"cat_{tx['rent_old']}"
    at.selectbox(key=f"{k}_purpose").set_value(rent).run()
    assert at.number_input(key=f"{k}_w_{rent}_2026-01_{A}").value == 33.34
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    assert split_of(conn, tx["rent_old"]) == {A: (1.0, 100000), B: (1.0, 100000), C: (1.0, 100000)}

    # Typed percents are stored as typed, and the total check is exact.
    select_row(at, tx["rent"])
    k = f"cat_{tx['rent']}"
    at.selectbox(key=f"{k}_purpose").set_value(rent).run()
    at.number_input(key=f"{k}_w_{rent}_2026-02_{A}").set_value(12.125).run()
    at.number_input(key=f"{k}_w_{rent}_2026-02_{B}").set_value(87.874).run()
    assert any("0.001% is still unassigned" in w.value for w in at.warning)
    at.number_input(key=f"{k}_w_{rent}_2026-02_{B}").set_value(87.875).run()
    assert not at.warning
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    assert split_of(conn, tx["rent"]) == {A: (12.125, 36375), B: (87.875, 263625)}


def test_page_deposit_new_person_inline(app_db):
    path, people, tx = app_db
    at = run_page()
    select_row(at, tx["venmo"])
    k = f"cat_{tx['venmo']}"
    # Remember is offered for every description; leave it off here
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.selectbox(key=f"{k}_person").set_value(-1).run()
    at.text_input(key=f"{k}_new_person").input("Dana").run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    conn = db.connect(path)
    dana = db.get_person_by_name(conn, "Dana")
    assert dana is not None
    assert tuple(tags(conn, tx["venmo"]))[:3] == (dana["id"], None, "manual")


def test_page_withdrawal_paid_to_person(app_db):
    path, people, tx = app_db
    at = run_page()
    select_row(at, tx["net1"])
    k = f"cat_{tx['net1']}"
    at.toggle(key=f"{k}_paid_to").set_value(True).run()
    assert not [s for s in at.selectbox if s.key == f"{k}_purpose"]
    at.selectbox(key=f"{k}_person").set_value(people["Bob"]).run()
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    conn = db.connect(path)
    assert tuple(tags(conn, tx["net1"]))[:3] == (people["Bob"], None, "manual")


def test_page_new_purpose_inline_and_filters(app_db):
    path, people, tx = app_db
    at = run_page()
    select_row(at, tx["net1"])
    k = f"cat_{tx['net1']}"
    at.selectbox(key=f"{k}_purpose").set_value(-1).run()
    at.text_input(key=f"{k}_new_purpose").input("Streaming").run()
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    conn = db.connect(path)
    pid = db.get_purpose_id(conn, "Streaming")
    assert tags(conn, tx["net1"])["purpose_id"] == pid
    assert set(split_of(conn, tx["net1"])) == {people["Alice"], people["Bob"]}
    assert tags(conn, tx["net2"])["purpose_id"] is None  # not remembered

    at.radio(key="cat_status").set_value("All").run()
    at.radio(key="cat_kind").set_value("Withdrawals").run()
    at.text_input(key="cat_search").input("xfinity").run()
    assert not at.exception
    assert len(at.selectbox(key="cat_selected").options) == 2


def test_page_refund_toggle(app_db):
    path, people, tx = app_db
    at = run_page()
    k = f"cat_{tx['refund']}"
    at.toggle(key=f"{k}_refund").set_value(True).run()
    conn = db.connect(path)
    other = db.get_purpose_id(conn, "Other")
    at.selectbox(key=f"{k}_purpose").set_value(other).run()
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    conn = db.connect(path)
    assert tags(conn, tx["refund"])["purpose_id"] == other
    assert sum(s for _, s in split_of(conn, tx["refund"]).values()) == -2500


def test_page_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HOUSEFIN_DB", str(tmp_path / "empty.db"))
    at = run_page()
    assert at.info
