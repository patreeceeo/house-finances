"""AppTest tests for the Rules page (synthetic data only)."""

from pathlib import Path

import pytest

from helpers import add_resident

from housefin import db, rules, splits

ROOT = Path(__file__).resolve().parent.parent
PAGE = str(ROOT / "pages" / "2_Rules.py")


def _tx(conn, n, desc, amount, *, date="2026-03-05"):
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description)"
        " VALUES (?, ?, '00:00:00', ?, ?, ?)",
        (f"fp{n}", date, amount, "Deposit" if amount > 0 else "Withdrawal", desc),
    )
    return cur.lastrowid


def _row(path, txn_id):
    c = db.connect(path)
    try:
        return c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    finally:
        c.close()


def _split(path, txn_id):
    c = db.connect(path)
    try:
        return {
            r["person_id"]: r["share_cents"]
            for r in c.execute("SELECT * FROM splits WHERE transaction_id = ?", (txn_id,))
        }
    finally:
        c.close()


@pytest.fixture
def data(monkeypatch, tmp_path):
    path = tmp_path / "rules.db"
    c = db.connect(path)
    # Only our own rule: drop the seeded Interest Paid rule.
    with c:
        c.execute("DELETE FROM rules")
    alice = add_resident(c, "Alice")
    bob = add_resident(c, "Bob")
    internet = db.get_purpose_id(c, "Internet")
    other = db.get_purpose_id(c, "Other")
    splits.set_purpose_split(c, internet, [(alice, 1), (bob, 1)])
    splits.set_purpose_split(c, other, [(alice, 1)])

    tagged = _tx(c, 1, "COMCAST CABLE 1111", -10000)
    manual = _tx(c, 2, "COMCAST CABLE 2222", -6000)
    c.commit()
    with c:
        c.execute(
            "UPDATE transactions SET purpose_id = ?, category_source = 'manual' WHERE id = ?",
            (other, manual),
        )
    splits.set_transaction_split(c, manual, [(bob, 1)])
    rule_id, result = rules.create_rule(
        c, "comcast", rules.WITHDRAWAL, match_type=rules.CONTAINS, purpose_id=internet
    )
    assert result.purpose_changed == [tagged]
    splits.apply_default_split(c, result.purpose_changed, replace=True)
    _tx(c, 3, "PAYROLL ACME", 50000)
    c.commit()
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return dict(
        path=path, rule=rule_id, tagged=tagged, manual=manual,
        alice=alice, bob=bob, internet=internet, other=other,
    )


def _app():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_lists_rules(data):
    at = _app()
    df = at.dataframe[0].value
    assert list(df.columns) == ["Match type", "Pattern", "Applies to", "Target", "Matches"]
    row = df.iloc[0]
    assert row["Pattern"] == "comcast"
    assert row["Match type"] == "contains"
    assert row["Applies to"] == "withdrawal"
    assert row["Target"] == "Purpose: Internet"
    assert row["Matches"] == 2


def test_empty(monkeypatch, tmp_path):
    path = tmp_path / "e.db"
    c = db.connect(path)
    with c:
        c.execute("DELETE FROM rules")
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    at = _app()
    assert "No rules yet" in at.info[0].value


def _edit_to_other(at, data, reapply):
    rid = data["rule"]
    at.selectbox(key=f"rule_target_{rid}").set_value(("purpose", data["other"]))
    if reapply:
        at.radio(key=f"rule_edit_mode_{rid}").set_value(at.radio(key=f"rule_edit_mode_{rid}").options[1])
    at.button(key=f"rule_save_{rid}").click().run()
    assert not at.exception, at.exception
    return at


def test_edit_without_reapply_changes_no_rows(data):
    at = _edit_to_other(_app(), data, reapply=False)
    assert "Rule saved." in at.success[0].value
    c = db.connect(data["path"])
    assert rules.get_rule(c, data["rule"]).purpose_id == data["other"]
    c.close()
    assert _row(data["path"], data["tagged"])["purpose_id"] == data["internet"]
    assert _split(data["path"], data["tagged"]) == {data["alice"]: 5000, data["bob"]: 5000}
    assert _row(data["path"], data["manual"])["purpose_id"] == data["other"]
    assert at.dataframe[0].value.iloc[0]["Target"] == "Purpose: Other"


def test_edit_with_reapply_retags_rule_rows_only(data):
    at = _edit_to_other(_app(), data, reapply=True)
    assert "1 transaction re-tagged" in at.success[0].value
    tagged = _row(data["path"], data["tagged"])
    assert tagged["purpose_id"] == data["other"]
    assert tagged["category_source"] == "rule"
    # split recomputed from Other's default (Alice only)
    assert _split(data["path"], data["tagged"]) == {data["alice"]: 10000}
    manual = _row(data["path"], data["manual"])
    assert manual["category_source"] == "manual"
    assert _split(data["path"], data["manual"]) == {data["bob"]: 6000}


def test_edit_pattern_with_reapply_clears_unmatched(data):
    at = _app()
    rid = data["rule"]
    at.text_input(key=f"rule_pattern_{rid}").input("COMCAST CABLE 9999")
    at.radio(key=f"rule_type_{rid}").set_value(rules.EXACT)
    at.run()
    assert "Would match 0 withdrawals." in [c.value for c in at.caption]
    at.radio(key=f"rule_edit_mode_{rid}").set_value(at.radio(key=f"rule_edit_mode_{rid}").options[1])
    at.button(key=f"rule_save_{rid}").click().run()
    assert not at.exception, at.exception
    assert "(1 now uncategorized)" in at.success[0].value
    tagged = _row(data["path"], data["tagged"])
    assert tagged["purpose_id"] is None and tagged["category_source"] is None
    assert _split(data["path"], data["tagged"]) == {}
    assert _row(data["path"], data["manual"])["category_source"] == "manual"


def test_empty_pattern_shows_error(data):
    at = _app()
    rid = data["rule"]
    at.text_input(key=f"rule_pattern_{rid}").input("   ")
    at.button(key=f"rule_save_{rid}").click().run()
    assert not at.exception
    assert at.error and "empty" in at.error[0].value


@pytest.mark.parametrize("reapply", [False, True])
def test_delete(data, reapply):
    at = _app()
    rid = data["rule"]
    if reapply:
        at.radio(key=f"rule_del_mode_{rid}").set_value(at.radio(key=f"rule_del_mode_{rid}").options[1])
    at.checkbox(key=f"rule_del_confirm_{rid}").check().run()
    at.button(key=f"rule_delete_{rid}").click().run()
    assert not at.exception, at.exception
    assert "Rule deleted." in at.success[0].value
    assert "No rules yet" in at.info[0].value
    tagged = _row(data["path"], data["tagged"])
    if reapply:
        assert tagged["purpose_id"] is None and tagged["category_source"] is None
        assert _split(data["path"], data["tagged"]) == {}
    else:
        assert tagged["purpose_id"] == data["internet"]
        assert _split(data["path"], data["tagged"]) == {data["alice"]: 5000, data["bob"]: 5000}
    manual = _row(data["path"], data["manual"])
    assert manual["purpose_id"] == data["other"] and manual["category_source"] == "manual"
    assert _split(data["path"], data["manual"]) == {data["bob"]: 6000}


def test_deposit_rule_offers_people_and_refund_purposes(data):
    c = db.connect(data["path"])
    rules.create_rule(c, "PAYROLL ACME", rules.DEPOSIT, person_id=data["alice"])
    c.close()
    at = _app()
    at.selectbox(key="rules_selected").select_index(0).run()  # newest first
    target_box = [s for s in at.selectbox if s.key and s.key.startswith("rule_target_")][0]
    labels = target_box.options
    assert "Person: Alice" in labels
    assert "Purpose: Internet (refund)" in labels
    assert target_box.value == ("person", data["alice"])
