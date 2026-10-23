"""Default splits that leave out inactive people are pending review (bead no5)."""

import sqlite3

import pytest

from helpers import add_resident

from housefin import balances
from housefin import categorize as cat
from housefin import db, splits

_n = 0


def add_tx(conn, date, amount_cents, purpose_id=None, description="RENT"):
    global _n
    _n += 1
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " purpose_id) VALUES (?, ?, '12:00:00', ?, 'Withdrawal', ?, ?)",
        (f"fp{_n}", date, amount_cents, description, purpose_id),
    )
    conn.commit()
    return cur.lastrowid


def left_out(conn, tid):
    return {r["person_id"]: r["weight"] for r in splits.get_left_out(conn, tid)}


def weights(conn, tid):
    return {r["person_id"]: r["weight"] for r in splits.get_transaction_split(conn, tid)}


@pytest.fixture
def setup():
    """Water default 40/35/25 Ann/Bob/Moana; Moana moves in 2026-07."""
    conn = db.connect(":memory:")
    ann = add_resident(conn, "Ann")
    bob = add_resident(conn, "Bob")
    moana = add_resident(conn, "Moana", "2026-07")
    rent = db.get_purpose_id(conn, "Water")
    splits.set_purpose_split(conn, rent, [(ann, 40), (bob, 35), (moana, 25)])
    yield conn, ann, bob, moana, rent
    conn.close()


def test_default_split_detail(setup):
    conn, ann, bob, moana, rent = setup
    assert splits.default_split_detail(conn, rent, "2026-06") == (
        [(ann, 40.0), (bob, 35.0)], [(moana, 25.0)]
    )
    assert splits.default_split_detail(conn, rent, "2026-07") == (
        [(ann, 40.0), (bob, 35.0), (moana, 25.0)], []
    )


def test_apply_default_records_who_was_left_out(setup):
    conn, ann, bob, moana, rent = setup
    june = add_tx(conn, "2026-06-01", -100000, rent)
    july = add_tx(conn, "2026-07-01", -100000, rent)
    splits.apply_default_split(conn, [june, july])
    assert weights(conn, june) == {ann: 40.0, bob: 35.0}
    assert left_out(conn, june) == {moana: 25.0}
    assert left_out(conn, july) == {}
    assert balances.splits_pending_review(conn) == (1, -100000)
    assert balances.splits_pending_review(conn, ["2026-07"]) == (0, 0)
    assert [r["id"] for r in cat.list_transactions(conn, status=cat.REVIEW)] == [june]


def test_confirm_keeps_split_and_clears_review(setup):
    conn, ann, bob, moana, rent = setup
    june = add_tx(conn, "2026-06-01", -100000, rent)
    splits.apply_default_split(conn, [june])
    splits.confirm_split(conn, june)
    assert weights(conn, june) == {ann: 40.0, bob: 35.0}
    assert left_out(conn, june) == {}
    assert balances.splits_pending_review(conn) == (0, 0)


def test_clearing_or_replacing_split_clears_review(setup):
    conn, ann, bob, moana, rent = setup
    june = add_tx(conn, "2026-06-01", -100000, rent)
    splits.apply_default_split(conn, [june])
    splits.set_transaction_split(conn, june, [(ann, 50), (bob, 50)])
    assert left_out(conn, june) == {}
    splits.apply_default_split(conn, [june], replace=True)
    assert left_out(conn, june) == {moana: 25.0}
    cat.clear_tags(conn, june)
    assert left_out(conn, june) == {}


def test_saving_from_editor_confirms(setup):
    conn, ann, bob, moana, rent = setup
    june = add_tx(conn, "2026-06-01", -100000, rent)
    splits.apply_default_split(conn, [june])
    txn = cat.get_transaction(conn, june)
    assert cat.left_out_for_editor(conn, txn, rent) == [("Moana", 25.0)]
    # Saved unchanged: same split, now confirmed.
    cat.save_withdrawal(conn, june, rent, cat.initial_weights(conn, txn, rent))
    assert weights(conn, june) == {ann: 40.0, bob: 35.0}
    assert left_out(conn, june) == {}
    assert cat.left_out_for_editor(conn, cat.get_transaction(conn, june), rent) == []


def test_editor_preview_for_new_purpose(setup):
    conn, ann, bob, moana, rent = setup
    june = add_tx(conn, "2026-06-01", -100000)
    txn = cat.get_transaction(conn, june)
    assert cat.left_out_for_editor(conn, txn, rent) == [("Moana", 25.0)]
    assert cat.left_out_for_editor(conn, {**dict(txn), "applies_month": "2026-07"}, rent) == []
    # Saving through the editor is the person's decision: not pending.
    cat.save_withdrawal(conn, june, rent, cat.initial_weights(conn, txn, rent))
    assert left_out(conn, june) == {}


def test_save_without_editor_weights_is_pending(setup):
    conn, ann, bob, moana, rent = setup
    june = add_tx(conn, "2026-06-01", -100000)
    cat.save_withdrawal(conn, june, rent)  # weights None: the purpose default
    assert left_out(conn, june) == {moana: 25.0}


def test_migration_backfills_left_out(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    for target, script in enumerate(db.MIGRATIONS[:3], start=1):
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")
    raw.execute("INSERT INTO purposes (name) VALUES ('Rent')")
    rent = raw.execute("SELECT id FROM purposes WHERE name = 'Rent'").fetchone()[0]
    ids = {}
    for name, start in (("Ann", None), ("Moana", "2026-07")):
        raw.execute("INSERT INTO people (name, active_from) VALUES (?, ?)", (name, start))
        ids[name] = raw.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()[0]
    raw.executemany(
        "INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, ?)",
        [(rent, ids["Ann"], 60), (rent, ids["Moana"], 40)],
    )
    rows = {}
    for key, date, src in (("june", "2026-06-01", "default"), ("july", "2026-07-01", "default"),
                           ("manual", "2026-06-02", "manual")):
        cur = raw.execute(
            "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
            " purpose_id, split_source) VALUES (?, ?, '12:00:00', -1000, 'Withdrawal', 'R', ?, ?)",
            (key, date, rent, src),
        )
        rows[key] = cur.lastrowid
        raw.execute(
            "INSERT INTO splits (transaction_id, person_id, weight, share_cents) VALUES (?, ?, 60, 1000)",
            (rows[key], ids["Ann"]),
        )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    assert left_out(conn, rows["june"]) == {ids["Moana"]: 40.0}
    assert left_out(conn, rows["july"]) == {}  # Moana active: not left out for that reason
    assert left_out(conn, rows["manual"]) == {}  # set by hand
    conn.close()


# --- pages ----------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    ann = add_resident(conn, "Ann")
    bob = add_resident(conn, "Bob")
    moana = add_resident(conn, "Moana", "2026-07")
    rent = db.get_purpose_id(conn, "Water")
    splits.set_purpose_split(conn, rent, [(ann, 40), (bob, 35), (moana, 25)])
    june = add_tx(conn, "2026-06-01", -100000, rent)
    splits.apply_default_split(conn, [june])
    conn.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path, june


def _page(name):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    root = Path(__file__).resolve().parent.parent
    at = AppTest.from_file(str(root / "pages" / name), default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_categorize_page_review_flow(app_db):
    path, june = app_db
    at = _page("1_Categorize.py")
    at.radio(key="cat_status").set_value("Needs review").run()
    assert at.selectbox(key="cat_selected").value == june
    assert len(at.selectbox(key="cat_selected").options) == 1
    k = f"cat_{june}"
    assert any("Moana (25%) is in the Water default split but not active in" in w.value
               for w in at.warning)
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    assert any("No splits are waiting for review" in i.value for i in at.info)
    conn = db.connect(path)
    assert left_out(conn, june) == {}
    conn.close()


def test_balances_page_warns(app_db):
    at = _page("6_Balances.py")
    assert any("1 split totalling $1,000.00" in w.value and "Needs review" in w.value
               for w in at.warning)
