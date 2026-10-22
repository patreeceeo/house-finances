"""Categorize screen data layer: list transactions, save manual tags.

Every save here marks the row ``category_source = 'manual'`` so rules never
overwrite it. A save can also:

* store an explicit split (``weights``) and, with ``remember_split``, make it
  the purpose's default for future transactions (past ones are untouched);
  the split's source (``transactions.split_source``) says whether it is the
  purpose default or set by hand (see :func:`_save_split`);
* Remember the description as a rule (``remember=True``), which tags every
  other *uncategorized* matching row. Rows tagged that way get their purpose's
  default split (``apply_default_split(replace=True)``): they were
  uncategorized, so any split rows they had are stale, never a manual split.

Saving a split from an editor is a person's decision, so it also confirms a
split that was pending review (default split that left someone out).

Each save is one transaction (``db.transaction``): if any step fails, nothing
from it is kept. Order inside a save matters and is: tag the row -> write its split (and the
remembered default) -> create the rule -> default splits for rows the rule
tagged. So rows tagged by a new rule start from the split just remembered.

Any transaction can carry a person (``save_person``): a deposit they sent,
or a withdrawal paid to them, which counts against their balance. Otherwise
deposits are refunds with a purpose and a split (``save_refund``) and
withdrawals carry a purpose and a split (``save_withdrawal``). Excluded
purposes (Transfer) never get a split.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from housefin import db, rules, splits
from housefin.parsers.base import normalize_description
from housefin.splits import Weights

UNCATEGORIZED = "uncategorized"
REVIEW = "review"
ALL = "all"
DEPOSITS = "deposits"
WITHDRAWALS = "withdrawals"

_NEEDS_REVIEW = "EXISTS (SELECT 1 FROM split_left_out l WHERE l.transaction_id = t.id)"

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class _Keep:
    def __repr__(self) -> str:
        return "KEEP"


KEEP = _Keep()  # sentinel: leave applies_month as it is


@dataclass
class SaveResult:
    rule_id: int | None = None
    rule_tagged: int = 0  # other rows the remembered rule tagged
    rule_tagged_ids: list[int] | None = None


# --- listing -------------------------------------------------------------------


def list_transactions(
    conn: sqlite3.Connection,
    *,
    status: str = UNCATEGORIZED,
    kind: str = ALL,
    months: list[str] | None = None,
    search: str | None = None,
) -> list[sqlite3.Row]:
    """Transactions newest first, with person/purpose names joined in.

    ``status``: 'uncategorized' (no person and no purpose), 'review' (split
    pending review: its default left someone out) or 'all'.
    ``kind``: 'all', 'deposits' (amount > 0) or 'withdrawals' (amount <= 0).
    ``months``: effective months (applies_month, else the date's month); None = all.
    ``search``: case-insensitive substring of the description, person or purpose name.

    Extra columns: person_name, purpose_name, purpose_excluded, month,
    needs_review (0/1).
    """
    where = ["1"]
    params: list = []
    if status == UNCATEGORIZED:
        where.append("t.person_id IS NULL AND t.purpose_id IS NULL")
    elif status == REVIEW:
        where.append(_NEEDS_REVIEW)
    elif status != ALL:
        raise ValueError(f"unknown status {status!r}")
    if kind == DEPOSITS:
        where.append("t.amount_cents > 0")
    elif kind == WITHDRAWALS:
        where.append("t.amount_cents <= 0")
    elif kind != ALL:
        raise ValueError(f"unknown kind {kind!r}")
    if months:
        where.append(f"{splits.TXN_MONTH_SQL} IN ({','.join('?' * len(months))})")
        params += list(months)
    if search and search.strip():
        escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        where.append(
            "(t.description LIKE ? ESCAPE '\\' OR COALESCE(p.name, '') LIKE ? ESCAPE '\\'"
            " OR COALESCE(pu.name, '') LIKE ? ESCAPE '\\')"
        )
        params += [like, like, like]
    sql = (
        "SELECT t.*, p.name AS person_name, pu.name AS purpose_name,"
        f" COALESCE(pu.excluded, 0) AS purpose_excluded, {splits.TXN_MONTH_SQL} AS month,"
        f" {_NEEDS_REVIEW} AS needs_review"
        " FROM transactions t"
        " LEFT JOIN people p ON p.id = t.person_id"
        " LEFT JOIN purposes pu ON pu.id = t.purpose_id"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY t.date DESC, t.time DESC, t.id DESC"
    )
    return conn.execute(sql, params).fetchall()


def get_transaction(conn: sqlite3.Connection, txn_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    return row


# --- remember preview --------------------------------------------------------------


@dataclass(frozen=True)
class RulePreview:
    match_type: str  # 'exact' or 'contains'
    total: int  # existing rows of this sign the pattern matches
    will_tag: int  # other uncategorized rows a save would tag now


def preview_rule(conn: sqlite3.Connection, txn_id: int, pattern: str) -> RulePreview:
    """What Remember with ``pattern`` would do for this transaction."""
    txn = get_transaction(conn, txn_id)
    match_type = rules.match_type_for(txn["description"], pattern)
    sign = rules.sign_of(txn["amount_cents"])
    if not normalize_description(pattern or ""):
        return RulePreview(match_type, 0, 0)
    total = rules.preview_match_count(conn, pattern, sign, match_type)
    uncategorized = rules.preview_match_count(
        conn, pattern, sign, match_type, uncategorized_only=True
    )
    # The row being saved becomes manual, so it is not one of the "others".
    if txn["category_source"] is None and rules.pattern_matches(
        match_type, pattern, txn["description"]
    ):
        uncategorized -= 1
    return RulePreview(match_type, total, max(uncategorized, 0))


# --- saving ------------------------------------------------------------------------


def validate_month(month: str | None) -> str | None:
    """'YYYY-MM' or None (blank); ValueError otherwise."""
    if month is None or not str(month).strip():
        return None
    month = str(month).strip()
    if not _MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    return month


def set_applies_month(conn: sqlite3.Connection, txn_id: int, month: str | None) -> None:
    """Set (or clear, with None/blank) the 'applies to month' override.

    Does not change tags or the existing split; the month only affects which
    people a default split starts from and which month reports count it in.
    """
    month = validate_month(month)
    get_transaction(conn, txn_id)
    with db.transaction(conn):
        conn.execute("UPDATE transactions SET applies_month = ? WHERE id = ?", (month, txn_id))


def _tag(conn, txn_id, person_id, purpose_id, applies_month) -> None:
    """Mark the row manual (callers wrap the whole save in a transaction)."""
    conn.execute(
        "UPDATE transactions SET person_id = ?, purpose_id = ?, category_source = 'manual'"
        " WHERE id = ?",
        (person_id, purpose_id, txn_id),
    )
    if applies_month is not KEEP:
        set_applies_month(conn, txn_id, applies_month)


def _remember(conn, txn_id, pattern, *, person_id=None, purpose_id=None) -> SaveResult:
    rule_id, result = rules.create_rule_from_transaction(
        conn, txn_id, pattern=pattern, person_id=person_id, purpose_id=purpose_id
    )
    # replace=True inside is safe: create_rule only tags uncategorized rows,
    # so any split rows they had are stale, never a manual split.
    splits.sync_after_rules(conn, result)
    return SaveResult(rule_id, len(result.updated), list(result.updated))


def _check_purpose(conn, purpose_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM purposes WHERE id = ?", (purpose_id,)).fetchone()
    if row is None:
        raise ValueError("pick a purpose")
    return row


def _save_purposed(
    conn, txn_id, purpose_id, weights, remember_split, remember, pattern, applies_month
) -> SaveResult:
    purpose = _check_purpose(conn, purpose_id)
    with db.transaction(conn):
        before = get_transaction(conn, txn_id)
        _tag(conn, txn_id, None, purpose_id, applies_month)
        if purpose["excluded"]:
            splits.clear_split(conn, txn_id)
        elif weights is None:
            splits.apply_default_split(conn, [txn_id], replace=True)
        else:
            _save_split(conn, before, purpose_id, weights, remember_split)
        if remember:
            return _remember(conn, txn_id, pattern, purpose_id=purpose_id)
    return SaveResult()


def _save_split(conn, before, purpose_id, weights, remember_split) -> None:
    """Store editor ``weights`` for a row being tagged with ``purpose_id``.

    Split editors always send their weights, edited or not, so the source is
    worked out from what they match (compared as exact proportions):

    * the row keeps its purpose and the weights equal its stored split ->
      left exactly as it is (same rows, same source), and confirmed if it was
      pending review;
    * the weights equal the purpose's default for the row's month -> stored
      with source 'default';
    * anything else -> 'manual' (set by hand).

    ``remember_split`` makes the weights the purpose default either way.
    """
    txn_id = before["id"]
    if before["purpose_id"] == purpose_id:
        stored = [
            (r["person_id"], r["weight"]) for r in splits.get_transaction_split(conn, txn_id)
        ]
        if stored and splits.same_split(stored, weights):
            splits.confirm_split(conn, txn_id)
            if remember_split:
                splits.set_purpose_split(conn, purpose_id, weights)
            return
    month = splits.transaction_month(get_transaction(conn, txn_id))  # after applies_month
    default = splits.default_split_for(conn, purpose_id, month)
    source = splits.DEFAULT if splits.same_split(default, weights) else splits.MANUAL
    splits.set_transaction_split(
        conn, txn_id, weights, remember_for_purpose=remember_split, source=source
    )


def save_person(
    conn: sqlite3.Connection,
    txn_id: int,
    person_id: int,
    *,
    remember: bool = False,
    pattern: str | None = None,
    applies_month=KEEP,
) -> SaveResult:
    """Tag a transaction to a person (manual); removes any split.

    A deposit is money they paid in; a withdrawal is money paid to them, so it
    reduces what they have paid in.

    ``remember``: also create a rule from ``pattern`` (default: the full
    description) and apply it to other uncategorized matching rows of the
    same sign.
    """
    get_transaction(conn, txn_id)
    if conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone() is None:
        raise ValueError("pick a person")
    with db.transaction(conn):
        _tag(conn, txn_id, person_id, None, applies_month)
        splits.clear_split(conn, txn_id)
        if remember:
            return _remember(conn, txn_id, pattern, person_id=person_id)
    return SaveResult()


def save_refund(
    conn: sqlite3.Connection,
    txn_id: int,
    purpose_id: int,
    weights: Weights | None = None,
    *,
    remember_split: bool = False,
    remember: bool = False,
    pattern: str | None = None,
    applies_month=KEEP,
) -> SaveResult:
    """Tag a deposit as a refund for ``purpose_id``; its split reduces shares.

    ``weights`` None -> the purpose's default split; otherwise the given split
    (zero weights dropped), saved as the purpose default if ``remember_split``.
    """
    txn = get_transaction(conn, txn_id)
    if txn["amount_cents"] <= 0:
        raise ValueError("a refund is a deposit")
    return _save_purposed(
        conn, txn_id, purpose_id, weights, remember_split, remember, pattern, applies_month
    )


def save_withdrawal(
    conn: sqlite3.Connection,
    txn_id: int,
    purpose_id: int,
    weights: Weights | None = None,
    *,
    remember_split: bool = False,
    remember: bool = False,
    pattern: str | None = None,
    applies_month=KEEP,
) -> SaveResult:
    """Tag a withdrawal with a purpose and split (manual).

    ``weights`` None -> the purpose's default split; otherwise the given split,
    saved as the purpose default if ``remember_split`` (past withdrawals keep
    theirs). Excluded purposes (Transfer) get no split.
    """
    txn = get_transaction(conn, txn_id)
    if txn["amount_cents"] > 0:
        raise ValueError("this is a deposit; use save_person or save_refund")
    return _save_purposed(
        conn, txn_id, purpose_id, weights, remember_split, remember, pattern, applies_month
    )


def clear_tags(conn: sqlite3.Connection, txn_id: int) -> None:
    """Back to uncategorized: no person, purpose, split or category_source.

    The row is then eligible for rules again (the next import or Remember may
    tag it). The applies_month override is kept.
    """
    get_transaction(conn, txn_id)
    with db.transaction(conn):
        conn.execute(
            "UPDATE transactions SET person_id = NULL, purpose_id = NULL,"
            " category_source = NULL WHERE id = ?",
            (txn_id,),
        )
        splits.clear_split(conn, txn_id)


# --- split editor helpers --------------------------------------------------------


def split_candidates(
    conn: sqlite3.Connection, month: str, current: Weights = ()
) -> list[sqlite3.Row]:
    """People offered in a split editor: those active in ``month`` (housemates
    by name, then the House), plus anyone already in ``current``."""
    in_current = {pid for pid, _ in current}
    people = [
        p for p in db.list_people(conn)
        if p["is_house"] or db.is_active(p, month) or p["id"] in in_current
    ]
    return sorted(people, key=lambda p: (p["is_house"], p["name"].casefold()))


def split_origin(
    conn: sqlite3.Connection, txn: sqlite3.Row, purpose_id: int | None
) -> str | None:
    """Where the split editor's starting weights (:func:`initial_weights`) come
    from: the stored split's source ('default' or 'manual') when the editor
    shows the stored split, else None (it starts from the purpose default)."""
    if purpose_id is not None and purpose_id == txn["purpose_id"]:
        if splits.get_transaction_split(conn, txn["id"]):
            return splits.get_split_source(conn, txn["id"]) or splits.MANUAL
    return None


def initial_weights(
    conn: sqlite3.Connection, txn: sqlite3.Row, purpose_id: int | None
) -> list[tuple[int, float]]:
    """Weights the split editor starts from: the stored split when the purpose
    is the row's current one, else the purpose's default for the row's month
    (an even split among active housemates for a purpose not yet created)."""
    month = splits.transaction_month(txn)
    if purpose_id is not None and purpose_id == txn["purpose_id"]:
        stored = splits.get_transaction_split(conn, txn["id"])
        if stored:
            return [(r["person_id"], r["weight"]) for r in stored]
    if purpose_id is None:
        return [(p["id"], 1.0) for p in db.active_people(conn, month)]
    return splits.default_split_for(conn, purpose_id, month)


def left_out_for_editor(
    conn: sqlite3.Connection, txn: sqlite3.Row | dict, purpose_id: int | None
) -> list[tuple[str, float]]:
    """``[(name, percent)]`` of the people the split editor's starting weights
    (:func:`initial_weights`) leave out of the purpose default because they
    are not active in the row's month; percent is their share of the default.
    For a stored split that is the record kept when it was written (empty once
    confirmed); otherwise it is worked out from the purpose's current default.
    """
    if purpose_id is None:
        return []
    if split_origin(conn, txn, purpose_id) is not None:
        kept = [(r["person_id"], r["weight"]) for r in splits.get_transaction_split(conn, txn["id"])]
        left = [(r["person_id"], r["weight"]) for r in splits.get_left_out(conn, txn["id"])]
    else:
        kept, left = splits.default_split_detail(conn, purpose_id, splits.transaction_month(txn))
    if not left:
        return []
    names = {p["id"]: p["name"] for p in db.list_people(conn)}
    percents = splits.to_percents(list(kept) + list(left))
    return [(names.get(pid, str(pid)), percents[pid]) for pid, _ in left]
