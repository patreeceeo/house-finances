import datetime as dt

import pytest

from housefin import dates, db


def test_month_arithmetic():
    assert dates.add_months("2026-12", 1) == "2027-01"
    assert dates.add_months("2026-01", -1) == "2025-12"
    assert dates.month_span("2026-11", "2027-02") == ["2026-11", "2026-12", "2027-01", "2027-02"]
    assert dates.month_span("2026-05", "2026-04") == []
    assert dates.current_month(dt.date(2026, 10, 20)) == "2026-10"


def test_is_month():
    assert dates.is_month("2026-10")
    for bad in ("2026-13", "2026-1", "Oct 2026", "", None):
        assert not dates.is_month(bad)


def test_month_bounds():
    assert dates.month_bounds("2026-07") == ("2026-07-01", "2026-07-31")
    assert dates.month_bounds("2028-02") == ("2028-02-01", "2028-02-29")
    assert dates.month_end("2026-09") == "2026-09-30"
    with pytest.raises(ValueError):
        dates.month_bounds("2026-7")


def test_nights_and_shift():
    assert dates.nights("2026-07-01", "2026-07-31") == 31
    assert dates.nights("2026-07-10", "2026-07-10") == 1
    assert dates.shift("2026-12-31", 1) == "2027-01-01"
    assert dates.shift("2026-03-01", -1) == "2026-02-28"


def test_month_of():
    assert dates.month_of({"applies_month": None, "date": "2026-08-02"}) == "2026-08"
    assert dates.month_of({"applies_month": "2026-10", "date": "2026-09-30"}) == "2026-10"


def _add_tx(conn, n, date, applies_month=None):
    conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description, applies_month)"
        " VALUES (?, ?, '00:00:00', -100, 'Withdrawal', 'X', ?)",
        (f"fp{n}", date, applies_month),
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    _add_tx(c, 1, "2026-01-15")
    _add_tx(c, 2, "2026-01-31", applies_month="2026-02")  # counts toward Feb
    _add_tx(c, 3, "2026-03-02")
    _add_tx(c, 4, "2026-03-20")
    c.commit()
    return c


def test_months_present_uses_applies_month(conn):
    assert dates.months_present(conn) == ["2026-03", "2026-02", "2026-01"]


def test_months_present_empty():
    assert dates.months_present(db.connect(":memory:")) == []


def test_month_filter_sql(conn):
    where, params = dates.month_filter_sql(None)
    assert (where, params) == ("1", [])
    assert dates.month_filter_sql([]) == ("1", [])

    where, params = dates.month_filter_sql(["2026-02", "2026-03"])
    ids = [r[0] for r in conn.execute(f"SELECT id FROM transactions WHERE {where} ORDER BY id", params)]
    assert ids == [2, 3, 4]

    where, params = dates.month_filter_sql(["2026-01"], alias="t")
    ids = [r[0] for r in conn.execute(f"SELECT t.id FROM transactions t WHERE {where}", params)]
    assert ids == [1]
