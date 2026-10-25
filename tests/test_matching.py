"""Balance matching: a person's items and the suggested groups.

All data is synthetic.
"""

import pytest

from helpers import add_resident

from housefin import balances, db, matching, splits
from housefin.matching import Item

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
    return {
        "ann": add_resident(conn, "Ann"),
        "bob": add_resident(conn, "Bob"),
        "rent": db.get_purpose_id(conn, "Rent"),
        "costco": db.get_purpose_id(conn, "Costco"),
        "transfer": db.get_transfer_purpose_id(conn),
    }


# --- pure: suggest_groups ------------------------------------------------------

_ids = iter(range(1, 1_000_000))


def paid(date, cents, tid=None):
    return Item(tid or next(_ids), "paid", date, date[:7], "Zelle", cents, cents)


def share(date, cents, tid=None, purpose="Rent"):
    return Item(tid or next(_ids), "share", date, date[:7], "Bill", cents, -cents * 2, purpose)


def shape(groups):
    """``[(months, paid, share, difference)]`` for compact assertions."""
    return [(g.months, g.paid_cents, g.share_cents, g.difference_cents) for g in groups]


def test_exact_month():
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), share("2026-01-20", 500), paid("2026-01-05", 1500),
        share("2026-02-02", 700), paid("2026-02-03", 700),
    ])
    assert shape(groups) == [
        (("2026-01",), 1500, 1500, 0),
        (("2026-02",), 700, 700, 0),
    ]
    # Items in date order.
    assert [i.date for i in groups[0].items] == ["2026-01-02", "2026-01-05", "2026-01-20"]


def test_two_deposits_for_one_month():
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), paid("2026-01-03", 600), paid("2026-01-15", 400),
        share("2026-02-02", 800), paid("2026-02-04", 800),
    ])
    assert shape(groups) == [
        (("2026-01",), 1000, 1000, 0),
        (("2026-02",), 800, 800, 0),
    ]


def test_one_deposit_covering_two_months():
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), share("2026-02-02", 1000), share("2026-03-02", 1000),
        paid("2026-01-01", 2000), paid("2026-03-03", 1000),
    ])
    assert shape(groups) == [
        (("2026-01", "2026-02"), 2000, 2000, 0),
        (("2026-03",), 1000, 1000, 0),
    ]


def test_next_month_joins_only_if_strictly_closer():
    # Overpaid by 300; Feb (1000) would leave it 700 short: not closer.
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), share("2026-02-02", 1000), paid("2026-01-03", 1300),
    ])
    assert shape(groups) == [
        (("2026-01",), 1300, 1000, 300),
        (("2026-02",), 0, 1000, -1000),
    ]
    # Exactly as far either way (|+500| vs |-500|): not strictly closer, stays out.
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), share("2026-02-02", 1000), paid("2026-01-03", 1500),
    ])
    assert [g.months for g in groups] == [("2026-01",), ("2026-02",)]


def test_short_month_merges_with_next():
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), paid("2026-01-03", 900),
        share("2026-02-02", 1000), paid("2026-02-03", 1100),
        share("2026-03-02", 1000), paid("2026-03-03", 1000),
    ])
    # Jan is 100 short, so it stays open and the Feb deposit joins it.
    assert shape(groups) == [
        (("2026-01", "2026-02"), 2000, 2000, 0),
        (("2026-03",), 1000, 1000, 0),
    ]


def test_paid_ahead():
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), paid("2026-01-03", 1000),
        paid("2026-02-03", 500), paid("2026-03-03", 250),
    ])
    assert shape(groups) == [
        (("2026-01",), 1000, 1000, 0),
        ((), 750, 0, 750),
    ]
    assert groups[1].paid_ahead and not groups[0].paid_ahead


def test_refund_shares():
    # A Feb refund (-200) makes Feb's shares 800, which the deposit pays exactly.
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), paid("2026-01-03", 1000),
        share("2026-02-02", 1000), share("2026-02-10", -200, purpose="Costco"),
        paid("2026-02-11", 800),
    ])
    assert shape(groups) == [
        (("2026-01",), 1000, 1000, 0),
        (("2026-02",), 800, 800, 0),
    ]


def test_withdrawal_paid_to_person_is_negative_deposit():
    # Jan is overpaid by 500 and closes. The house then pays Ann 500 back: that
    # payback opens Feb's group (-500 against 1000, short), so the next
    # deposit joins it.
    groups = matching.suggest_groups([
        share("2026-01-02", 1000), paid("2026-01-03", 1500),
        paid("2026-01-20", -500),
        share("2026-02-02", 1000), paid("2026-02-03", 1500),
    ])
    assert shape(groups) == [
        (("2026-01",), 1500, 1000, 500),
        (("2026-02",), 1000, 1000, 0),
    ]


def test_no_deposits():
    groups = matching.suggest_groups([
        share("2026-02-02", 700), share("2026-01-02", 1000), share("2026-01-09", 50),
    ])
    assert shape(groups) == [
        (("2026-01",), 0, 1050, -1050),
        (("2026-02",), 0, 700, -700),
    ]


def test_no_shares():
    groups = matching.suggest_groups([paid("2026-01-03", 1000), paid("2026-02-03", -200)])
    assert shape(groups) == [((), 800, 0, 800)]
    assert matching.suggest_groups([]) == []


def test_same_day_deposits_ordered_by_transaction_id():
    # The 1000 deposit (lower id) closes Jan; the 300 opens Feb, whichever was given first.
    items = [
        paid("2026-01-05", 300, tid=9), paid("2026-01-05", 1000, tid=8),
        share("2026-01-02", 1000, tid=1), share("2026-02-02", 300, tid=2),
    ]
    for given in (items, items[::-1]):
        groups = matching.suggest_groups(given)
        assert [[i.transaction_id for i in g.items] for g in groups] == [[1, 8], [9, 2]]


# The proposal's example: Moana, Jun - Oct 2026 (shares, then deposits).
MOANA_SHARES = [
    ("2026-06-02", 104437), ("2026-06-09", 6118), ("2026-06-14", 2375),
    ("2026-06-20", 5000), ("2026-06-29", 3809),
    ("2026-07-02", 99521), ("2026-07-07", 1897), ("2026-07-15", 2529),
    ("2026-07-16", 3175), ("2026-07-20", 5000), ("2026-07-24", 12502), ("2026-07-29", 3809),
    ("2026-08-03", 104277), ("2026-08-05", 1580), ("2026-08-10", 7174),
    ("2026-08-20", 5000), ("2026-08-26", 2723), ("2026-08-29", 4516),
    ("2026-09-02", 101684), ("2026-09-02", 2919), ("2026-09-04", 1083), ("2026-09-09", 4537),
    ("2026-09-10", 4275), ("2026-09-12", 378), ("2026-09-20", 5000), ("2026-09-22", -812),
    ("2026-09-30", 4516),
    ("2026-10-02", 102940), ("2026-10-08", 5261), ("2026-10-14", 1937), ("2026-10-20", 5000),
]
MOANA_PAID = [
    ("2026-07-03", 121739), ("2026-07-24", 12502), ("2026-08-04", 115931),
    ("2026-09-03", 125270), ("2026-09-29", 119064), ("2026-10-03", 102940),
]


def test_moana_example():
    items = [share(d, c, tid=i) for i, (d, c) in enumerate(MOANA_SHARES, 1)]
    items += [paid(d, c, tid=100 + i) for i, (d, c) in enumerate(MOANA_PAID, 1)]
    groups = matching.suggest_groups(items)
    assert [(g.months, g.difference_cents) for g in groups] == [
        (("2026-06",), 0),
        (("2026-07",), 0),
        (("2026-08",), 0),
        (("2026-09", "2026-10"), -16714),
    ]
    assert sum(g.difference_cents for g in groups) == -16714
    # Every item in exactly one group.
    keys = [i.key for g in groups for i in g.items]
    assert sorted(keys) == sorted(i.key for i in items)


# --- DB: person_items and the check -------------------------------------------


def test_person_items_through_latest_month_and_no_transfers(conn, house):
    h = house
    rent_jan = tx(conn, "2026-01-01", -2000, purpose=h["rent"],
                  weights=[(h["ann"], 1), (h["bob"], 1)], description="Rent Jan")
    dep_jan = tx(conn, "2026-01-03", 1000, person=h["ann"], description="Zelle Ann")
    # Dated February but applies to January.
    late = tx(conn, "2026-02-01", 50, person=h["ann"], applies_month="2026-01")
    refund = tx(conn, "2026-01-10", 400, purpose=h["costco"],
                weights=[(h["ann"], 1), (h["bob"], 1)], description="Refund")
    payback = tx(conn, "2026-01-12", -300, person=h["ann"], description="Paid to Ann")
    # Transfers left out.
    tx(conn, "2026-01-15", 99999, person=h["ann"], purpose=h["transfer"])
    # March is after the latest selected month.
    tx(conn, "2026-03-01", -2000, purpose=h["rent"], weights=[(h["ann"], 1), (h["bob"], 1)])
    tx(conn, "2026-03-02", 1000, person=h["ann"])

    items = matching.person_items(conn, h["ann"], ["2026-01", "2026-02"])
    assert [(i.transaction_id, i.side, i.month, i.cents) for i in items] == [
        (rent_jan, "share", "2026-01", 1000),
        (dep_jan, "paid", "2026-01", 1000),
        (refund, "share", "2026-01", -200),
        (payback, "paid", "2026-01", -300),
        (late, "paid", "2026-01", 50),
    ]
    rent = items[0]
    assert rent.purpose == "Rent" and rent.amount_cents == -2000
    assert rent.description == "Rent Jan" and rent.key == (rent_jan, "share")
    assert items[1].purpose is None and items[1].amount_cents == 1000

    # Only the latest selected month matters (all time through it).
    assert matching.person_items(conn, h["ann"], ["2026-02"]) == items
    assert len(matching.person_items(conn, h["ann"], ["2026-03"])) == 7
    assert matching.person_items(conn, h["ann"], []) == matching.person_items(conn, h["ann"])
    assert len(matching.person_items(conn, h["ann"])) == 7


def test_differences_sum_to_running_balance(conn, house):
    h = house
    for m in ("01", "02", "03"):
        tx(conn, f"2026-{m}-01", -3001, purpose=h["rent"], weights=[(h["ann"], 1), (h["bob"], 1)])
    tx(conn, "2026-01-02", 1500, person=h["ann"])
    tx(conn, "2026-02-02", 1400, person=h["ann"])
    tx(conn, "2026-02-20", 300, purpose=h["costco"], weights=[(h["ann"], 1), (h["bob"], 2)])
    tx(conn, "2026-02-21", -100, person=h["ann"])
    tx(conn, "2026-03-02", 5000, person=h["ann"])
    tx(conn, "2026-03-05", 5000, person=h["bob"], purpose=h["transfer"])

    for months in (None, ["2026-01"], ["2026-02"], ["2026-01", "2026-03"]):
        rows = {r.person_id: r for r in balances.balances(conn, months)}
        for pid in (h["ann"], h["bob"]):
            groups = matching.suggest_groups(matching.person_items(conn, pid, months))
            check = matching.check_groups(conn, pid, months, groups)
            assert check.ok and check.off_by_cents == 0
            assert check.groups_cents == check.running_cents == rows[pid].running_cents


def test_check_reports_mismatch(conn, house):
    h = house
    tx(conn, "2026-01-01", -2000, purpose=h["rent"], weights=[(h["ann"], 1), (h["bob"], 1)])
    tx(conn, "2026-01-02", 900, person=h["ann"])
    groups = matching.suggest_groups(matching.person_items(conn, h["ann"]))
    # Drop a group, as broken matching code might.
    check = matching.check_groups(conn, h["ann"], None, groups[1:])
    assert not check.ok
    assert check.running_cents == -100 and check.groups_cents == 0
    assert check.off_by_cents == 100


# --- kept groups ---------------------------------------------------------------


def rent(conn, h, date, cents=-2000, ann=1, bob=1):
    """A Rent withdrawal split Ann:Bob by weight."""
    weights = [(p, w) for p, w in ((h["ann"], ann), (h["bob"], bob)) if w]
    return tx(conn, date, cents, purpose=h["rent"], weights=weights)


def ann_groups(conn, h, months=None):
    groups = matching.groups_for(conn, h["ann"], months)
    check = matching.check_groups(conn, h["ann"], months, groups)
    assert check.ok, check  # the sum invariant holds after every edit
    return groups


def kshape(groups):
    """``[(kept, months, paid, share)]``."""
    return [(g.kept, g.months, g.paid_cents, g.share_cents) for g in groups]


def stored_groups(conn):
    return conn.execute("SELECT COUNT(*) FROM match_groups").fetchone()[0]


@pytest.fixture
def three_months(conn, house):
    """Ann: Jan paid exactly, Feb short by 100, Mar paid; Jan/Feb/Mar rent."""
    h = house
    ids = {
        "rent_jan": rent(conn, h, "2026-01-01"),
        "dep_jan": tx(conn, "2026-01-03", 1000, person=h["ann"]),
        "rent_feb": rent(conn, h, "2026-02-01"),
        "dep_feb": tx(conn, "2026-02-03", 900, person=h["ann"]),
        "rent_mar": rent(conn, h, "2026-03-01"),
        "dep_mar": tx(conn, "2026-03-03", 1100, person=h["ann"]),
    }
    return h, ids


def test_suggestions_only_until_kept(conn, three_months):
    h, ids = three_months
    groups = ann_groups(conn, h)
    assert kshape(groups) == [
        (False, ("2026-01",), 1000, 1000),
        (False, ("2026-02", "2026-03"), 2000, 2000),
    ]
    assert groups[0].key == f"s:{ids['rent_jan']}-share,{ids['dep_jan']}-paid"
    assert not groups[0].changed_since_kept and groups[0].id is None
    assert stored_groups(conn) == 0  # nothing is kept without a click


def test_keep(conn, three_months):
    h, ids = three_months
    groups = ann_groups(conn, h)
    gid = matching.keep(conn, h["ann"], None, groups[1].key)
    groups = ann_groups(conn, h)
    assert kshape(groups) == [
        (False, ("2026-01",), 1000, 1000),
        (True, ("2026-02", "2026-03"), 2000, 2000),
    ]
    kept = groups[1]
    assert kept.id == gid and kept.key == f"k:{gid}" and kept.note is None
    assert (kept.kept_paid_cents, kept.kept_share_cents) == (2000, 2000)
    assert not kept.changed_since_kept
    # A stale suggestion key is refused and changes nothing.
    with pytest.raises(matching.StaleEdit):
        matching.keep(conn, h["ann"], None, "s:999-paid")
    assert stored_groups(conn) == 1


def test_keep_all_balanced(conn, house):
    h = house
    rent(conn, h, "2026-01-01")
    tx(conn, "2026-01-03", 1000, person=h["ann"])
    rent(conn, h, "2026-02-01")
    tx(conn, "2026-02-03", 1300, person=h["ann"])  # 300 over, not balanced
    rent(conn, h, "2026-03-01")
    tx(conn, "2026-03-03", 1000, person=h["ann"])
    ids = matching.keep_all_balanced(conn, h["ann"], None)
    assert len(ids) == 2
    assert kshape(ann_groups(conn, h)) == [
        (True, ("2026-01",), 1000, 1000),
        (False, ("2026-02",), 1300, 1000),
        (True, ("2026-03",), 1000, 1000),
    ]
    # Nothing left to keep.
    assert matching.keep_all_balanced(conn, h["ann"], None) == []


def test_move_out_of_suggestion_keeps_both(conn, three_months):
    h, ids = three_months
    groups = ann_groups(conn, h)
    # Move the Feb deposit into the Jan suggestion.
    target = matching.move(
        conn, h["ann"], None, [(ids["dep_feb"], "paid")], groups[0].key
    )
    groups = ann_groups(conn, h)
    assert kshape(groups) == [
        (True, ("2026-01",), 1900, 1000),
        (True, ("2026-02", "2026-03"), 1100, 2000),
    ]
    assert groups[0].id == target
    assert (groups[0].kept_paid_cents, groups[1].kept_paid_cents) == (1900, 1100)


def test_move_to_new_group_and_empty_group_deleted(conn, three_months):
    h, ids = three_months
    jan = matching.keep(conn, h["ann"], None, ann_groups(conn, h)[0].key)
    # JSON-style keys from the UI.
    new = matching.move(conn, h["ann"], None, [[ids["rent_jan"], "share"]], matching.NEW)
    groups = ann_groups(conn, h)
    assert kshape(groups) == [
        (True, ("2026-01",), 0, 1000),
        (True, (), 1000, 0),
        (False, ("2026-02", "2026-03"), 2000, 2000),
    ]
    assert [g.id for g in groups[:2]] == [new, jan]
    # Moving the last item out deletes the emptied kept group.
    matching.move(conn, h["ann"], None, [(ids["dep_jan"], "paid")], f"k:{new}")
    groups = ann_groups(conn, h)
    assert [g.id for g in groups if g.kept] == [new]
    assert stored_groups(conn) == 1
    assert kshape(groups)[0] == (True, ("2026-01",), 1000, 1000)


def test_move_into_own_group_is_refused(conn, three_months):
    h, ids = three_months
    jan = ann_groups(conn, h)[0]
    # A suggestion stays a suggestion: nothing is kept.
    with pytest.raises(ValueError, match="already in that group"):
        matching.move(conn, h["ann"], None, [(ids["rent_jan"], "share")], jan.key)
    assert stored_groups(conn) == 0
    gid = matching.keep(conn, h["ann"], None, jan.key)
    with pytest.raises(ValueError, match="already in that group"):
        matching.move(
            conn, h["ann"], None,
            [(ids["rent_jan"], "share"), (ids["dep_jan"], "paid")], f"k:{gid}",
        )
    # Some items from elsewhere still move in.
    matching.move(
        conn, h["ann"], None, [(ids["rent_jan"], "share"), (ids["dep_feb"], "paid")], f"k:{gid}"
    )
    assert (ids["dep_feb"], "paid") in [i.key for i in ann_groups(conn, h)[0].items]


def test_move_refuses_stale_items(conn, three_months):
    h, ids = three_months
    key = ann_groups(conn, h)[0].key
    with pytest.raises(matching.StaleEdit):
        matching.move(conn, h["ann"], None, [(ids["rent_jan"], "paid")], key)
    with pytest.raises(matching.StaleEdit):
        matching.move(conn, h["ann"], None, [(ids["rent_jan"], "share")], "k:42")
    with pytest.raises(ValueError):
        matching.move(conn, h["ann"], None, [], key)
    # Items after the latest selected month aren't shown, so can't be moved.
    with pytest.raises(matching.StaleEdit):
        matching.move(conn, h["ann"], ["2026-01"], [(ids["dep_mar"], "paid")], key)
    assert stored_groups(conn) == 0


def test_split_by_month(conn, house):
    h = house
    r_jan = rent(conn, h, "2026-01-01")
    early = tx(conn, "2025-12-20", 300, person=h["ann"])  # before the group: first month
    d_jan = tx(conn, "2026-01-05", 500, person=h["ann"])
    r_mar = rent(conn, h, "2026-03-01")
    d_feb = tx(conn, "2026-02-10", 400, person=h["ann"])  # no Feb share: latest earlier, Jan
    d_mar = tx(conn, "2026-03-05", 800, person=h["ann"])
    gid = matching.move(
        conn, h["ann"], None,
        [(r_jan, "share"), (early, "paid"), (d_jan, "paid"), (r_mar, "share"),
         (d_feb, "paid"), (d_mar, "paid")],
        matching.NEW,
    )
    matching.set_note(conn, h["ann"], None, f"k:{gid}", "All of it")
    ids = matching.split_by_month(conn, h["ann"], None, f"k:{gid}")
    assert ids[0] == gid and len(ids) == 2
    groups = ann_groups(conn, h)
    assert [(g.id, g.months, sorted(i.transaction_id for i in g.items), g.note)
            for g in groups] == [
        (ids[0], ("2026-01",), sorted([r_jan, early, d_jan, d_feb]), "All of it"),
        (ids[1], ("2026-03",), sorted([r_mar, d_mar]), None),
    ]
    assert not any(g.changed_since_kept for g in groups)
    # A suggestion can be split too (it is kept as it goes); paid ahead can't.
    tx(conn, "2026-04-01", 50, person=h["ann"])
    ahead = ann_groups(conn, h)[-1]
    assert ahead.paid_ahead and not ahead.kept
    with pytest.raises(ValueError):
        matching.split_by_month(conn, h["ann"], None, ahead.key)


def test_split_suggestion(conn, house):
    h = house
    rent(conn, h, "2026-01-01")
    rent(conn, h, "2026-02-01")
    tx(conn, "2026-01-02", 2000, person=h["ann"])
    (group,) = ann_groups(conn, h)
    assert group.months == ("2026-01", "2026-02")
    matching.split_by_month(conn, h["ann"], None, group.key)
    assert kshape(ann_groups(conn, h)) == [
        (True, ("2026-01",), 2000, 1000),
        (True, ("2026-02",), 0, 1000),
    ]


def test_merge(conn, three_months):
    h, ids = three_months
    first, second = ann_groups(conn, h)
    matching.set_note(conn, h["ann"], None, second.key, "Feb short")
    second = ann_groups(conn, h)[1]
    gid = matching.merge(conn, h["ann"], None, first.key, second.key)
    (group,) = ann_groups(conn, h)
    assert group.id == gid and group.note == "Feb short"
    assert (group.months, group.paid_cents, group.share_cents) == (
        ("2026-01", "2026-02", "2026-03"), 3000, 3000)
    assert (group.kept_paid_cents, group.kept_share_cents) == (3000, 3000)
    assert stored_groups(conn) == 1
    with pytest.raises(ValueError):
        matching.merge(conn, h["ann"], None, group.key, group.key)


def test_set_note(conn, three_months):
    h, ids = three_months
    gid = matching.set_note(conn, h["ann"], None, ann_groups(conn, h)[0].key,
                            "  Internet posted late.  ")
    group = ann_groups(conn, h)[0]
    assert group.kept and group.id == gid and group.note == "Internet posted late."
    matching.set_note(conn, h["ann"], None, group.key, "  ")
    assert ann_groups(conn, h)[0].note is None


def test_unkeep(conn, three_months):
    h, ids = three_months
    before = kshape(ann_groups(conn, h))
    gid = matching.keep(conn, h["ann"], None, ann_groups(conn, h)[1].key)
    matching.unkeep(conn, h["ann"], None, f"k:{gid}")
    assert kshape(ann_groups(conn, h)) == before
    assert stored_groups(conn) == 0
    with pytest.raises(ValueError):
        matching.unkeep(conn, h["ann"], None, ann_groups(conn, h)[0].key)
    with pytest.raises(matching.StaleEdit):
        matching.unkeep(conn, h["ann"], None, f"k:{gid}")


def test_kept_groups_survive_new_import(conn, three_months):
    h, ids = three_months
    matching.keep_all_balanced(conn, h["ann"], None)
    kept_before = [g for g in ann_groups(conn, h) if g.kept]
    rent(conn, h, "2026-04-01")
    tx(conn, "2026-04-02", 1000, person=h["ann"])
    tx(conn, "2026-01-15", 50, person=h["ann"])  # even one dated inside a kept group
    groups = ann_groups(conn, h)
    assert [g for g in groups if g.kept] == kept_before
    assert kshape([g for g in groups if not g.kept]) == [
        (False, ("2026-04",), 1050, 1000),
    ]


def test_changed_since_kept_after_split_edit(conn, three_months):
    h, ids = three_months
    matching.keep(conn, h["ann"], None, ann_groups(conn, h)[0].key)
    splits.set_transaction_split(conn, ids["rent_jan"], [(h["ann"], 3), (h["bob"], 1)])
    group = ann_groups(conn, h)[0]
    assert group.changed_since_kept
    assert (group.kept_paid_cents, group.kept_share_cents) == (1000, 1000)  # old
    assert (group.paid_cents, group.share_cents) == (1000, 1500)  # new
    # Keeping it again records the new figures.
    matching.keep(conn, h["ann"], None, group.key)
    group = ann_groups(conn, h)[0]
    assert not group.changed_since_kept and group.kept_share_cents == 1500


def test_deleting_transactions_deletes_emptied_kept_group(conn, three_months):
    h, ids = three_months
    gid = matching.keep(conn, h["ann"], None, ann_groups(conn, h)[0].key)
    conn.execute("DELETE FROM transactions WHERE id = ?", (ids["dep_jan"],))
    conn.commit()
    group = ann_groups(conn, h)[0]
    assert group.id == gid and group.changed_since_kept and group.paid_cents == 0
    conn.execute("DELETE FROM transactions WHERE id = ?", (ids["rent_jan"],))
    conn.commit()
    groups = ann_groups(conn, h)
    assert not any(g.kept for g in groups)
    assert stored_groups(conn) == 0


def test_retagged_transaction_leaves_group(conn, three_months):
    h, ids = three_months
    matching.keep(conn, h["ann"], None, ann_groups(conn, h)[0].key)
    conn.execute("UPDATE transactions SET person_id = ? WHERE id = ?", (h["bob"], ids["dep_jan"]))
    conn.commit()
    group = ann_groups(conn, h)[0]
    assert group.kept and [i.transaction_id for i in group.items] == [ids["rent_jan"]]
    assert group.changed_since_kept and group.paid_cents == 0
    # It now counts for Bob, in his suggestions.
    bob = matching.groups_for(conn, h["bob"])
    assert ids["dep_jan"] in [i.transaction_id for g in bob for i in g.items]
    assert matching.check_groups(conn, h["bob"], None, bob).ok
    # Left out of the split: Ann's share leaves too, emptying (deleting) the group.
    splits.set_transaction_split(conn, ids["rent_jan"], [(h["bob"], 1)])
    assert not any(g.kept for g in ann_groups(conn, h))
    assert stored_groups(conn) == 0


def test_kept_group_past_selected_range(conn, three_months):
    h, ids = three_months
    first, second = ann_groups(conn, h)
    gid = matching.merge(conn, h["ann"], None, first.key, second.key)
    # Through Feb: only the group's Jan and Feb items show and count.
    (group,) = ann_groups(conn, h, ["2026-02"])
    assert group.id == gid and group.months == ("2026-01", "2026-02")
    assert (group.paid_cents, group.share_cents) == (1900, 2000)
    assert [i.transaction_id for i in group.later_items] == [ids["rent_mar"], ids["dep_mar"]]
    assert not group.changed_since_kept
    # Through Dec 2025: nothing shown, but the group is not deleted.
    assert ann_groups(conn, h, ["2025-12"]) == []
    assert stored_groups(conn) == 1
