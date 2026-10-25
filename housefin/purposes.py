"""Purposes: add, rename and (guarded) delete, plus default-split editing helpers.

The built-in Transfer purpose is protected: it is looked up by name
(``db.TRANSFER``), so it can be neither renamed nor deleted, and as an
excluded purpose it never has a split.

Default splits live in ``purpose_splits`` (see :mod:`housefin.splits`).
Changing one only affects withdrawals that get their split later; existing
transaction splits are never touched here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from housefin import dates, db, splits


class PurposeError(ValueError):
    """A purpose change was refused; the message is meant for the user."""


def is_split_by_rooms(purpose: sqlite3.Row | Mapping) -> bool:
    """Whether a purpose's split comes from room assignments (Rent): it has
    no default split; see the Rooms page."""
    return bool(purpose["split_by_rooms"])


def is_protected(purpose: sqlite3.Row | Mapping) -> bool:
    """Built-in purposes that cannot be renamed or deleted (Transfer)."""
    return purpose["name"] == db.TRANSFER


def _get(conn: sqlite3.Connection, purpose_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM purposes WHERE id = ?", (purpose_id,)).fetchone()
    if row is None:
        raise PurposeError("That purpose no longer exists.")
    return row


def _clean_name(conn: sqlite3.Connection, name: str, *, ignore_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise PurposeError("A purpose needs a name.")
    clash = conn.execute(
        "SELECT name FROM purposes WHERE lower(name) = lower(?) AND id IS NOT ?",
        (name, ignore_id),
    ).fetchone()
    if clash is not None:
        raise PurposeError(f"There is already a purpose called {clash['name']!r}.")
    return name


def add(conn: sqlite3.Connection, name: str) -> int:
    """Add a regular (included) purpose; returns its id. Names are unique,
    ignoring case and surrounding spaces."""
    return db.add_purpose(conn, _clean_name(conn, name))


def rename(conn: sqlite3.Connection, purpose_id: int, new_name: str) -> None:
    row = _get(conn, purpose_id)
    if is_protected(row):
        raise PurposeError(f"{row['name']} is built in and cannot be renamed.")
    name = _clean_name(conn, new_name, ignore_id=purpose_id)
    with db.transaction(conn):
        conn.execute("UPDATE purposes SET name = ? WHERE id = ?", (name, purpose_id))


def usage(conn: sqlite3.Connection, purpose_id: int) -> dict[str, int]:
    """How many transactions, rules and transaction splits use a purpose.

    ``splits`` counts split rows of transactions with this purpose (it is
    implied by ``transactions`` but reported for the message). The purpose's
    own default split is not a use.
    """
    q = lambda sql: conn.execute(sql, (purpose_id,)).fetchone()[0]  # noqa: E731
    return {
        "transactions": q("SELECT COUNT(*) FROM transactions WHERE purpose_id = ?"),
        "rules": q("SELECT COUNT(*) FROM rules WHERE purpose_id = ?"),
        "splits": q(
            "SELECT COUNT(*) FROM splits s JOIN transactions t ON t.id = s.transaction_id"
            " WHERE t.purpose_id = ?"
        ),
    }


def describe_usage(counts: Mapping[str, int]) -> str:
    """'3 transactions, 1 rule' (empty string when unused)."""
    parts = []
    for key, singular in (("transactions", "transaction"), ("rules", "rule")):
        n = counts.get(key, 0)
        if n:
            parts.append(f"{n} {singular if n == 1 else key}")
    if not parts and counts.get("splits"):
        parts.append(f"{counts['splits']} split rows")
    return ", ".join(parts)


def delete(conn: sqlite3.Connection, purpose_id: int) -> None:
    """Delete an unused purpose (its default split goes with it).

    Refused for Transfer, for the purpose split by rooms (Rent: nothing else
    could take that role) and for purposes still used by transactions, rules
    or splits; recategorize those first.
    """
    row = _get(conn, purpose_id)
    if is_protected(row):
        raise PurposeError(f"{row['name']} is built in and cannot be deleted.")
    if is_split_by_rooms(row):
        raise PurposeError(
            f"{row['name']} is split by rooms and cannot be deleted; rename it instead."
        )
    used = describe_usage(usage(conn, purpose_id))
    if used:
        raise PurposeError(
            f"{row['name']} is still used by {used}; recategorize those before deleting it."
        )
    with db.transaction(conn):
        conn.execute("DELETE FROM purpose_splits WHERE purpose_id = ?", (purpose_id,))
        conn.execute("DELETE FROM purposes WHERE id = ?", (purpose_id,))


def list_with_usage(conn: sqlite3.Connection) -> list[dict]:
    """Purposes (db.list_purposes order) as dicts with usage counts and
    ``protected`` / ``deletable`` flags."""
    out = []
    for row in db.list_purposes(conn):
        d = dict(row)
        d["usage"] = usage(conn, row["id"])
        d["protected"] = is_protected(row)
        d["split_by_rooms"] = is_split_by_rooms(row)
        d["deletable"] = (
            not d["protected"] and not d["split_by_rooms"] and not describe_usage(d["usage"])
        )
        out.append(d)
    return out


# --- default splits ------------------------------------------------------------


def split_candidates(conn: sqlite3.Connection, month: str | None = None) -> list[sqlite3.Row]:
    """People offered in a default split editor: those in the house in ``month``
    (default: this month) plus the House (listed first)."""
    return db.people_in_house(conn, month or dates.current_month(), include_house=True)


def even_split_people(conn: sqlite3.Connection, month: str | None = None) -> list[sqlite3.Row]:
    """Who the no-default fallback splits among in ``month`` (House excluded)."""
    return db.people_in_house(conn, month or dates.current_month())


def save_default_split(
    conn: sqlite3.Connection,
    purpose_id: int,
    weights: Mapping[int, float],
    *,
    month: str | None = None,
) -> None:
    """Replace a purpose's default split from editor weights.

    ``weights`` covers the offered people (see :func:`split_candidates`);
    zero means "not in the split". Stored weights of people who are not
    offered (moved out, or not yet moved in) are kept, since
    ``splits.default_split_for`` already drops them in months they are
    not in the house. All zeros (and nobody hidden) clears the default, i.e. the
    even-split fallback. Excluded purposes (Transfer) are refused. Past
    transactions are not changed.
    """
    row = _get(conn, purpose_id)
    if row["excluded"]:
        raise PurposeError(f"{row['name']} is left out of reports and has no split.")
    _refuse_split_by_rooms(row)
    if any(w < 0 for w in weights.values()):
        raise PurposeError("Weights cannot be negative.")
    offered = {p["id"] for p in split_candidates(conn, month)}
    unknown = set(weights) - offered
    if unknown:
        raise PurposeError("Only current housemates and the House can be in a default split.")
    kept = [(pid, w) for pid, w in splits.get_purpose_split(conn, purpose_id) if pid not in offered]
    new = [(pid, float(w)) for pid, w in weights.items() if w > 0]
    splits.set_purpose_split(conn, purpose_id, kept + new)


def _refuse_split_by_rooms(row: sqlite3.Row) -> None:
    if is_split_by_rooms(row):
        raise PurposeError(
            f"{row['name']} is split by rooms, so it has no default split: "
            "set room percents and who is in which room on the Rooms page."
        )


def clear_default_split(conn: sqlite3.Connection, purpose_id: int) -> None:
    """Back to the even split among housemates in the house (refused for a purpose
    split by rooms, which has no default split)."""
    _refuse_split_by_rooms(_get(conn, purpose_id))
    splits.set_purpose_split(conn, purpose_id, [])
