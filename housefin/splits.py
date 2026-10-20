"""Splits: who is responsible for each withdrawal (or refund deposit), and how much.

Sign convention (read this before consuming ``splits.share_cents``)
------------------------------------------------------------------
``transactions.amount_cents`` is signed from the bank's point of view
(deposits > 0, withdrawals < 0). Stored shares are signed from the *person's*
point of view: ``share_cents`` is what that person owes for the transaction.

* withdrawal of -400000 -> shares are positive and sum to +400000
* refund deposit of +2500 -> shares are negative and sum to -2500

So for every transaction with a split, ``sum(share_cents) == -amount_cents``,
and a person's "share of spending" is simply ``SUM(share_cents)`` (refunds
reduce it automatically). Balance = paid in - share of spending.

The House may appear in a split; its share is stored like anyone else's and
consumers (Balances) simply ignore the House row. Purposes with
``excluded = 1`` (the built-in Transfer) never get a split.

A transaction's month is ``applies_month`` if set, otherwise the month of its
``date`` (see :data:`TXN_MONTH_SQL`).

Where a split came from is kept in ``transactions.split_source``:
``'default'`` (written from the purpose's default split by
:func:`apply_default_split`), ``'manual'`` (set by hand,
:func:`set_transaction_split`) or NULL (no split). Every function here that
writes or removes split rows keeps it in step; other modules remove a split
with :func:`clear_split`. Rows saved before the column existed with split rows
but no source (only possible from raw SQL) are treated as manual.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from fractions import Fraction

from housefin import db

Weights = Sequence[tuple[int, float]]

DEFAULT = "default"
MANUAL = "manual"

# SQL expression for a transaction's effective month; use with alias ``t``.
TXN_MONTH_SQL = "COALESCE(t.applies_month, substr(t.date, 1, 7))"


# --- pure computation ----------------------------------------------------------


def _exact(weight: float | int) -> Fraction:
    # str() so a weight like 33.3 is treated as the decimal the user typed.
    return Fraction(weight) if isinstance(weight, int) else Fraction(str(weight))


def compute_shares(amount_cents: int, weights: Weights) -> list[tuple[int, int]]:
    """Split ``amount_cents`` across ``(person_id, weight)`` pairs.

    Each share is amount x weight / total weight, rounded half away from zero
    to the cent. Whatever the rounding leaves over (positive or negative) goes
    to the largest weight; ties go to the lowest person_id. Shares carry the
    sign of ``amount_cents`` and always sum exactly to it. Returns
    ``[(person_id, share_cents)]`` in input order.

    This function is sign-agnostic; callers storing splits pass
    ``-transaction.amount_cents`` (see the module docstring).
    """
    if not weights:
        raise ValueError("a split needs at least one person")
    ids = [pid for pid, _ in weights]
    if len(set(ids)) != len(ids):
        raise ValueError("a person appears twice in the split")
    exact = [_exact(w) for _, w in weights]
    if any(w <= 0 for w in exact):
        raise ValueError("split weights must be positive")

    sign = -1 if amount_cents < 0 else 1
    magnitude = abs(amount_cents)
    total = sum(exact)
    shares = []
    for w in exact:
        raw = magnitude * w / total
        shares.append(int(raw + Fraction(1, 2)))  # half-up on a non-negative value
    leftover = magnitude - sum(shares)
    if leftover:
        largest = min(range(len(weights)), key=lambda i: (-exact[i], ids[i]))
        shares[largest] += leftover
    return [(pid, sign * s) for pid, s in zip(ids, shares)]


def to_percents(weights: Weights) -> dict[int, float]:
    """``{person_id: percent}`` for the positive weights, to two decimals and
    summing to exactly 100 (rounding leftover goes as in :func:`compute_shares`).
    Split editors show these; the percents are then saved as the weights."""
    positive = [(pid, w) for pid, w in weights if w > 0]
    if not positive:
        return {}
    return {pid: hundredths / 100 for pid, hundredths in compute_shares(10000, positive)}


def percent_total_error(percents: Iterable[float]) -> str | None:
    """Why a split editor's percents can't be saved, or None when they total 100%."""
    total = round(sum(percents), 2)
    if total == 100:
        return None
    if total > 100:
        return f"Percents total {total:g}%; take off {round(total - 100, 2):g}%."
    return f"Percents total {total:g}%; {round(100 - total, 2):g}% is still unassigned."


def same_split(a: Weights, b: Weights) -> bool:
    """Whether two weight lists divide an amount the same way (compared as the
    percents a split editor shows, so 1/1/1 equals 33.34/33.33/33.33)."""
    return to_percents(_clean(a)) == to_percents(_clean(b))


def transaction_month(row: sqlite3.Row | dict) -> str:
    """Effective 'YYYY-MM' of a transactions row."""
    return row["applies_month"] or row["date"][:7]


# --- purpose default splits ------------------------------------------------------


def get_purpose_split(conn: sqlite3.Connection, purpose_id: int) -> list[tuple[int, float]]:
    """The stored default ``[(person_id, weight)]`` for a purpose (may be empty)."""
    rows = conn.execute(
        "SELECT person_id, weight FROM purpose_splits WHERE purpose_id = ? ORDER BY person_id",
        (purpose_id,),
    ).fetchall()
    return [(r["person_id"], r["weight"]) for r in rows]


def set_purpose_split(conn: sqlite3.Connection, purpose_id: int, weights: Weights) -> None:
    """Replace a purpose's default split. Empty ``weights`` clears it (back to
    the even-split fallback). Zero weights are dropped. Past transactions are
    not touched."""
    weights = _clean(weights)
    with db.transaction(conn):
        conn.execute("DELETE FROM purpose_splits WHERE purpose_id = ?", (purpose_id,))
        conn.executemany(
            "INSERT INTO purpose_splits (purpose_id, person_id, weight) VALUES (?, ?, ?)",
            [(purpose_id, pid, w) for pid, w in weights],
        )


def _clean(weights: Weights) -> list[tuple[int, float]]:
    """Drop zero weights (UI 'not in this split'); reject negatives/duplicates."""
    out = [(int(pid), w) for pid, w in weights if w != 0]
    if any(w < 0 for _, w in out):
        raise ValueError("split weights must not be negative")
    if len({pid for pid, _ in out}) != len(out):
        raise ValueError("a person appears twice in the split")
    return out


def _is_excluded(conn: sqlite3.Connection, purpose_id: int) -> bool:
    row = conn.execute("SELECT excluded FROM purposes WHERE id = ?", (purpose_id,)).fetchone()
    return row is not None and bool(row["excluded"])


def default_split_for(
    conn: sqlite3.Connection, purpose_id: int, month: str
) -> list[tuple[int, float]]:
    """The split a new transaction with this purpose in ``month`` starts from.

    * Excluded purposes (Transfer) -> ``[]`` (no split).
    * The purpose's remembered default, minus anyone not active in ``month``
      (others keep their weights; the House is always active).
    * If there is no default, or nobody in it is active: an even split
      (weight 1 each) among people active that month, House excluded.
    * ``[]`` if nobody is active at all.
    """
    if _is_excluded(conn, purpose_id):
        return []
    people = {p["id"]: p for p in db.list_people(conn)}
    default = [
        (pid, w)
        for pid, w in get_purpose_split(conn, purpose_id)
        if pid in people and (people[pid]["is_house"] or db.is_active(people[pid], month))
    ]
    if default:
        return default
    return [(p["id"], 1.0) for p in db.active_people(conn, month)]


# --- transaction splits ---------------------------------------------------------


def get_transaction_split(conn: sqlite3.Connection, txn_id: int) -> list[sqlite3.Row]:
    """Rows (person_id, name, weight, share_cents) of a transaction's split."""
    return conn.execute(
        "SELECT s.person_id, p.name, s.weight, s.share_cents FROM splits s"
        " JOIN people p ON p.id = s.person_id"
        " WHERE s.transaction_id = ? ORDER BY s.weight DESC, p.name",
        (txn_id,),
    ).fetchall()


def _get_txn(conn: sqlite3.Connection, txn_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, amount_cents, date, applies_month, purpose_id, split_source"
        " FROM transactions WHERE id = ?",
        (txn_id,),
    ).fetchone()
    if row is None:
        raise KeyError(txn_id)
    return row


def get_split_source(conn: sqlite3.Connection, txn_id: int) -> str | None:
    """``'default'``, ``'manual'`` or None (no split) for a transaction."""
    return _get_txn(conn, txn_id)["split_source"]


def clear_split(conn: sqlite3.Connection, txn_id: int) -> None:
    """Remove a transaction's split and its source. No transaction of its own:
    callers already inside one (``db.transaction``) use it as a single step."""
    conn.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
    conn.execute("UPDATE transactions SET split_source = NULL WHERE id = ?", (txn_id,))


def _write_split(
    conn: sqlite3.Connection, txn: sqlite3.Row, weights: Weights, source: str
) -> None:
    """Replace the split rows and record ``source`` (no commit). Empty weights
    -> no split (source NULL)."""
    if source not in (DEFAULT, MANUAL):
        raise ValueError(f"unknown split source {source!r}")
    clear_split(conn, txn["id"])
    if not weights:
        return
    conn.execute("UPDATE transactions SET split_source = ? WHERE id = ?", (source, txn["id"]))
    shares = dict(compute_shares(-txn["amount_cents"], weights))
    conn.executemany(
        "INSERT INTO splits (transaction_id, person_id, weight, share_cents) VALUES (?, ?, ?, ?)",
        [(txn["id"], pid, w, shares[pid]) for pid, w in weights],
    )


def set_transaction_split(
    conn: sqlite3.Connection,
    txn_id: int,
    weights: Weights,
    *,
    remember_for_purpose: bool = False,
    source: str = MANUAL,
) -> None:
    """Save a split for one transaction, computing share_cents.

    Zero weights are dropped; empty ``weights`` removes the split. ``source``
    is recorded as the split's origin: 'manual' (set by hand, the default) or
    'default' (the caller knows these weights are the purpose default). With
    ``remember_for_purpose`` the weights also become the default split of the
    transaction's current purpose (other transactions are not changed, not
    even ones whose split came from the old default).
    """
    weights = _clean(weights)
    txn = _get_txn(conn, txn_id)
    with db.transaction(conn):
        _write_split(conn, txn, weights, source)
        if remember_for_purpose and txn["purpose_id"] is not None and weights:
            set_purpose_split(conn, txn["purpose_id"], weights)


def apply_default_split(
    conn: sqlite3.Connection, txn_ids: Iterable[int], *, replace: bool = False
) -> int:
    """Give transactions their purpose's default split (source 'default').

    Call it for rows whose purpose was just set or changed. For each id: no
    purpose -> skipped; excluded purpose (Transfer) -> any split is removed; a
    split set by hand (source 'manual', or split rows with no source) is kept
    unless ``replace=True`` (use that when the purpose itself changed: the old
    split belongs to the old purpose and is stale). Anything else (no split,
    or a split that came from a default) gets the purpose's current default.

    Not for re-applying an edited purpose default to past transactions:
    changing a default never changes existing splits, so don't pass ids whose
    purpose did not change. Returns how many transactions got a split written.
    """
    written = 0
    with db.transaction(conn):
        for txn_id in txn_ids:
            txn = _get_txn(conn, txn_id)
            if txn["purpose_id"] is None:
                continue
            if _is_excluded(conn, txn["purpose_id"]):
                clear_split(conn, txn_id)
                continue
            if not replace and _is_manual(conn, txn):
                continue
            weights = default_split_for(conn, txn["purpose_id"], transaction_month(txn))
            _write_split(conn, txn, weights, DEFAULT)
            if weights:
                written += 1
    return written


def _is_manual(conn: sqlite3.Connection, txn: sqlite3.Row) -> bool:
    if txn["split_source"] == MANUAL:
        return True
    if txn["split_source"] == DEFAULT:
        return False
    # No recorded source: split rows written outside this module count as manual.
    return conn.execute(
        "SELECT 1 FROM splits WHERE transaction_id = ? LIMIT 1", (txn["id"],)
    ).fetchone() is not None


def sync_after_rules(conn: sqlite3.Connection, changed) -> int:
    """Bring splits in line after a rule operation changed purposes.

    ``changed`` is a ``rules.ApplyResult`` (anything with ``purpose_changed``)
    or an iterable of transaction ids whose purpose changed. For each: purpose
    now NULL -> split removed; excluded purpose (Transfer) -> split removed;
    otherwise the purpose's default split replaces whatever was there
    (``replace=True``: the purpose changed, so the old split is stale). Rule
    operations never touch manual rows, so no manual split is overwritten.

    Runs in one ``db.transaction`` (a savepoint inside a caller's). Returns how
    many transactions got a split written.
    """
    ids = list(getattr(changed, "purpose_changed", changed))
    if not ids:
        return 0
    with db.transaction(conn):
        marks = ",".join("?" * len(ids))
        no_purpose = {
            r[0]
            for r in conn.execute(
                f"SELECT id FROM transactions WHERE id IN ({marks}) AND purpose_id IS NULL",
                ids,
            )
        }
        for i in no_purpose:
            clear_split(conn, i)
        return apply_default_split(
            conn, [i for i in ids if i not in no_purpose], replace=True
        )


# --- reporting -------------------------------------------------------------------


def share_totals(
    conn: sqlite3.Connection,
    months: Iterable[str] | None = None,
    *,
    through: str | None = None,
) -> dict[int, int]:
    """``{person_id: SUM(share_cents)}`` over split transactions.

    ``months``: restrict to these effective months ('YYYY-MM'); None = all.
    ``through``: only months <= this one (for running balances).
    Excluded purposes (Transfer) are left out. The House is included; drop
    ``db.get_house_id(conn)`` if you don't want it. Positive = owed (see the
    module docstring).
    """
    sql = (
        "SELECT s.person_id, SUM(s.share_cents) AS total FROM splits s"
        " JOIN transactions t ON t.id = s.transaction_id"
        " LEFT JOIN purposes pu ON pu.id = t.purpose_id"
        " WHERE COALESCE(pu.excluded, 0) = 0"
    )
    params: list = []
    if months is not None:
        months = list(months)
        if not months:
            return {}
        sql += f" AND {TXN_MONTH_SQL} IN ({','.join('?' * len(months))})"
        params += months
    if through is not None:
        sql += f" AND {TXN_MONTH_SQL} <= ?"
        params.append(through)
    sql += " GROUP BY s.person_id"
    return {r["person_id"]: r["total"] for r in conn.execute(sql, params)}
