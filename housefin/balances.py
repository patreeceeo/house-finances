"""Balances: what each person paid in, their share of spending, and what they owe.

Pure query functions over the DB (no Streamlit), used by pages/6_Balances.py.

Definitions (see also the sign convention in :mod:`housefin.splits`):

* **Paid in** - deposits (``amount_cents > 0``) credited to a person
  (``person_id``). Refund deposits carry a purpose + split and no person, so
  they are not paid in; they reduce shares instead.
* **Share of spending** - ``SUM(splits.share_cents)``: positive for
  withdrawals, negative for refunds.
* **Net** = paid in - share, for the selected months.
* **Running balance** = the same over all time through the end of the latest
  selected month (with All selected it equals net). Positive = credit,
  negative = owed.

Transactions whose purpose is excluded (Transfer) count toward neither.
Months are effective months (``applies_month`` else the date's month).
The House never gets a row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from housefin import db, splits
from housefin.splits import TXN_MONTH_SQL

_NOT_EXCLUDED = "COALESCE(pu.excluded, 0) = 0"
_JOIN_PURPOSE = " LEFT JOIN purposes pu ON pu.id = t.purpose_id"


@dataclass(frozen=True)
class BalanceRow:
    person_id: int
    name: str
    paid_in_cents: int
    share_cents: int
    running_cents: int
    moved_out: bool

    @property
    def net_cents(self) -> int:
        return self.paid_in_cents - self.share_cents


def _month_clause(months: list[str] | None, through: str | None) -> tuple[str, list]:
    sql, params = "", []
    if months is not None:
        sql += f" AND {TXN_MONTH_SQL} IN ({','.join('?' * len(months))})"
        params += months
    if through is not None:
        sql += f" AND {TXN_MONTH_SQL} <= ?"
        params.append(through)
    return sql, params


def _norm(months: Iterable[str] | None) -> list[str] | None:
    """None/empty -> None (all time), else a sorted list."""
    if months is None:
        return None
    months = sorted(set(months))
    return months or None


def paid_in_totals(
    conn: sqlite3.Connection,
    months: Iterable[str] | None = None,
    *,
    through: str | None = None,
) -> dict[int, int]:
    """``{person_id: SUM(deposit amount_cents)}`` (House included)."""
    clause, params = _month_clause(_norm(months), through)
    rows = conn.execute(
        "SELECT t.person_id, SUM(t.amount_cents) AS total FROM transactions t"
        + _JOIN_PURPOSE
        + f" WHERE t.amount_cents > 0 AND t.person_id IS NOT NULL AND {_NOT_EXCLUDED}"
        + clause
        + " GROUP BY t.person_id",
        params,
    )
    return {r["person_id"]: r["total"] for r in rows}


def share_totals(
    conn: sqlite3.Connection,
    months: Iterable[str] | None = None,
    *,
    through: str | None = None,
) -> dict[int, int]:
    """``{person_id: SUM(share_cents)}`` (House included; Transfer excluded)."""
    return splits.share_totals(conn, _norm(months), through=through)


def latest_month(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(f"SELECT MAX({TXN_MONTH_SQL}) FROM transactions t").fetchone()
    return row[0]


def reference_month(conn: sqlite3.Connection, months: Iterable[str] | None) -> str:
    """The month 'moved out' is judged against: the latest selected month, or
    for All the latest month in the data (today's month if there is none)."""
    months = _norm(months)
    if months:
        return months[-1]
    return latest_month(conn) or date.today().strftime("%Y-%m")


def balances(conn: sqlite3.Connection, months: Iterable[str] | None = None) -> list[BalanceRow]:
    """Balance rows for the Balances screen, ordered by name.

    Rows: every non-House person who is not moved out (as of
    :func:`reference_month`) and either is active in a selected month (All:
    has started by the reference month) or has amounts in the selection; plus
    every moved-out person whose running balance is non-zero.
    """
    months = _norm(months)
    ref = reference_month(conn, months)
    through = months[-1] if months else None

    paid = paid_in_totals(conn, months)
    share = share_totals(conn, months)
    if months is None:
        run_paid, run_share = paid, share
    else:
        run_paid = paid_in_totals(conn, through=through)
        run_share = share_totals(conn, through=through)

    out = []
    for p in db.list_people(conn, include_house=False):
        pid = p["id"]
        row = BalanceRow(
            person_id=pid,
            name=p["name"],
            paid_in_cents=paid.get(pid, 0),
            share_cents=share.get(pid, 0),
            running_cents=run_paid.get(pid, 0) - run_share.get(pid, 0),
            moved_out=p["active_until"] is not None and p["active_until"] < ref,
        )
        if row.moved_out:
            keep = row.running_cents != 0
        else:
            if months is None:
                active = p["active_from"] is None or p["active_from"] <= ref
            else:
                active = any(db.is_active(p, m) for m in months)
            keep = active or row.paid_in_cents != 0 or row.share_cents != 0
        if keep:
            out.append(row)
    return sorted(out, key=lambda r: r.name.casefold())


def paid_in_transactions(
    conn: sqlite3.Connection,
    person_id: int,
    months: Iterable[str] | None = None,
    *,
    through: str | None = None,
) -> list[sqlite3.Row]:
    """Deposits behind a person's Paid in (newest first).

    Columns: id, date, month, amount_cents, description.
    """
    clause, params = _month_clause(_norm(months), through)
    return conn.execute(
        f"SELECT t.id, t.date, {TXN_MONTH_SQL} AS month, t.amount_cents, t.description"
        " FROM transactions t"
        + _JOIN_PURPOSE
        + f" WHERE t.amount_cents > 0 AND t.person_id = ? AND {_NOT_EXCLUDED}"
        + clause
        + " ORDER BY t.date DESC, t.time DESC, t.id DESC",
        [person_id, *params],
    ).fetchall()


def share_transactions(
    conn: sqlite3.Connection,
    person_id: int,
    months: Iterable[str] | None = None,
    *,
    through: str | None = None,
) -> list[sqlite3.Row]:
    """Withdrawals / refunds behind a person's Share of spending (newest first).

    Columns: id, date, month, amount_cents, description, purpose, weight,
    share_cents.
    """
    clause, params = _month_clause(_norm(months), through)
    return conn.execute(
        f"SELECT t.id, t.date, {TXN_MONTH_SQL} AS month, t.amount_cents, t.description,"
        " pu.name AS purpose, s.weight, s.share_cents"
        " FROM splits s JOIN transactions t ON t.id = s.transaction_id"
        + _JOIN_PURPOSE
        + f" WHERE s.person_id = ? AND {_NOT_EXCLUDED}"
        + clause
        + " ORDER BY t.date DESC, t.time DESC, t.id DESC",
        [person_id, *params],
    ).fetchall()


def uncategorized_withdrawals(
    conn: sqlite3.Connection, months: Iterable[str] | None = None
) -> tuple[int, int]:
    """``(count, total_cents)`` of withdrawals in the months that are in no one's
    share: no purpose, or a non-excluded purpose without a split. The total is
    negative (bank sign) or 0."""
    clause, params = _month_clause(_norm(months), None)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(t.amount_cents), 0) FROM transactions t"
        + _JOIN_PURPOSE
        + " WHERE t.amount_cents < 0 AND (t.purpose_id IS NULL OR ("
        + _NOT_EXCLUDED
        + " AND NOT EXISTS (SELECT 1 FROM splits s WHERE s.transaction_id = t.id)))"
        + clause,
        params,
    ).fetchone()
    return row[0], row[1]
