"""Remembered rules: match transaction descriptions and fill in person/purpose.

A rule carries a target: ``person_id`` (a deposit they sent, or a withdrawal
paid to them) or ``purpose_id`` (a withdrawal, or a refund deposit). Applying a rule copies both columns from
the rule onto the transaction (so exactly one of them ends up set) and marks
it ``category_source = 'rule'``. Rows tagged ``manual`` are never touched.

Rules never compute splits. Every function that changes rows returns an
``ApplyResult``; callers (ingest, UI) should pass ``purpose_changed`` to the
split layer so those rows get their purpose's default split (or, when the
purpose became NULL, have their splits cleared).

Matching (spec "Categorization and remembered rules"):

- exact: description equals the pattern after whitespace collapse (case kept).
- contains: pattern is a case-insensitive substring (both whitespace-collapsed).
- A rule only matches transactions of its ``applies_to`` sign.
- Precedence: exact first, then the longest contains pattern, then newest.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from housefin import db
from housefin.parsers.base import normalize_description

EXACT = "exact"
CONTAINS = "contains"
DEPOSIT = "deposit"
WITHDRAWAL = "withdrawal"


@dataclass(frozen=True)
class Rule:
    id: int
    match_type: str
    pattern: str
    applies_to: str
    person_id: int | None
    purpose_id: int | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Rule:
        return cls(
            id=row["id"],
            match_type=row["match_type"],
            pattern=row["pattern"],
            applies_to=row["applies_to"],
            person_id=row["person_id"],
            purpose_id=row["purpose_id"],
            created_at=row["created_at"],
        )

    def matches(self, description: str, amount_cents: int) -> bool:
        if sign_of(amount_cents) != self.applies_to:
            return False
        return pattern_matches(self.match_type, self.pattern, description)

    def specificity(self) -> tuple:
        """Sort key; the highest value wins among matching rules."""
        is_exact = self.match_type == EXACT
        length = 0 if is_exact else len(normalize_description(self.pattern))
        return (is_exact, length, self.created_at, self.id)


@dataclass
class ApplyResult:
    """Transactions changed by a rule operation.

    ``updated``: every row whose person/purpose/category_source changed.
    ``purpose_changed``: subset whose purpose_id changed (including to or from
    NULL); these need their split (re)computed or cleared by splits.py.
    ``cleared``: subset that became uncategorized (no rule matches any more).
    """

    updated: list[int] = field(default_factory=list)
    purpose_changed: list[int] = field(default_factory=list)
    cleared: list[int] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.updated)

    def extend(self, other: ApplyResult) -> None:
        for name in ("updated", "purpose_changed", "cleared"):
            mine = getattr(self, name)
            mine.extend(i for i in getattr(other, name) if i not in mine)


# --- pure helpers ------------------------------------------------------------


def sign_of(amount_cents: int) -> str:
    """'deposit' for money in (> 0), 'withdrawal' for money out (<= 0)."""
    return DEPOSIT if amount_cents > 0 else WITHDRAWAL


def pattern_matches(match_type: str, pattern: str, description: str) -> bool:
    pat = normalize_description(pattern)
    desc = normalize_description(description)
    if not pat:
        return False
    if match_type == EXACT:
        return desc == pat
    if match_type == CONTAINS:
        return pat.casefold() in desc.casefold()
    raise ValueError(f"unknown match_type {match_type!r}")


def match_type_for(description: str, pattern: str) -> str:
    """Match type implied by the Remember pattern field: the full description
    (after whitespace collapse) is exact; anything else is contains."""
    same = normalize_description(pattern) == normalize_description(description)
    return EXACT if same else CONTAINS


def best_rule(rules: Iterable[Rule], description: str, amount_cents: int) -> Rule | None:
    """The most specific matching rule, or None."""
    matching = [r for r in rules if r.matches(description, amount_cents)]
    return max(matching, key=Rule.specificity, default=None)


# --- rule CRUD ---------------------------------------------------------------


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Rule | None:
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return Rule.from_row(row) if row else None


def all_rules(conn: sqlite3.Connection) -> list[Rule]:
    return [Rule.from_row(r) for r in conn.execute("SELECT * FROM rules ORDER BY id")]


def _validate(match_type: str, pattern: str, applies_to: str, person_id, purpose_id) -> str:
    if match_type not in (EXACT, CONTAINS):
        raise ValueError(f"unknown match_type {match_type!r}")
    if applies_to not in (DEPOSIT, WITHDRAWAL):
        raise ValueError(f"unknown applies_to {applies_to!r}")
    pattern = normalize_description(pattern)
    if not pattern:
        raise ValueError("pattern is empty")
    if (person_id is None) == (purpose_id is None):
        raise ValueError("a rule sets exactly one of person_id or purpose_id")
    return pattern


def _same_key(rule: Rule, match_type: str, pattern: str, applies_to: str) -> bool:
    if rule.match_type != match_type or rule.applies_to != applies_to:
        return False
    a, b = normalize_description(rule.pattern), pattern
    return a == b if match_type == EXACT else a.casefold() == b.casefold()


def create_rule(
    conn: sqlite3.Connection,
    pattern: str,
    applies_to: str,
    *,
    match_type: str = EXACT,
    person_id: int | None = None,
    purpose_id: int | None = None,
    apply: bool = True,
) -> tuple[int, ApplyResult]:
    """Create a rule (pattern stored whitespace-collapsed) and, by default,
    apply it to every uncategorized row it matches.

    If a rule with the same match type, pattern and sign already exists, its
    target is replaced instead of adding a duplicate (same id returned).
    Returns (rule_id, ApplyResult for the uncategorized rows it tagged).
    """
    pattern = _validate(match_type, pattern, applies_to, person_id, purpose_id)
    existing = next(
        (r for r in all_rules(conn) if _same_key(r, match_type, pattern, applies_to)), None
    )
    with db.transaction(conn):
        if existing:
            rule_id = existing.id
            conn.execute(
                "UPDATE rules SET person_id = ?, purpose_id = ? WHERE id = ?",
                (person_id, purpose_id, rule_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO rules (match_type, pattern, applies_to, person_id, purpose_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (match_type, pattern, applies_to, person_id, purpose_id),
            )
            rule_id = cur.lastrowid
        result = apply_rules(conn) if apply else ApplyResult()
    return rule_id, result


def create_rule_from_transaction(
    conn: sqlite3.Connection,
    transaction_id: int,
    *,
    pattern: str | None = None,
    person_id: int | None = None,
    purpose_id: int | None = None,
    match_type: str | None = None,
    apply: bool = True,
) -> tuple[int, ApplyResult]:
    """Remember: rule whose applies_to comes from the source transaction's sign.

    ``pattern`` defaults to the full description; ``match_type`` defaults to
    exact when the pattern is the full description, else contains.
    The source row itself should already be saved as manual by the caller.
    """
    row = conn.execute(
        "SELECT description, amount_cents FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    if row is None:
        raise KeyError(transaction_id)
    if pattern is None:
        pattern = row["description"]
    if match_type is None:
        match_type = match_type_for(row["description"], pattern)
    return create_rule(
        conn,
        pattern,
        sign_of(row["amount_cents"]),
        match_type=match_type,
        person_id=person_id,
        purpose_id=purpose_id,
        apply=apply,
    )


def update_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    *,
    pattern: str | None = None,
    match_type: str | None = None,
    person_id: int | None = None,
    purpose_id: int | None = None,
    reapply: bool = False,
) -> ApplyResult:
    """Edit a rule. Omitted pattern/match_type keep their values; the target is
    replaced only when person_id or purpose_id is given (pass one of them).

    With ``reapply``, every non-manual row that matched the old or matches the
    new version is re-evaluated against all rules (rule-tagged rows that no
    longer match any rule become uncategorized; uncategorized ones that now
    match get tagged). Without it no transaction is changed.
    """
    old = get_rule(conn, rule_id)
    if old is None:
        raise KeyError(rule_id)
    new_type = match_type or old.match_type
    new_pattern = old.pattern if pattern is None else pattern
    if person_id is None and purpose_id is None:
        person_id, purpose_id = old.person_id, old.purpose_id
    new_pattern = _validate(new_type, new_pattern, old.applies_to, person_id, purpose_id)
    with db.transaction(conn):
        conn.execute(
            "UPDATE rules SET match_type = ?, pattern = ?, person_id = ?, purpose_id = ?"
            " WHERE id = ?",
            (new_type, new_pattern, person_id, purpose_id, rule_id),
        )
        if not reapply:
            return ApplyResult()
        new = get_rule(conn, rule_id)
        return _reevaluate(conn, [old, new])


def delete_rule(conn: sqlite3.Connection, rule_id: int, *, reapply: bool = False) -> ApplyResult:
    """Delete a rule. With ``reapply``, rule-tagged rows it matched are
    re-evaluated against the remaining rules (or become uncategorized)."""
    old = get_rule(conn, rule_id)
    if old is None:
        raise KeyError(rule_id)
    with db.transaction(conn):
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return _reevaluate(conn, [old]) if reapply else ApplyResult()


def reapply_rule(conn: sqlite3.Connection, rule_id: int) -> ApplyResult:
    """Re-evaluate every non-manual row this rule matches."""
    rule = get_rule(conn, rule_id)
    if rule is None:
        raise KeyError(rule_id)
    return _reevaluate(conn, [rule])


# --- applying ----------------------------------------------------------------


def find_best_rule(
    conn: sqlite3.Connection, description: str, amount_cents: int
) -> Rule | None:
    return best_rule(all_rules(conn), description, amount_cents)


def apply_rules(
    conn: sqlite3.Connection, transaction_ids: Iterable[int] | None = None
) -> ApplyResult:
    """Tag rows from the best matching rule.

    With no ids, every uncategorized row is considered. With ids (e.g. rows
    just imported), those rows are considered unless tagged manual; a
    rule-tagged row is re-tagged if a different rule now wins, but is left
    alone if no rule matches. Rows with no match stay as they are.
    """
    if transaction_ids is None:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE category_source IS NULL"
        ).fetchall()
    else:
        rows = _rows_by_id(conn, transaction_ids)
    return _retag(conn, rows, all_rules(conn), clear_unmatched=False)


def _reevaluate(conn: sqlite3.Connection, rules: list[Rule]) -> ApplyResult:
    """Re-tag non-manual rows matched by any of ``rules`` using all current
    rules; rule-tagged rows left with no match become uncategorized."""
    candidates = [
        row
        for row in conn.execute(
            "SELECT * FROM transactions WHERE category_source IS NULL OR category_source = 'rule'"
        )
        if any(r.matches(row["description"], row["amount_cents"]) for r in rules)
    ]
    return _retag(conn, candidates, all_rules(conn), clear_unmatched=True)


def _rows_by_id(conn: sqlite3.Connection, ids: Iterable[int]) -> list[sqlite3.Row]:
    ids = list(dict.fromkeys(ids))
    rows: list[sqlite3.Row] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows += conn.execute(f"SELECT * FROM transactions WHERE id IN ({marks})", chunk).fetchall()
    return rows


def _retag(
    conn: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
    rules: list[Rule],
    *,
    clear_unmatched: bool,
) -> ApplyResult:
    result = ApplyResult()
    with db.transaction(conn):
        for row in rows:
            if row["category_source"] == "manual":
                continue
            rule = best_rule(rules, row["description"], row["amount_cents"])
            if rule is not None:
                target = (rule.person_id, rule.purpose_id, "rule")
            elif clear_unmatched and row["category_source"] == "rule":
                target = (None, None, None)
            else:
                continue
            current = (row["person_id"], row["purpose_id"], row["category_source"])
            if target == current:
                continue
            conn.execute(
                "UPDATE transactions SET person_id = ?, purpose_id = ?, category_source = ?"
                " WHERE id = ?",
                (*target, row["id"]),
            )
            result.updated.append(row["id"])
            if target[1] != row["purpose_id"]:
                result.purpose_changed.append(row["id"])
            if target[2] is None:
                result.cleared.append(row["id"])
    return result


# --- previews and listings ---------------------------------------------------


def preview_match_count(
    conn: sqlite3.Connection,
    pattern: str,
    applies_to: str,
    match_type: str = EXACT,
    *,
    uncategorized_only: bool = False,
) -> int:
    """How many existing rows of that sign the pattern would match."""
    sql = "SELECT description, amount_cents FROM transactions"
    if uncategorized_only:
        sql += " WHERE category_source IS NULL"
    return sum(
        1
        for row in conn.execute(sql)
        if sign_of(row["amount_cents"]) == applies_to
        and pattern_matches(match_type, pattern, row["description"])
    )


@dataclass(frozen=True)
class RuleSummary:
    rule: Rule
    match_count: int  # rows of the rule's sign whose description it matches
    target_name: str | None  # person or purpose name


def list_rules(conn: sqlite3.Connection) -> list[RuleSummary]:
    """Every rule with its match count, newest first."""
    rows = conn.execute("SELECT description, amount_cents FROM transactions").fetchall()
    people = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM people")}
    purposes = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM purposes")}
    out = []
    for rule in sorted(all_rules(conn), key=lambda r: (r.created_at, r.id), reverse=True):
        count = sum(1 for row in rows if rule.matches(row["description"], row["amount_cents"]))
        name = people.get(rule.person_id) if rule.person_id else purposes.get(rule.purpose_id)
        out.append(RuleSummary(rule, count, name))
    return out
