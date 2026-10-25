"""Reports: synthetic October 2026 fixture with hand-computed totals."""

import csv
import io
from fractions import Fraction
from pathlib import Path

import pytest

from housefin import db, reports, splits

ROOT = Path(__file__).resolve().parent.parent


def _txn(conn, n, amount, date="2026-10-05", *, person=None, purpose=None, applies_month=None, desc="X"):
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " person_id, purpose_id, applies_month) VALUES (?, ?, '12:00:00', ?, ?, ?, ?, ?, ?)",
        (f"fp{n}", date, amount, "Withdrawal" if amount < 0 else "Deposit", desc,
         person, purpose, applies_month),
    )
    conn.commit()
    return cur.lastrowid


def _seed(conn):
    """People Ann, Bob, Cy; October 2026 activity plus one September row.

    Hand-computed October values:
      Deposits by person: Ann 2 / 1500.00, Bob 1 / 800.00, Uncategorized 1 / 46.00
        (refund +20.00 and transfer-in excluded)
      Withdrawals by purpose: Water 1 / 600.00, Costco 3 / 207.27
        (230.00 + 17.27 - 40.00 refund), Uncategorized 1 / 12.00
        (transfer-out excluded; Rent Sep row with applies_month=Oct counted
        -> Rent 1 / 3000.00)
      Withdrawals by person (Costco 50/50 Ann/Bob; Water even Ann/Bob/Cy;
      Rent 1:1:1 Ann/Bob/Cy; Costco 17.27 unsplit):
        Ann 200.00 + 115.00 - 20.00 + 1000.00 = 1295.00
        Bob 200.00 + 115.00 - 20.00 + 1000.00 = 1295.00
        Cy  200.00 + 1000.00 = 1200.00
        Uncategorized: 17.27 (Costco no split) + 12.00 (no purpose) = 29.27
    """
    with conn:
        conn.executemany("INSERT INTO people (name) VALUES (?)", [("Ann",), ("Bob",), ("Cy",)])
    ids = {n: db.get_person_by_name(conn, n)["id"] for n in ("Ann", "Bob", "Cy")}
    pur = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM purposes")}
    A, B, C = ids["Ann"], ids["Bob"], ids["Cy"]

    t = {}
    # deposits
    t["ann1"] = _txn(conn, 1, 100000, "2026-10-01", person=A)
    t["ann2"] = _txn(conn, 2, 50000, "2026-10-15", person=A)
    t["bob1"] = _txn(conn, 3, 80000, "2026-10-02", person=B)
    t["unk_in"] = _txn(conn, 4, 4600, "2026-10-03")
    t["xfer_in"] = _txn(conn, 5, 999900, "2026-10-04", purpose=pur["Transfer"])
    t["refund"] = _txn(conn, 6, 4000, "2026-10-20", purpose=pur["Costco"])
    t["sep_dep"] = _txn(conn, 7, 12300, "2026-09-10", person=B)
    # withdrawals
    t["water"] = _txn(conn, 10, -60000, "2026-10-08", purpose=pur["Water"])
    t["costco1"] = _txn(conn, 11, -23000, "2026-10-09", purpose=pur["Costco"])
    t["costco2"] = _txn(conn, 12, -1727, "2026-10-10", purpose=pur["Costco"])  # no split
    t["unk_out"] = _txn(conn, 13, -1200, "2026-10-11")
    t["xfer_out"] = _txn(conn, 14, -500000, "2026-10-12", purpose=pur["Transfer"])
    t["rent"] = _txn(conn, 15, -300000, "2026-09-30", purpose=pur["Rent"], applies_month="2026-10")
    t["sep_out"] = _txn(conn, 16, -5000, "2026-09-15", purpose=pur["Water"])

    splits.set_transaction_split(conn, t["water"], [(A, 1), (B, 1), (C, 1)])
    splits.set_transaction_split(conn, t["costco1"], [(A, 1), (B, 1)])
    splits.set_transaction_split(conn, t["refund"], [(A, 1), (B, 1)])
    splits.set_transaction_split(conn, t["rent"], [(A, 1), (B, 1), (C, 1)])
    splits.set_transaction_split(conn, t["sep_out"], [(C, 1)])
    return ids, t


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    _seed(c)
    yield c
    c.close()


def _summary(groups):
    return {g.name: (g.count, g.total_cents) for g in groups}


OCT = ["2026-10"]


def test_deposits_by_person_october(conn):
    groups = reports.deposits_by_person(conn, OCT)
    assert _summary(groups) == {
        "Ann": (2, 150000),
        "Bob": (1, 80000),
        reports.UNCATEGORIZED: (1, 4600),
    }
    assert groups[-1].name == reports.UNCATEGORIZED  # always last


def test_withdrawal_to_person_in_deposits_view(conn):
    bob = db.get_person_by_name(conn, "Bob")["id"]
    w = _txn(conn, 99, -30000, "2026-10-21", person=bob)
    deposits = {g.name: g for g in reports.deposits_by_person(conn, OCT)}
    assert (deposits["Bob"].count, deposits["Bob"].total_cents) == (2, 50000)
    assert w in [t["id"] for t in deposits["Bob"].transactions]
    # Not spending: neither withdrawal view counts it.
    for view in (reports.withdrawals_by_purpose, reports.withdrawals_by_person):
        assert all(w not in [t["id"] for t in g.transactions] for g in view(conn, OCT))


def test_withdrawals_by_purpose_october(conn):
    assert _summary(reports.withdrawals_by_purpose(conn, OCT)) == {
        "Rent": (1, 300000),  # applies_month override pulls it into October
        "Water": (1, 60000),
        "Costco": (3, 20727),  # 230.00 + 17.27 - 40.00 refund
        reports.UNCATEGORIZED: (1, 1200),
    }


def test_withdrawals_by_person_october(conn):
    assert _summary(reports.withdrawals_by_person(conn, OCT)) == {
        "Ann": (4, 129500),
        "Bob": (4, 129500),
        "Cy": (2, 120000),
        reports.UNCATEGORIZED: (2, 2927),
    }


def test_by_person_and_by_purpose_reconcile(conn):
    by_purpose = sum(g.total_cents for g in reports.withdrawals_by_purpose(conn, OCT))
    by_person = sum(g.total_cents for g in reports.withdrawals_by_person(conn, OCT))
    assert by_purpose == by_person


def test_all_time_and_transfers_excluded(conn):
    dep = _summary(reports.deposits_by_person(conn))
    assert dep["Bob"] == (2, 92300)
    out = _summary(reports.withdrawals_by_purpose(conn))
    assert out["Water"] == (2, 65000)
    assert "Transfer" not in out
    names = {g.name for v in reports.VIEWS for g in reports.report(conn, v)}
    assert "Transfer" not in names
    assert _summary(reports.withdrawals_by_person(conn))["Cy"] == (3, 125000)


def test_month_filter_multi(conn):
    assert _summary(reports.deposits_by_person(conn, ["2026-09"])) == {"Bob": (1, 12300)}
    both = _summary(reports.deposits_by_person(conn, ["2026-09", "2026-10"]))
    assert both["Bob"] == (2, 92300)


def test_exclude_uncategorized(conn):
    for view in reports.VIEWS:
        groups = reports.report(conn, view, OCT, include_uncategorized=False)
        assert reports.UNCATEGORIZED not in {g.name for g in groups}
        assert all(not g.uncategorized for g in groups)


def test_group_transactions_listed(conn):
    costco = next(g for g in reports.withdrawals_by_purpose(conn, OCT) if g.name == "Costco")
    assert [t["value_cents"] for t in costco.transactions] == [23000, 1727, -4000]
    assert sum(t["value_cents"] for t in costco.transactions) == costco.total_cents
    ann = next(g for g in reports.withdrawals_by_person(conn, OCT) if g.name == "Ann")
    assert sorted(t["value_cents"] for t in ann.transactions) == [-2000, 11500, 20000, 100000]


def test_csv_matches_table(conn):
    view = reports.WITHDRAWALS_BY_PURPOSE
    groups = reports.report(conn, view, OCT)
    rows = list(csv.reader(io.StringIO(reports.to_csv(groups, view))))
    assert rows[0] == reports.table_columns(view)
    table = reports.table_rows(groups, view)
    assert len(rows) - 1 == len(table)
    for csv_row, t in zip(rows[1:], table):
        assert csv_row[0] == t["Group"]
        assert int(csv_row[1]) == t["Count"]
        assert float(csv_row[2]) == pytest.approx(t[reports.TOTAL_LABELS[view]])
    assert ["Costco", "3", "207.27"] in rows
    assert rows[-1] == [reports.UNCATEGORIZED, "1", "12.00"]


def test_csv_negative_and_empty():
    assert reports._dollars(-5) == "-0.05"
    assert reports.to_csv([], reports.DEPOSITS_BY_PERSON) == "Group,Count,Total in ($)\n"


def test_unknown_view(conn):
    with pytest.raises(ValueError):
        reports.report(conn, "nope")


def test_reports_page(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    path = tmp_path / "r.db"
    c = db.connect(path)
    _seed(c)
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))

    at = AppTest.from_file(str(ROOT / "pages/5_Reports.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    labels = [e.label for e in at.expander]
    assert any(label.startswith(reports.UNCATEGORIZED) for label in labels)

    at.radio(key="report_view").set_value(reports.WITHDRAWALS_BY_PERSON).run()
    assert not at.exception, at.exception
    at.toggle(key="report_include_uncat").set_value(False).run()
    assert not at.exception, at.exception
    assert not any(e.label.startswith(reports.UNCATEGORIZED) for e in at.expander)


# --- monthly average by purpose ----------------------------------------------


def test_period_months(conn):
    # All: first to last month in the data, gaps included.
    assert reports.period_months(conn, None) == ["2026-09", "2026-10"]
    _txn(conn, 90, -100, "2026-06-01")
    assert reports.period_months(conn, None) == ["2026-06", "2026-07", "2026-08", "2026-09",
                                                 "2026-10"]
    assert reports.period_months(conn, ["2026-10", "2026-06"]) == ["2026-06", "2026-10"]
    assert reports.period_months(db.connect(":memory:"), None) == []


def test_next_month_rolls_over_the_year():
    assert reports._next_month("2026-12") == "2027-01"
    assert reports._next_month("2026-09") == "2026-10"


def test_monthly_by_purpose(conn):
    period, rows = reports.monthly_by_purpose(conn, None, ["Water", "Costco", "Rent"])
    assert period == ["2026-09", "2026-10"]
    got = {r.name: (r.by_month, r.total_cents, r.average_cents) for r in rows}
    assert got == {
        # Rent paid in Sep but applies to Oct; Sep counts as 0.
        "Rent": ({"2026-09": 0, "2026-10": 300000}, 300000, 150000),
        "Water": ({"2026-09": 5000, "2026-10": 60000}, 65000, 32500),
        # Refund reduces Oct: 230.00 + 17.27 - 40.00.
        "Costco": ({"2026-09": 0, "2026-10": 20727}, 20727, 10364),  # 103.635 -> 103.64
        "Uncategorized": ({"2026-09": 0, "2026-10": 1200}, 1200, 600),
    }
    assert [r.name for r in rows] == ["Rent", "Water", "Costco", "Uncategorized"]
    costco = next(r for r in rows if r.name == "Costco")
    assert costco.average_is_rounded and costco.average == Fraction(20727, 2)
    total = reports.monthly_total(rows, period)
    assert total.by_month == {"2026-09": 5000, "2026-10": 381927}
    assert total.total_cents == sum(r.total_cents for r in rows)


def test_monthly_by_purpose_filters(conn):
    period, rows = reports.monthly_by_purpose(conn, OCT, ["Water"], include_uncategorized=False)
    assert period == OCT
    assert [(r.name, r.by_month, r.average_cents) for r in rows] == [
        ("Water", {"2026-10": 60000}, 60000)
    ]
    # Transfer is never a row, even if named.
    _, rows = reports.monthly_by_purpose(conn, None, ["Transfer"], include_uncategorized=False)
    assert rows == []


def test_round_cents_halves_away_from_zero():
    F = Fraction
    assert [reports.round_cents(F(n, 2)) for n in (1, 3, -1, -3)] == [1, 2, -1, -2]
    assert reports.round_cents(F(10, 3)) == 3 and reports.round_cents(F(-20, 3)) == -7


def test_monthly_table_and_csv(conn):
    period, rows = reports.monthly_by_purpose(conn, None, ["Costco"], include_uncategorized=False)
    table = reports.monthly_table_rows(rows, period)
    assert table == [{"Purpose": "Costco", "Sep 2026": 0.0, "Oct 2026": 207.27,
                      "Total ($)": 207.27, "Average / month ($)": 103.64}]
    assert reports.monthly_csv(rows, period) == (
        "Purpose,2026-09,2026-10,Total,Average per month\n"
        "Costco,0.00,207.27,207.27,103.64\n"
    )


def test_reports_page_monthly_average(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    path = tmp_path / "r.db"
    c = db.connect(path)
    _seed(c)
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))

    at = AppTest.from_file(str(ROOT / "pages/5_Reports.py"), default_timeout=30).run()
    at.radio(key="report_view").set_value(reports.MONTHLY_BY_PURPOSE).run()
    assert not at.exception, at.exception
    df = at.dataframe[0].value
    assert list(df["Purpose"]) == ["Rent", "Water", "Costco", "Uncategorized", "Total"]
    assert any("total ÷ 2 months" in c.value for c in at.caption)
    at.multiselect(key="report_avg_purposes").set_value(["Water"]).run()
    assert not at.exception, at.exception
    assert list(at.dataframe[0].value["Purpose"]) == ["Water", "Uncategorized", "Total"]
