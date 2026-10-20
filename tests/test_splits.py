import pytest

from housefin import db, splits
from housefin.splits import compute_shares


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


_n = 0


def _txn(conn, amount_cents, date="2026-10-05", purpose_id=None, applies_month=None):
    global _n
    _n += 1
    with conn:
        cur = conn.execute(
            "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
            " purpose_id, applies_month) VALUES (?, ?, '12:00:00', ?, ?, 'X', ?, ?)",
            (
                f"fp{_n}",
                date,
                amount_cents,
                "Withdrawal" if amount_cents < 0 else "Deposit",
                purpose_id,
                applies_month,
            ),
        )
    return cur.lastrowid


def _shares(conn, txn_id):
    return {r["person_id"]: r["share_cents"] for r in splits.get_transaction_split(conn, txn_id)}


# --- compute_shares ---------------------------------------------------------------


def test_rent_example():
    assert compute_shares(400000, [(1, 35), (2, 35), (3, 30)]) == [
        (1, 140000),
        (2, 140000),
        (3, 120000),
    ]


def test_leftover_cent_goes_to_largest_weight():
    # 100 cents at 1:1:1:1:1:2 -> 14.29 x5 and 28.57; rounded they sum to 99,
    # so the leftover cent goes to the weight-2 person.
    ws = [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 2)]
    assert compute_shares(100, ws) == [(1, 14), (2, 14), (3, 14), (4, 14), (5, 14), (6, 30)]


def test_tie_for_largest_weight_goes_to_lowest_person_id():
    assert compute_shares(100, [(7, 1), (3, 1), (5, 1)]) == [(7, 33), (3, 34), (5, 33)]


def test_negative_leftover_taken_from_largest():
    # 2 cents over 3 equal people: each rounds to 1, so the largest gives one back.
    assert compute_shares(2, [(1, 1), (2, 1), (3, 1)]) == [(1, 0), (2, 1), (3, 1)]


def test_sign_follows_amount_and_sums_exactly():
    shares = compute_shares(-1001, [(1, 1), (2, 1), (3, 1)])
    assert shares == [(1, -333), (2, -334), (3, -334)]
    for amount in (1, 7, 99, 12345, -12345, 0):
        for ws in ([(1, 3), (2, 7)], [(1, 0.1), (2, 0.2), (3, 0.3)], [(1, 1)]):
            assert sum(s for _, s in compute_shares(amount, ws)) == amount


def test_invalid_weights():
    with pytest.raises(ValueError):
        compute_shares(100, [])
    with pytest.raises(ValueError):
        compute_shares(100, [(1, 0)])
    with pytest.raises(ValueError):
        compute_shares(100, [(1, 1), (1, 2)])


# --- defaults ---------------------------------------------------------------------


def test_even_split_fallback_respects_active_months(conn):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob", active_from="2026-09")
    c = db.add_person(conn, "Cara", active_until="2026-09")
    rent = db.get_purpose_id(conn, "Rent")
    assert splits.default_split_for(conn, rent, "2026-08") == [(a, 1.0), (c, 1.0)]
    assert splits.default_split_for(conn, rent, "2026-09") == [(a, 1.0), (b, 1.0), (c, 1.0)]
    assert splits.default_split_for(conn, rent, "2026-10") == [(a, 1.0), (b, 1.0)]


def test_default_drops_moved_out_people_others_keep_weights(conn):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob")
    c = db.add_person(conn, "Cara", active_until="2026-09")
    house = db.get_house_id(conn)
    rent = db.get_purpose_id(conn, "Rent")
    splits.set_purpose_split(conn, rent, [(a, 35), (b, 35), (c, 30), (house, 5)])
    assert dict(splits.default_split_for(conn, rent, "2026-09")) == {a: 35, b: 35, c: 30, house: 5}
    assert dict(splits.default_split_for(conn, rent, "2026-10")) == {a: 35, b: 35, house: 5}


def test_default_with_nobody_active_falls_back_to_even(conn):
    a = db.add_person(conn, "Alice")
    c = db.add_person(conn, "Cara", active_until="2026-09")
    water = db.get_purpose_id(conn, "Water")
    splits.set_purpose_split(conn, water, [(c, 1)])
    assert splits.default_split_for(conn, water, "2026-10") == [(a, 1.0)]


def test_transfer_has_no_default(conn):
    db.add_person(conn, "Alice")
    assert splits.default_split_for(conn, db.get_transfer_purpose_id(conn), "2026-10") == []


# --- transaction splits ----------------------------------------------------------


def test_set_split_and_remember_for_purpose(conn):
    m, d, p = (db.add_person(conn, n) for n in ("Moana", "David", "Patrick"))
    rent = db.get_purpose_id(conn, "Rent")
    t1 = _txn(conn, -400000, purpose_id=rent)
    splits.set_transaction_split(conn, t1, [(m, 35), (d, 35), (p, 30)], remember_for_purpose=True)
    assert _shares(conn, t1) == {m: 140000, d: 140000, p: 120000}
    assert dict(splits.get_purpose_split(conn, rent)) == {m: 35, d: 35, p: 30}

    # Next rent gets the same weights.
    t2 = _txn(conn, -400000, date="2026-11-01", purpose_id=rent)
    assert splits.apply_default_split(conn, [t2]) == 1
    assert _shares(conn, t2) == {m: 140000, d: 140000, p: 120000}

    # Changing the default does not touch past withdrawals.
    t3 = _txn(conn, -400000, date="2026-12-01", purpose_id=rent)
    splits.set_transaction_split(conn, t3, [(m, 1), (d, 1), (p, 0)], remember_for_purpose=True)
    assert _shares(conn, t3) == {m: 200000, d: 200000}
    assert _shares(conn, t1) == {m: 140000, d: 140000, p: 120000}
    assert dict(splits.get_purpose_split(conn, rent)) == {m: 1, d: 1}


def test_set_split_without_remember_leaves_default(conn):
    a = db.add_person(conn, "Alice")
    rent = db.get_purpose_id(conn, "Rent")
    t = _txn(conn, -1000, purpose_id=rent)
    splits.set_transaction_split(conn, t, [(a, 1)])
    assert splits.get_purpose_split(conn, rent) == []


def test_refund_produces_negative_shares(conn):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob")
    costco = db.get_purpose_id(conn, "Costco")
    refund = _txn(conn, 2501, purpose_id=costco)
    splits.apply_default_split(conn, [refund])
    shares = _shares(conn, refund)
    # 12.505 each rounds to 12.51 twice (one cent too many); the tie for
    # largest weight goes to the lowest id, which gives the cent back.
    assert shares == {a: -1250, b: -1251}
    assert sum(shares.values()) == -2501


def test_apply_default_uses_applies_month(conn):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob", active_from="2026-11")
    rent = db.get_purpose_id(conn, "Rent")
    t = _txn(conn, -1000, date="2026-10-30", purpose_id=rent, applies_month="2026-11")
    splits.apply_default_split(conn, [t])
    assert _shares(conn, t) == {a: 500, b: 500}


def test_apply_default_skips_manual_and_unpurposed_and_clears_transfer(conn):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob")
    rent = db.get_purpose_id(conn, "Rent")
    manual = _txn(conn, -1000, purpose_id=rent)
    splits.set_transaction_split(conn, manual, [(a, 1)])
    none = _txn(conn, -1000)
    transfer = _txn(conn, -1000, purpose_id=db.get_transfer_purpose_id(conn))
    with conn:
        conn.execute(
            "INSERT INTO splits VALUES (?, ?, 1, 1000)", (transfer, a)
        )  # stale split from a previous purpose
    assert splits.apply_default_split(conn, [manual, none, transfer]) == 0
    assert _shares(conn, manual) == {a: 1000}
    assert _shares(conn, none) == {}
    assert _shares(conn, transfer) == {}
    # replace=True recomputes from the default (e.g. after a purpose change).
    assert splits.apply_default_split(conn, [manual], replace=True) == 1
    assert _shares(conn, manual) == {a: 500, b: 500}


def test_house_can_be_in_split(conn):
    a = db.add_person(conn, "Alice")
    house = db.get_house_id(conn)
    other = db.get_purpose_id(conn, "Other")
    t = _txn(conn, -300, purpose_id=other)
    splits.set_transaction_split(conn, t, [(a, 2), (house, 1)])
    assert _shares(conn, t) == {a: 200, house: 100}


# --- totals --------------------------------------------------------------------------


def test_share_totals(conn):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob")
    rent = db.get_purpose_id(conn, "Rent")
    transfer = db.get_transfer_purpose_id(conn)
    t_sep = _txn(conn, -1000, date="2026-09-03", purpose_id=rent)
    t_oct = _txn(conn, -2000, date="2026-10-03", purpose_id=rent)
    t_nov = _txn(conn, -4000, date="2026-10-31", purpose_id=rent, applies_month="2026-11")
    refund = _txn(conn, 600, date="2026-10-10", purpose_id=rent)
    splits.apply_default_split(conn, [t_sep, t_oct, t_nov, refund])
    t_tr = _txn(conn, -9999, purpose_id=transfer)
    with conn:  # even if a Transfer somehow has split rows, totals ignore it
        conn.execute("INSERT INTO splits VALUES (?, ?, 1, 9999)", (t_tr, a))

    assert splits.share_totals(conn) == {a: 3200, b: 3200}
    assert splits.share_totals(conn, ["2026-10"]) == {a: 700, b: 700}
    assert splits.share_totals(conn, ["2026-09", "2026-11"]) == {a: 2500, b: 2500}
    assert splits.share_totals(conn, through="2026-10") == {a: 1200, b: 1200}
    assert splits.share_totals(conn, []) == {}


def test_to_percents_sums_to_exactly_100():
    assert splits.to_percents([(1, 3), (2, 1)]) == {1: 75.0, 2: 25.0}
    thirds = splits.to_percents([(1, 1), (2, 1), (3, 1)])
    assert sorted(thirds.values()) == [33.33, 33.33, 33.34]
    assert round(sum(thirds.values()), 2) == 100
    assert splits.to_percents([(1, 0), (2, 2)]) == {2: 100.0}
    assert splits.to_percents([]) == {}


def test_percent_total_error():
    assert splits.percent_total_error([33.33, 33.33, 33.34]) is None
    assert "5% is still unassigned" in splits.percent_total_error([60, 35])
    assert "take off 10%" in splits.percent_total_error([60, 50])


# --- sync_after_rules -------------------------------------------------------------


def _set_purpose(conn, txn_id, purpose_id):
    with conn:
        conn.execute("UPDATE transactions SET purpose_id = ? WHERE id = ?", (purpose_id, txn_id))


def test_sync_after_rules_recomputes_clears_and_skips_excluded(conn):
    from housefin import rules

    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob")
    rent = db.get_purpose_id(conn, "Rent")
    costco = db.get_purpose_id(conn, "Costco")
    transfer = db.get_transfer_purpose_id(conn)
    splits.set_purpose_split(conn, rent, [(a, 75), (b, 25)])

    # Each starts with a stale split, then a "rule" changes its purpose.
    retagged = _txn(conn, -1000, purpose_id=costco)
    to_null = _txn(conn, -1000, purpose_id=costco)
    to_transfer = _txn(conn, -1000, purpose_id=costco)
    untouched = _txn(conn, -1000, purpose_id=costco)
    for t in (retagged, to_null, to_transfer, untouched):
        splits.set_transaction_split(conn, t, [(a, 1)])
    _set_purpose(conn, retagged, rent)
    _set_purpose(conn, to_null, None)
    _set_purpose(conn, to_transfer, transfer)

    result = rules.ApplyResult(
        updated=[retagged, to_null, to_transfer],
        purpose_changed=[retagged, to_null, to_transfer],
    )
    assert splits.sync_after_rules(conn, result) == 1
    assert _shares(conn, retagged) == {a: 750, b: 250}  # replaced, not kept
    assert _shares(conn, to_null) == {}
    assert _shares(conn, to_transfer) == {}
    assert _shares(conn, untouched) == {a: 1000}


def test_sync_after_rules_accepts_ids_and_empty(conn):
    a = db.add_person(conn, "Alice")
    rent = db.get_purpose_id(conn, "Rent")
    t = _txn(conn, -500, purpose_id=rent)
    assert splits.sync_after_rules(conn, []) == 0
    assert _shares(conn, t) == {}
    assert splits.sync_after_rules(conn, [t]) == 1
    assert _shares(conn, t) == {a: 500}


def test_sync_after_rules_joins_caller_transaction(conn):
    a = db.add_person(conn, "Alice")
    rent = db.get_purpose_id(conn, "Rent")
    t = _txn(conn, -500, purpose_id=rent)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            splits.sync_after_rules(conn, [t])
            assert _shares(conn, t) == {a: 500}
            raise RuntimeError("caller fails later")
    assert _shares(conn, t) == {}
