"""Categorize rent editor: vacant share and "Recalculate from rooms"
(docs/rooms-spec.md, "Review when a room is vacant")."""

from pathlib import Path

import pytest

from housefin import categorize as cat
from housefin import db, rooms, splits

ROOT = Path(__file__).resolve().parent.parent
JULY = 1572803  # the spec's worked example: $15,728.03

_n = 0


def add_tx(conn, date, amount_cents, purpose_id=None, description="RENT PAYMENT"):
    global _n
    _n += 1
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " purpose_id) VALUES (?, ?, '12:00:00', ?, ?, ?, ?)",
        (f"re{_n}", date, amount_cents, "Withdrawal" if amount_cents < 0 else "Deposit",
         description, purpose_id),
    )
    conn.commit()
    return cur.lastrowid


def build_house(conn):
    """July 2026 from the spec: Attic vacant 11-31, Nook vacant 1-14."""
    ids = {
        "Front": rooms.add_room(conn, "Front", 40),
        "Back": rooms.add_room(conn, "Back", 33.375),
        "Attic": rooms.add_room(conn, "Attic", 20),
        "Nook": rooms.add_room(conn, "Nook", 6.625),
    }
    for name in ("Ann", "Bob", "Cy", "Moana"):
        ids[name] = db.add_person(conn, name)
    rooms.add_assignment(conn, ids["Front"], ids["Ann"], "2026-01-01")
    rooms.add_assignment(conn, ids["Back"], ids["Bob"], "2026-01-01")
    rooms.add_assignment(conn, ids["Attic"], ids["Cy"], "2026-01-01", "2026-07-10")
    rooms.add_assignment(conn, ids["Nook"], ids["Moana"], "2026-07-15")
    ids["House"] = db.get_house_id(conn)
    ids["Rent"] = db.get_purpose_id(conn, "Rent")
    return ids


def fill_july(conn, ids):
    rooms.add_assignment(conn, ids["Attic"], ids["Cy"], "2026-07-11", "2026-07-31")
    nook = rooms.list_assignments(conn, ids["Nook"])[0]
    rooms.update_assignment(conn, nook["id"], ids["Nook"], ids["Moana"], "2026-07-01")


def shares(conn, tid):
    return {r["person_id"]: r["share_cents"] for r in splits.get_transaction_split(conn, tid)}


@pytest.fixture
def house():
    conn = db.connect(":memory:")
    ids = build_house(conn)
    yield conn, ids
    conn.close()


# --- data layer -------------------------------------------------------------------


def test_rent_seeded_as_split_by_rooms(house):
    conn, ids = house
    assert cat.is_rent(conn, ids["Rent"])
    assert not cat.is_rent(conn, db.get_purpose_id(conn, "Water"))


def test_plan_needs_vacant_choice(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-01", -JULY)
    plan = cat.rent_plan(conn, cat.get_transaction(conn, t))
    assert plan.needs_vacant_choice
    assert plan.weights == [] and plan.error.startswith("Choose who covers the vacant share ($2,601.47)")
    assert plan.split.warnings() == [
        "Attic vacant 11–31 Jul (420 of 3,100)",
        "Nook vacant 1–14 Jul (92.75 of 3,100)",
    ]
    # Saving without a choice leaves the payment unsplit, waiting for review.
    cat.save_withdrawal(conn, t, ids["Rent"])
    assert shares(conn, t) == {} and splits.needs_room_review(conn, t)


def test_vacant_share_to_house_saved_exactly(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-01", -JULY)
    plan = cat.rent_plan(conn, cat.get_transaction(conn, t), None, [ids["House"]])
    assert plan.error is None
    lines = {ln.name: (ln.room_cents, ln.vacant_cents) for ln in plan.lines}
    assert lines == {
        "Ann": (629121, 0), "Bob": (524923, 0), "Cy": (101471, 0), "Moana": (57141, 0),
        "House": (0, 260147),
    }
    cat.save_withdrawal(conn, t, ids["Rent"], plan.weights)
    assert shares(conn, t) == {ids["Ann"]: 629121, ids["Bob"]: 524923, ids["Cy"]: 101471,
                               ids["Moana"]: 57141, ids["House"]: 260147}
    assert sum(shares(conn, t).values()) == JULY
    assert splits.get_split_source(conn, t) == splits.MANUAL
    assert not splits.needs_room_review(conn, t)
    assert t not in [r["id"] for r in cat.list_transactions(conn, status=cat.REVIEW)]


def test_vacant_share_split_among_people(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-01", -JULY)
    plan = cat.rent_plan(conn, cat.get_transaction(conn, t), None, [ids["Ann"], ids["Bob"], ids["Cy"]])
    vac = {ln.name: ln.vacant_cents for ln in plan.lines}
    assert sorted(vac.values()) == [0, 86715, 86716, 86716]  # 260147 / 3, leftover cents rotate
    assert sum(vac.values()) == 260147
    expected = {ln.person_id: ln.total_cents for ln in plan.lines}
    cat.save_withdrawal(conn, t, ids["Rent"], plan.weights)
    assert shares(conn, t) == expected
    assert sum(expected.values()) == JULY


def test_refund_rent_signs(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-02", 3100)
    plan = cat.rent_plan(conn, cat.get_transaction(conn, t), None, [ids["House"]])
    assert sum(ln.total_cents for ln in plan.lines) == -3100
    cat.save_refund(conn, t, ids["Rent"], plan.weights)
    assert shares(conn, t) == {ln.person_id: ln.total_cents for ln in plan.lines if ln.total_cents}


def test_full_month_plan_is_default(house):
    conn, ids = house
    fill_july(conn, ids)
    t = add_tx(conn, "2026-07-01", -JULY)
    plan = cat.rent_plan(conn, cat.get_transaction(conn, t))
    assert plan.error is None and not plan.needs_vacant_choice
    cat.save_withdrawal(conn, t, ids["Rent"], plan.weights)
    assert splits.get_split_source(conn, t) == splits.DEFAULT
    assert sum(shares(conn, t).values()) == JULY


def test_bad_percents_no_plan(house):
    conn, ids = house
    with conn:
        conn.execute("UPDATE rooms SET percent = 39.999 WHERE id = ?", (ids["Front"],))
    t = add_tx(conn, "2026-07-01", -JULY)
    plan = cat.rent_plan(conn, cat.get_transaction(conn, t), None, [ids["House"]])
    assert plan.lines == [] and plan.weights == []
    assert plan.error.startswith("Rent can't be split by rooms")
    with pytest.raises(ValueError):
        cat.recalculate_rent(conn, t)


def test_review_filter_rent(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-01", -JULY, ids["Rent"])
    splits.apply_default_split(conn, [t])
    assert [r["id"] for r in cat.list_transactions(conn, status=cat.REVIEW)] == [t]
    cat.recalculate_rent(conn, t, [ids["House"]])
    assert cat.list_transactions(conn, status=cat.REVIEW) == []


def test_recalculate_compares_and_replaces(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-01", -JULY, ids["Rent"])
    splits.set_transaction_split(conn, t, [(ids["Ann"], 1), (ids["Bob"], 1)])
    fill_july(conn, ids)
    plan = cat.rent_plan(conn, cat.get_transaction(conn, t))
    rows = {n: (o, nw) for n, o, nw in cat.recalc_comparison(conn, t, plan)}
    assert rows["Ann"][1] == 629121
    assert rows["Ann"][0] + rows["Bob"][0] == JULY
    assert rows["Cy"][0] is None and rows["Moana"][0] is None
    assert shares(conn, t) == {ids["Ann"]: rows["Ann"][0], ids["Bob"]: rows["Bob"][0]}  # unchanged
    cat.recalculate_rent(conn, t)
    assert shares(conn, t) == {ln.person_id: ln.total_cents for ln in plan.lines}
    assert splits.get_split_source(conn, t) == splits.DEFAULT


def test_recalculate_needs_vacant_choice(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-01", -JULY, ids["Rent"])
    splits.set_transaction_split(conn, t, [(ids["Ann"], 1)])
    with pytest.raises(ValueError, match="vacant share"):
        cat.recalculate_rent(conn, t)
    assert shares(conn, t) == {ids["Ann"]: JULY}
    cat.recalculate_rent(conn, t, [ids["Bob"]])
    assert shares(conn, t)[ids["Bob"]] == 524923 + 260147


def test_recalculate_only_rent(house):
    conn, ids = house
    t = add_tx(conn, "2026-07-01", -1000, db.get_purpose_id(conn, "Water"))
    with pytest.raises(ValueError):
        cat.recalculate_rent(conn, t)


# --- page (AppTest) -------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    ids = build_house(conn)
    ids["july"] = add_tx(conn, "2026-07-01", -JULY)
    conn.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path, ids


def run_page():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "pages" / "1_Categorize.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def frames(at):
    return [df.value for df in at.dataframe]


def test_page_vacant_share_must_be_assigned(app_db):
    path, ids = app_db
    at = run_page()
    t = ids["july"]
    k = f"cat_{t}"
    at.selectbox(key=f"{k}_purpose").set_value(ids["Rent"]).run()
    assert not at.exception, at.exception
    assert any("Attic vacant 11–31 Jul (420 of 3,100)" in w.value for w in at.warning)
    table = frames(at)[-1]
    assert list(table["Person"]) == ["Ann", "Bob", "Cy", "Moana", "Vacant", "Vacant"]
    assert list(table["Share"])[:4] == ["$6,291.21", "$5,249.23", "$1,014.71", "$571.41"]
    assert at.button(key=f"{k}_save").disabled
    at.checkbox(key=f"{k}_remember").uncheck().run()

    at.multiselect(key=f"{k}_vacant_2026-07").set_value([ids["House"]]).run()
    assert not at.exception, at.exception
    assert not at.button(key=f"{k}_save").disabled
    result = frames(at)[-1]
    house_row = result[result["Person"] == "House"].iloc[0]
    assert house_row["Vacant share"] == "$2,601.47" and house_row["Total"] == "$2,601.47"
    assert any("Total $15,728.03 = the full payment" in c.value for c in at.caption)
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception

    conn = db.connect(path)
    s = shares(conn, t)
    assert s[ids["House"]] == 260147 and sum(s.values()) == JULY
    assert not splits.needs_room_review(conn, t)
    conn.close()


def test_page_bad_percents_no_split(app_db):
    path, ids = app_db
    conn = db.connect(path)
    with conn:
        conn.execute("UPDATE rooms SET percent = 39.999 WHERE id = ?", (ids["Front"],))
    conn.close()
    at = run_page()
    k = f"cat_{ids['july']}"
    at.selectbox(key=f"{k}_purpose").set_value(ids["Rent"]).run()
    assert not at.exception, at.exception
    assert any("Percents total 99.999%" in e.value for e in at.error)
    assert at.button(key=f"{k}_save").disabled
    assert not [m for m in at.multiselect if m.key == f"{k}_vacant_2026-07"]


def test_page_review_and_recalculate(app_db):
    path, ids = app_db
    t = ids["july"]
    conn = db.connect(path)
    with conn:
        conn.execute("UPDATE transactions SET purpose_id = ?, category_source = 'manual'"
                     " WHERE id = ?", (ids["Rent"], t))
    splits.apply_default_split(conn, [t])  # vacant: no split, waits for review
    conn.close()

    at = run_page()
    at.radio(key="cat_status").set_value("Needs review").run()
    assert at.selectbox(key="cat_selected").value == t
    assert len(at.selectbox(key="cat_selected").options) == 1
    k = f"cat_{t}"
    at.multiselect(key=f"{k}_vacant_2026-07").set_value([ids["Ann"], ids["Bob"]]).run()
    at.checkbox(key=f"{k}_remember").uncheck().run()
    at.button(key=f"{k}_save").click().run()
    assert not at.exception, at.exception
    assert any("No splits are waiting for review" in i.value for i in at.info)

    # Fill July, then recalculate from rooms: old vs new before saving.
    conn = db.connect(path)
    before = shares(conn, t)
    fill_july(conn, ids)
    conn.close()
    at.radio(key="cat_status").set_value("All").run()
    at.selectbox(key="cat_selected").set_value(t).run()
    at.button(key=f"{k}_recalc").click().run()
    assert not at.exception, at.exception
    comp = frames(at)[-1]
    assert list(comp.columns) == ["Person", "Old share", "New share", "Change"]
    ann = comp[comp["Person"] == "Ann"].iloc[0]
    assert ann["Old share"] == f"${before[ids['Ann']] // 100:,}.{before[ids['Ann']] % 100:02d}"
    conn = db.connect(path)
    assert shares(conn, t) == before  # nothing saved until confirmed
    conn.close()
    at.button(key=f"{k}_recalc_save").click().run()
    assert not at.exception, at.exception
    conn = db.connect(path)
    after = shares(conn, t)
    assert after != before and sum(after.values()) == JULY
    assert splits.get_split_source(conn, t) == splits.DEFAULT
    conn.close()


def test_page_recalculate_cancel(app_db):
    path, ids = app_db
    t = ids["july"]
    conn = db.connect(path)
    with conn:
        conn.execute("UPDATE transactions SET purpose_id = ?, category_source = 'manual'"
                     " WHERE id = ?", (ids["Rent"], t))
    splits.set_transaction_split(conn, t, [(ids["Ann"], 1)])
    conn.close()
    at = run_page()
    at.radio(key="cat_status").set_value("All").run()
    at.selectbox(key="cat_selected").set_value(t).run()
    k = f"cat_{t}"
    at.button(key=f"{k}_recalc").click().run()
    assert at.button(key=f"{k}_recalc_save").disabled  # vacant share not yet assigned
    at.button(key=f"{k}_recalc_cancel").click().run()
    assert not at.exception, at.exception
    assert not [b for b in at.button if b.key == f"{k}_recalc_save"]
    conn = db.connect(path)
    assert shares(conn, t) == {ids["Ann"]: JULY}
    conn.close()
