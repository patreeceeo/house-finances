"""Rent split by rooms (docs/rooms-spec.md, "How a rent payment is split")."""

from decimal import Decimal

import pytest

from housefin import categorize, db, rooms, rules, splits


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


_n = 0


def _txn(conn, amount_cents, date="2026-07-03", purpose_id=None, applies_month=None,
         description="X"):
    global _n
    _n += 1
    with conn:
        cur = conn.execute(
            "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
            " purpose_id, applies_month) VALUES (?, ?, '12:00:00', ?, ?, ?, ?, ?)",
            (f"rs{_n}", date, amount_cents,
             "Withdrawal" if amount_cents < 0 else "Deposit", description,
             purpose_id, applies_month),
        )
    return cur.lastrowid


@pytest.fixture
def rent(conn):
    """A purpose split by rooms (set directly: seeding Rent is another bead)."""
    pid = db.add_purpose(conn, "Room rent")
    with conn:
        conn.execute("UPDATE purposes SET split_by_rooms = 1 WHERE id = ?", (pid,))
    return pid


@pytest.fixture
def house(conn):
    """The spec's worked example: July 2026."""
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
    return ids


def _shares(conn, txn_id):
    return {r["person_id"]: r["share_cents"] for r in splits.get_transaction_split(conn, txn_id)}


def _fill_july(conn, ids):
    """Make July 2026 fully occupied."""
    rooms.add_assignment(conn, ids["Attic"], ids["Cy"], "2026-07-11", "2026-07-31")
    nook = rooms.list_assignments(conn, ids["Nook"])[0]
    rooms.update_assignment(conn, nook["id"], ids["Nook"], ids["Moana"], "2026-07-01")


def test_worked_example_to_the_cent(conn, house):
    rs = splits.room_split(conn, "2026-07", 1572803)
    assert rs.error is None
    assert rs.days_in_month == 31
    assert rs.total_weight == Decimal(3100)
    by_name = {p.name: p for p in rs.people}
    assert {n: (p.room, p.nights, p.weight, p.share_cents) for n, p in by_name.items()} == {
        "Ann": ("Front", 31, Decimal(1240), 629121),
        "Bob": ("Back", 31, Decimal("1034.625"), 524923),
        "Cy": ("Attic", 10, Decimal(200), 101471),
        "Moana": ("Nook", 17, Decimal("112.625"), 57141),
    }
    assert [(v.room, v.first, v.last, v.nights, v.weight) for v in rs.vacant] == [
        ("Attic", "2026-07-11", "2026-07-31", 21, Decimal(420)),
        ("Nook", "2026-07-01", "2026-07-14", 14, Decimal("92.75")),
    ]
    assert rs.people_cents == 1312656
    assert rs.vacant_cents == 260147
    assert sum(v.amount_cents for v in rs.vacant) == 260147
    assert rs.vacant_weight == Decimal("512.75")
    assert rs.people_cents + rs.vacant_cents == 1572803
    assert not rs.fully_occupied
    assert rs.warnings() == [
        "Attic vacant 11–31 Jul (420 of 3,100)",
        "Nook vacant 1–14 Jul (92.75 of 3,100)",
    ]


def test_people_shares_do_not_depend_on_rotation_beyond_one_cent(conn, house):
    for rotate in range(6):
        rs = splits.room_split(conn, "2026-07", 1572803, rotate=rotate)
        assert rs.people_cents + rs.vacant_cents == 1572803
        for p in rs.people:
            exact = Decimal(1572803) * p.weight / 3100
            assert int(exact) <= p.share_cents <= int(exact) + 1


def test_vacant_month_is_flagged_not_split(conn, house, rent):
    t = _txn(conn, -1572803, purpose_id=rent)
    assert splits.apply_default_split(conn, [t]) == 0
    assert _shares(conn, t) == {}
    assert splits.get_split_source(conn, t) is None
    assert splits.get_left_out(conn, t) == []
    assert splits.needs_room_review(conn, t)
    review = categorize.list_transactions(conn, status=categorize.REVIEW)
    assert t in [r["id"] for r in review]


def test_full_month_written_as_default_split(conn, house, rent):
    _fill_july(conn, house)
    t = _txn(conn, -1572803, purpose_id=rent)
    assert splits.apply_default_split(conn, [t]) == 1
    assert splits.get_split_source(conn, t) == splits.DEFAULT
    expected = {p.person_id: p.share_cents
                for p in splits.room_split_for_transaction(conn, t).people}
    assert _shares(conn, t) == expected
    assert sum(expected.values()) == 1572803
    weights = {r["person_id"]: r["weight"] for r in splits.get_transaction_split(conn, t)}
    assert weights == {house["Ann"]: 1240, house["Bob"]: 1034.625,
                       house["Cy"]: 620, house["Moana"]: 205.375}
    assert not splits.needs_room_review(conn, t)
    rs = splits.room_split_for_transaction(conn, t)
    assert rs.fully_occupied and rs.warnings() == []


def test_uses_applies_month(conn, house, rent):
    _fill_july(conn, house)
    # Dated in August (Attic empty from Aug since Cy's second stay ends 31 Jul),
    # but applies to July, which is full.
    t = _txn(conn, -100000, date="2026-08-01", purpose_id=rent, applies_month="2026-07")
    assert splits.apply_default_split(conn, [t]) == 1
    t2 = _txn(conn, -100000, date="2026-08-01", purpose_id=rent)
    assert splits.apply_default_split(conn, [t2]) == 0
    assert splits.needs_room_review(conn, t2)
    assert splits.room_split_for_transaction(conn, t2).warnings() == [
        "Attic vacant 1–31 Aug (620 of 3,100)",
    ]


def test_person_in_two_rooms_in_a_month(conn, house, rent):
    # Cy moves from the Attic to the Nook; Moana gone. Attic left vacant.
    nook = rooms.list_assignments(conn, house["Nook"])[0]
    rooms.delete_assignment(conn, nook["id"])
    rooms.add_assignment(conn, house["Nook"], house["Cy"], "2026-07-11")
    rs = splits.room_split(conn, "2026-07", 310000)
    cy = next(p for p in rs.people if p.name == "Cy")
    assert (cy.room, cy.nights, cy.weight) == ("Attic, Nook", 31, Decimal("200") + Decimal("6.625") * 21)


def test_bad_room_percents_flagged(conn, house, rent):
    _fill_july(conn, house)
    with conn:  # simulate stored percents that no longer total 100
        conn.execute("UPDATE rooms SET percent = 39.999 WHERE id = ?", (house["Front"],))
    rs = splits.room_split(conn, "2026-07", 1000)
    assert rs.error and rs.people == [] and not rs.fully_occupied
    assert rs.warnings()[0].startswith("Rent can't be split by rooms: Percents total 99.999%")
    t = _txn(conn, -1000, purpose_id=rent)
    assert splits.apply_default_split(conn, [t]) == 0
    assert splits.needs_room_review(conn, t)


def test_no_rooms_flagged(conn, rent):
    db.add_person(conn, "Ann")
    t = _txn(conn, -1000, purpose_id=rent)
    assert splits.apply_default_split(conn, [t]) == 0
    assert splits.needs_room_review(conn, t)
    assert "no rooms" in splits.room_split_for_transaction(conn, t).warnings()[0]


def test_default_split_replaced_by_flag_when_month_turns_vacant(conn, house, rent):
    _fill_july(conn, house)
    t = _txn(conn, -1572803, purpose_id=rent)
    splits.apply_default_split(conn, [t])
    assert _shares(conn, t)
    attic = [a for a in rooms.list_assignments(conn, house["Attic"])
             if a["start_date"] == "2026-07-11"][0]
    rooms.delete_assignment(conn, attic["id"])
    splits.apply_default_split(conn, [t])  # e.g. purpose re-set
    assert _shares(conn, t) == {}
    assert splits.needs_room_review(conn, t)


def test_manual_rent_split_kept_and_not_flagged(conn, house, rent):
    t = _txn(conn, -1000, purpose_id=rent)
    splits.set_transaction_split(conn, t, [(house["Ann"], 1), (house["Bob"], 1)])
    splits.apply_default_split(conn, [t])
    assert _shares(conn, t) == {house["Ann"]: 500, house["Bob"]: 500}
    assert not splits.needs_room_review(conn, t)


def test_rules_and_import_use_room_split(conn, house, rent):
    _fill_july(conn, house)  # July full; June has the Nook empty
    t_full = _txn(conn, -1572803, date="2026-07-03", description="RENT PAYMENT")
    t_vacant = _txn(conn, -1572803, date="2026-06-03", description="RENT PAYMENT")
    _, result = rules.create_rule(conn, "RENT PAYMENT", rules.WITHDRAWAL, purpose_id=rent)
    assert set(result.purpose_changed) == {t_vacant, t_full}
    assert splits.sync_after_rules(conn, result) == 1
    assert sum(_shares(conn, t_full).values()) == 1572803
    assert splits.get_split_source(conn, t_full) == splits.DEFAULT
    assert _shares(conn, t_vacant) == {}
    assert splits.needs_room_review(conn, t_vacant)
    assert splits.room_split_for_transaction(conn, t_vacant).warnings() == [
        "Nook vacant 1–30 Jun (198.75 of 3,000)",
    ]
