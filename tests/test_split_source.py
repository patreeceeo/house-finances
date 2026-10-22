"""transactions.split_source: 'default' vs 'manual' splits (bead xl4)."""

import sqlite3

import pytest

from housefin import categorize as cat
from housefin import db, rules, splits

_n = 0


def add_tx(conn, date, amount_cents, description="SHOP 1", purpose_id=None, category_source=None):
    global _n
    _n += 1
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " purpose_id, category_source) VALUES (?, ?, '12:00:00', ?, ?, ?, ?, ?)",
        (f"fp{_n}", date, amount_cents, "Deposit" if amount_cents > 0 else "Withdrawal",
         description, purpose_id, category_source),
    )
    conn.commit()
    return cur.lastrowid


def source(conn, tid):
    return splits.get_split_source(conn, tid)


def weights(conn, tid):
    return {r["person_id"]: r["weight"] for r in splits.get_transaction_split(conn, tid)}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def setup(conn):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob")
    costco = db.get_purpose_id(conn, "Costco")
    water = db.get_purpose_id(conn, "Water")
    return conn, a, b, costco, water


# --- migration ------------------------------------------------------------------


def test_migration_backfills_existing_splits(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    for target, script in enumerate(db.MIGRATIONS[:2], start=1):
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")
    raw.execute("INSERT INTO purposes (name) VALUES ('Costco')")
    raw.execute("INSERT INTO people (name) VALUES ('Alice')")
    alice = raw.execute("SELECT id FROM people WHERE name = 'Alice'").fetchone()[0]
    ids = {}
    for name, cs, with_split in [
        ("rule", "rule", True), ("manual", "manual", True), ("bare", "manual", False),
        ("untagged", None, False),
    ]:
        cur = raw.execute(
            "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
            " purpose_id, category_source) VALUES (?, '2026-01-01', '00:00:00', -100, 'W', 'X', 1, ?)",
            (name, cs),
        )
        ids[name] = cur.lastrowid
        if with_split:
            raw.execute("INSERT INTO splits VALUES (?, ?, 1, 100)", (cur.lastrowid, alice))
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    assert source(conn, ids["rule"]) == "default"
    assert source(conn, ids["manual"]) == "manual"
    assert source(conn, ids["bare"]) is None
    assert source(conn, ids["untagged"]) is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE transactions SET split_source = 'other' WHERE id = ?", (ids["rule"],))
    conn.close()


# --- splits layer ---------------------------------------------------------------


def test_sources_recorded_by_writers(setup):
    conn, a, b, costco, water = setup
    t = add_tx(conn, "2026-02-01", -1000, purpose_id=costco)
    assert source(conn, t) is None
    splits.apply_default_split(conn, [t])
    assert source(conn, t) == "default"
    splits.set_transaction_split(conn, t, [(a, 1)])
    assert source(conn, t) == "manual"
    splits.set_transaction_split(conn, t, [])
    assert source(conn, t) is None


def test_apply_default_split_skips_only_manual(setup):
    conn, a, b, costco, water = setup
    by_default = add_tx(conn, "2026-02-01", -1000, purpose_id=costco)
    by_hand = add_tx(conn, "2026-02-01", -1000, purpose_id=costco)
    splits.apply_default_split(conn, [by_default])
    splits.set_transaction_split(conn, by_hand, [(a, 1)])
    # Raw split rows with no recorded source count as manual too.
    legacy = add_tx(conn, "2026-02-01", -1000, purpose_id=costco)
    conn.execute("INSERT INTO splits VALUES (?, ?, 1, 1000)", (legacy, b))
    conn.commit()

    splits.set_purpose_split(conn, costco, [(b, 1)])
    assert splits.apply_default_split(conn, [by_default, by_hand, legacy]) == 1
    assert weights(conn, by_default) == {b: 1}
    assert weights(conn, by_hand) == {a: 1} and source(conn, by_hand) == "manual"
    assert weights(conn, legacy) == {b: 1}  # untouched (was already b)
    assert source(conn, legacy) is None
    # replace=True (purpose changed) overrides a manual split too.
    assert splits.apply_default_split(conn, [by_hand], replace=True) == 1
    assert weights(conn, by_hand) == {b: 1} and source(conn, by_hand) == "default"


def test_changing_default_never_changes_past_splits(setup):
    conn, a, b, costco, water = setup
    t = add_tx(conn, "2026-02-01", -1000, purpose_id=costco)
    splits.apply_default_split(conn, [t])
    before = weights(conn, t)
    splits.set_purpose_split(conn, costco, [(a, 1)])
    other = add_tx(conn, "2026-02-02", -1000, purpose_id=costco)
    splits.set_transaction_split(conn, other, [(a, 1), (b, 3)], remember_for_purpose=True)
    assert weights(conn, t) == before
    assert source(conn, t) == "default"


def test_excluded_and_cleared_purpose_reset_source(setup):
    conn, a, b, costco, water = setup
    transfer = db.get_transfer_purpose_id(conn)
    t1 = add_tx(conn, "2026-02-01", -1000, purpose_id=costco)
    t2 = add_tx(conn, "2026-02-01", -1000, purpose_id=costco)
    splits.apply_default_split(conn, [t1, t2])
    conn.execute("UPDATE transactions SET purpose_id = ? WHERE id = ?", (transfer, t1))
    conn.execute("UPDATE transactions SET purpose_id = NULL WHERE id = ?", (t2,))
    conn.commit()
    splits.sync_after_rules(conn, [t1, t2])
    assert source(conn, t1) is None and weights(conn, t1) == {}
    assert source(conn, t2) is None and weights(conn, t2) == {}


def test_same_split_compares_percents():
    assert splits.same_split([(1, 1), (2, 1), (3, 1)], [(1, 33.34), (2, 33.33), (3, 33.33)])
    assert splits.same_split([(1, 35), (2, 0)], [(1, 100)])
    assert not splits.same_split([(1, 1), (2, 1)], [(1, 60), (2, 40)])


# --- rules ----------------------------------------------------------------------


def test_rule_retag_gives_default_and_never_touches_manual(setup):
    conn, a, b, costco, water = setup
    manual = add_tx(conn, "2026-02-01", -1000, "STORE 9")
    tagged = add_tx(conn, "2026-02-02", -1000, "STORE 9")
    cat.save_withdrawal(conn, manual, costco, [(a, 100)])
    _, result = rules.create_rule_from_transaction(conn, tagged, pattern="STORE 9", purpose_id=water)
    splits.sync_after_rules(conn, result)
    assert source(conn, tagged) == "default"
    assert weights(conn, manual) == {a: 100} and source(conn, manual) == "manual"
    # Moving the rule to another purpose re-splits only the rule-tagged row.
    rule_id = conn.execute("SELECT id FROM rules WHERE pattern = 'STORE 9'").fetchone()[0]
    splits.set_purpose_split(conn, costco, [(b, 1)])
    result = rules.update_rule(conn, rule_id, purpose_id=costco, reapply=True)
    splits.sync_after_rules(conn, result)
    assert weights(conn, tagged) == {b: 1} and source(conn, tagged) == "default"
    assert weights(conn, manual) == {a: 100}


# --- categorize -----------------------------------------------------------------


def test_unedited_editor_weights_save_as_default(setup):
    conn, a, b, costco, water = setup
    t = add_tx(conn, "2026-02-01", -1000)
    init = cat.initial_weights(conn, cat.get_transaction(conn, t), costco)
    percents = list(splits.to_percents(init).items())
    cat.save_withdrawal(conn, t, costco, percents)
    assert source(conn, t) == "default"
    assert cat.split_origin(conn, cat.get_transaction(conn, t), costco) == "default"
    assert cat.split_origin(conn, cat.get_transaction(conn, t), water) is None


def test_edited_weights_save_as_manual_and_back_to_default(setup):
    conn, a, b, costco, water = setup
    t = add_tx(conn, "2026-02-01", -1000)
    cat.save_withdrawal(conn, t, costco, [(a, 70), (b, 30)])
    assert source(conn, t) == "manual"
    assert cat.split_origin(conn, cat.get_transaction(conn, t), costco) == "manual"
    cat.save_withdrawal(conn, t, costco, [(a, 50), (b, 50)])  # the even default again
    assert source(conn, t) == "default"


def test_resave_unchanged_keeps_old_split_after_default_changes(setup):
    conn, a, b, costco, water = setup
    t = add_tx(conn, "2026-02-01", -1000)
    cat.save_withdrawal(conn, t, costco, None)  # even default
    splits.set_purpose_split(conn, costco, [(a, 1)])
    # The editor shows the stored 50/50; saving it unchanged keeps it.
    shown = cat.initial_weights(conn, cat.get_transaction(conn, t), costco)
    cat.save_withdrawal(conn, t, costco, list(splits.to_percents(shown).items()))
    assert weights(conn, t) == {a: 1.0, b: 1.0}
    assert source(conn, t) == "default"


def test_remember_split_keeps_manual_source(setup):
    conn, a, b, costco, water = setup
    t = add_tx(conn, "2026-02-01", -1000)
    cat.save_withdrawal(conn, t, costco, [(a, 70), (b, 30)], remember_split=True)
    assert source(conn, t) == "manual"
    assert splits.get_purpose_split(conn, costco) == [(a, 70), (b, 30)]


def test_purpose_change_gets_new_default(setup):
    conn, a, b, costco, water = setup
    splits.set_purpose_split(conn, water, [(b, 1)])
    t = add_tx(conn, "2026-02-01", -1000)
    cat.save_withdrawal(conn, t, costco, [(a, 100)])
    init = cat.initial_weights(conn, cat.get_transaction(conn, t), water)
    cat.save_withdrawal(conn, t, water, list(splits.to_percents(init).items()))
    assert weights(conn, t) == {b: 100.0} and source(conn, t) == "default"


def test_deposit_transfer_and_clear_reset_source(setup):
    conn, a, b, costco, water = setup
    transfer = db.get_transfer_purpose_id(conn)
    dep = add_tx(conn, "2026-02-01", 500)
    cat.save_refund(conn, dep, costco, [(a, 100)])
    assert source(conn, dep) == "manual"
    cat.save_person(conn, dep, a)
    assert source(conn, dep) is None
    w = add_tx(conn, "2026-02-01", -500)
    cat.save_withdrawal(conn, w, costco)
    cat.save_withdrawal(conn, w, transfer)
    assert source(conn, w) is None
    cat.save_withdrawal(conn, w, costco)
    cat.clear_tags(conn, w)
    assert source(conn, w) is None and weights(conn, w) == {}


def test_remember_tags_others_with_default_source(setup):
    conn, a, b, costco, water = setup
    t = add_tx(conn, "2026-02-01", -1000, "WAREHOUSE 1")
    other = add_tx(conn, "2026-02-03", -2000, "WAREHOUSE 1")
    res = cat.save_withdrawal(conn, t, costco, [(a, 80), (b, 20)], remember_split=True,
                              remember=True, pattern="WAREHOUSE 1")
    assert res.rule_tagged_ids == [other]
    assert source(conn, t) == "manual"
    assert source(conn, other) == "default"
    assert weights(conn, other) == {a: 80, b: 20}
