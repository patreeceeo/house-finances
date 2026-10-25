"""Report queries for the Reports screen: grouped totals plus per-group rows.

Every view leaves out transactions whose purpose is excluded (the built-in
Transfer). Months use the transaction month (applies_month override, else the
month of the date) via :func:`housefin.ui.month_filter_sql`.

Views
-----
* **Deposits by person** -- deposits (amount > 0) tagged with a person, plus
  withdrawals paid to a person, grouped by that person; total = net money in,
  so withdrawals reduce it (as on the Balances screen). Refund deposits (a
  purpose, no person) are *not* shown here; they reduce spending in the two
  withdrawal views instead. Deposits with neither person nor purpose form the
  Uncategorized group.
* **Withdrawals by purpose** -- withdrawals plus refund deposits, grouped by
  purpose; total = money out (``-amount_cents``), so refunds reduce it.
  Withdrawals paid to a person are not shown (they are in Deposits by person).
  Withdrawals with neither person nor purpose form the Uncategorized group.
* **Withdrawals by person** -- each person's ``SUM(splits.share_cents)`` over
  withdrawals and refund deposits (refund shares are negative). The House is
  shown like anyone else. Withdrawals paid to a person are not shares and are
  not shown. Withdrawals/refunds that have a purpose but no split rows, or
  withdrawals with neither person nor purpose, form the Uncategorized group
  with their full ``-amount_cents``.
* **Monthly average by purpose** -- Withdrawals by purpose split out per
  month of the period, for chosen purposes, with each one's total divided by
  the number of months in the period (months with no spending count as 0).

Totals are integer cents with a positive "in" (deposits view) or "out"
(withdrawal views) sign.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from fractions import Fraction

from housefin import ui

DEPOSITS_BY_PERSON = "Deposits by person"
WITHDRAWALS_BY_PURPOSE = "Withdrawals by purpose"
WITHDRAWALS_BY_PERSON = "Withdrawals by person"
MONTHLY_BY_PURPOSE = "Monthly average by purpose"
# Views with grouped totals (:func:`report`), and every view the page offers.
VIEWS = (DEPOSITS_BY_PERSON, WITHDRAWALS_BY_PURPOSE, WITHDRAWALS_BY_PERSON)
PAGE_VIEWS = (*VIEWS, MONTHLY_BY_PURPOSE)

UNCATEGORIZED = "Uncategorized"

TOTAL_LABELS = {
    DEPOSITS_BY_PERSON: "Total in ($)",
    WITHDRAWALS_BY_PURPOSE: "Total out ($)",
    WITHDRAWALS_BY_PERSON: "Share ($)",
}

# Not an excluded (Transfer) purpose; use with LEFT JOIN purposes pu.
_NOT_EXCLUDED = "COALESCE(pu.excluded, 0) = 0"
# A refund deposit: money in, with a purpose and no person.
_IS_REFUND = "(t.amount_cents > 0 AND t.person_id IS NULL AND t.purpose_id IS NOT NULL)"
# Spending: a withdrawal not paid to a person, or a refund deposit.
_IS_SPENDING = f"((t.amount_cents < 0 AND t.person_id IS NULL) OR {_IS_REFUND})"

_TXN_COLS = (
    "t.id, t.date, t.description, t.amount_cents, t.applies_month, "
    f"{ui.month_expr('t')} AS month"
)


@dataclass
class Group:
    name: str
    total_cents: int = 0
    transactions: list[dict] = field(default_factory=list)
    uncategorized: bool = False
    # Ordering key within the view (e.g. person/purpose id), not shown.
    key: int | None = None

    @property
    def count(self) -> int:
        return len(self.transactions)


def _txn(row: sqlite3.Row, value_cents: int) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "month": row["month"],
        "description": row["description"],
        "amount_cents": row["amount_cents"],
        "value_cents": value_cents,  # what this row contributes to the group total
    }


def _add(groups: dict, gid, name: str, row: sqlite3.Row, value: int, *, uncategorized=False):
    g = groups.get(gid)
    if g is None:
        g = groups[gid] = Group(name=name, uncategorized=uncategorized, key=gid)
    g.transactions.append(_txn(row, value))
    g.total_cents += value


def _finish(groups: dict, include_uncategorized: bool) -> list[Group]:
    out = [g for g in groups.values() if include_uncategorized or not g.uncategorized]
    # Largest totals first; Uncategorized always last.
    out.sort(key=lambda g: (g.uncategorized, -g.total_cents, g.name))
    return out


def deposits_by_person(
    conn: sqlite3.Connection, months: list[str] | None = None, *, include_uncategorized: bool = True
) -> list[Group]:
    where, params = ui.month_filter_sql(months, "t")
    rows = conn.execute(
        f"SELECT {_TXN_COLS}, t.person_id, p.name AS person FROM transactions t"
        " LEFT JOIN purposes pu ON pu.id = t.purpose_id"
        " LEFT JOIN people p ON p.id = t.person_id"
        f" WHERE ((t.amount_cents > 0 AND NOT {_IS_REFUND}) OR (t.amount_cents < 0 AND t.person_id IS NOT NULL))"
        f" AND {_NOT_EXCLUDED} AND {where}"
        " ORDER BY t.date, t.time, t.id",
        params,
    ).fetchall()
    groups: dict = {}
    for r in rows:
        if r["person_id"] is None:
            _add(groups, None, UNCATEGORIZED, r, r["amount_cents"], uncategorized=True)
        else:
            _add(groups, r["person_id"], r["person"], r, r["amount_cents"])
    return _finish(groups, include_uncategorized)


def withdrawals_by_purpose(
    conn: sqlite3.Connection, months: list[str] | None = None, *, include_uncategorized: bool = True
) -> list[Group]:
    where, params = ui.month_filter_sql(months, "t")
    rows = conn.execute(
        f"SELECT {_TXN_COLS}, t.purpose_id, pu.name AS purpose FROM transactions t"
        " LEFT JOIN purposes pu ON pu.id = t.purpose_id"
        f" WHERE {_IS_SPENDING} AND {_NOT_EXCLUDED} AND {where}"
        " ORDER BY t.date, t.time, t.id",
        params,
    ).fetchall()
    groups: dict = {}
    for r in rows:
        if r["purpose_id"] is None:
            _add(groups, None, UNCATEGORIZED, r, -r["amount_cents"], uncategorized=True)
        else:
            _add(groups, r["purpose_id"], r["purpose"], r, -r["amount_cents"])
    return _finish(groups, include_uncategorized)


def withdrawals_by_person(
    conn: sqlite3.Connection, months: list[str] | None = None, *, include_uncategorized: bool = True
) -> list[Group]:
    where, params = ui.month_filter_sql(months, "t")
    cond = f"{_IS_SPENDING} AND {_NOT_EXCLUDED} AND {where}"
    groups: dict = {}
    shares = conn.execute(
        f"SELECT {_TXN_COLS}, s.person_id, p.name AS person, s.share_cents"
        " FROM transactions t LEFT JOIN purposes pu ON pu.id = t.purpose_id"
        " JOIN splits s ON s.transaction_id = t.id JOIN people p ON p.id = s.person_id"
        f" WHERE {cond} ORDER BY t.date, t.time, t.id",
        params,
    ).fetchall()
    for r in shares:
        _add(groups, r["person_id"], r["person"], r, r["share_cents"])
    unsplit = conn.execute(
        f"SELECT {_TXN_COLS} FROM transactions t LEFT JOIN purposes pu ON pu.id = t.purpose_id"
        f" WHERE {cond}"
        " AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)"
        " ORDER BY t.date, t.time, t.id",
        params,
    ).fetchall()
    for r in unsplit:
        _add(groups, None, UNCATEGORIZED, r, -r["amount_cents"], uncategorized=True)
    return _finish(groups, include_uncategorized)


_VIEW_FUNCS = {
    DEPOSITS_BY_PERSON: deposits_by_person,
    WITHDRAWALS_BY_PURPOSE: withdrawals_by_purpose,
    WITHDRAWALS_BY_PERSON: withdrawals_by_person,
}


def report(
    conn: sqlite3.Connection,
    view: str,
    months: list[str] | None = None,
    *,
    include_uncategorized: bool = True,
) -> list[Group]:
    """Groups for one of :data:`VIEWS`."""
    try:
        func = _VIEW_FUNCS[view]
    except KeyError:
        raise ValueError(f"unknown report view: {view!r}") from None
    return func(conn, months, include_uncategorized=include_uncategorized)


# --- monthly average by purpose ----------------------------------------------


@dataclass
class MonthlyRow:
    """One purpose's spending per month of the period (Withdrawals by purpose
    rules: money out, refunds reduce it). Months with none are 0."""

    name: str
    by_month: dict[str, int]
    uncategorized: bool = False

    @property
    def total_cents(self) -> int:
        return sum(self.by_month.values())

    @property
    def average(self) -> Fraction:
        """Exact average per month of the period, in cents."""
        return Fraction(self.total_cents, len(self.by_month)) if self.by_month else Fraction(0)

    @property
    def average_cents(self) -> int:
        """:attr:`average` rounded to the nearest cent (halves away from zero)."""
        return round_cents(self.average)

    @property
    def average_is_rounded(self) -> bool:
        return self.average.denominator != 1


def round_cents(value: Fraction) -> int:
    whole, rest = divmod(abs(value), 1)
    cents = int(whole) + (1 if rest >= Fraction(1, 2) else 0)
    return -cents if value < 0 else cents


def _next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def period_months(conn: sqlite3.Connection, months: list[str] | None) -> list[str]:
    """The months an average is taken over, oldest first: the selected months,
    or for All every month from the first to the last in the data (including
    months with no transactions at all)."""
    if months:
        return sorted(set(months))
    present = ui.months_present(conn)
    if not present:
        return []
    out, m = [], present[-1]
    while m <= present[0]:
        out.append(m)
        m = _next_month(m)
    return out


def monthly_by_purpose(
    conn: sqlite3.Connection,
    months: list[str] | None = None,
    purposes: list[str] | None = None,
    *,
    include_uncategorized: bool = True,
) -> tuple[list[str], list[MonthlyRow]]:
    """``(period, rows)``: per-month spending for each purpose named in
    ``purposes`` (None = every purpose with spending) over :func:`period_months`.
    Uncategorized is included when asked, whatever ``purposes`` says. Largest
    totals first, Uncategorized last."""
    period = period_months(conn, months)
    groups = withdrawals_by_purpose(conn, months, include_uncategorized=include_uncategorized)
    rows = []
    for g in groups:
        if not g.uncategorized and purposes is not None and g.name not in purposes:
            continue
        by_month = dict.fromkeys(period, 0)
        for t in g.transactions:
            by_month[t["month"]] += t["value_cents"]
        rows.append(MonthlyRow(name=g.name, by_month=by_month, uncategorized=g.uncategorized))
    rows.sort(key=lambda r: (r.uncategorized, -r.total_cents, r.name))
    return period, rows


def monthly_total(rows: list[MonthlyRow], period: list[str]) -> MonthlyRow:
    """A Total row: the rows summed month by month."""
    return MonthlyRow(
        name="Total", by_month={m: sum(r.by_month[m] for r in rows) for m in period}
    )


def monthly_table_rows(rows: list[MonthlyRow], period: list[str]) -> list[dict]:
    """The on-screen table: per-month dollars, total and average per month."""
    out = []
    for r in rows:
        row = {"Purpose": r.name}
        row.update({ui.format_month(m): ui.cents_to_dollars(r.by_month[m]) for m in period})
        row["Total ($)"] = ui.cents_to_dollars(r.total_cents)
        row["Average / month ($)"] = ui.cents_to_dollars(r.average_cents)
        out.append(row)
    return out


def monthly_csv(rows: list[MonthlyRow], period: list[str]) -> str:
    """CSV of the monthly table (months as YYYY-MM; average rounded to the cent)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Purpose", *period, "Total", "Average per month"])
    for r in rows:
        w.writerow([r.name, *(_dollars(r.by_month[m]) for m in period),
                    _dollars(r.total_cents), _dollars(r.average_cents)])
    return buf.getvalue()


# --- table / CSV -------------------------------------------------------------


def _dollars(cents: int) -> str:
    """Plain decimal dollars for CSV: -123456 -> '-1234.56'."""
    sign = "-" if cents < 0 else ""
    d, c = divmod(abs(int(cents)), 100)
    return f"{sign}{d}.{c:02d}"


def table_columns(view: str) -> list[str]:
    return ["Group", "Count", TOTAL_LABELS[view]]


def table_rows(groups: list[Group], view: str) -> list[dict]:
    """The on-screen table: one row per group (totals in dollars as floats)."""
    label = TOTAL_LABELS[view]
    return [
        {"Group": g.name, "Count": g.count, label: ui.cents_to_dollars(g.total_cents)}
        for g in groups
    ]


def to_csv(groups: list[Group], view: str) -> str:
    """CSV of the grouped table, matching the on-screen rows and order."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(table_columns(view))
    for g in groups:
        w.writerow([g.name, g.count, _dollars(g.total_cents)])
    return buf.getvalue()


def grand_total(groups: list[Group]) -> tuple[int, int]:
    """(count, total_cents) over the given groups.

    In Withdrawals by person a transaction split among several people is
    counted once per person.
    """
    return sum(g.count for g in groups), sum(g.total_cents for g in groups)
