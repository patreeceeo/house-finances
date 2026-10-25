"""Balance matching: a person's deposits grouped with the shares they pay for.

Pure functions over the DB (no Streamlit), used by the Balances page's
"Match deposits to shares" section.

A person's *items* are exactly what their Running balance covers (see
:mod:`housefin.balances`): all time through the end of the latest selected
month, Transfers left out. There are two sides:

* ``'paid'`` - a transaction tagged with the person (Paid in). ``cents`` is
  ``amount_cents`` in bank sign: deposits positive, withdrawals paid to the
  person negative.
* ``'share'`` - the person's split row on a withdrawal or refund (Share of
  spending). ``cents`` is ``share_cents``: positive for withdrawals, negative
  for refunds.

One transaction can be on both sides for the same person, so an item is
identified by ``(transaction_id, side)`` (:attr:`Item.key`), the same key kept
groups store. Each item is in exactly one group, so the groups' differences
(paid - shares) add up to the running balance; :func:`check_groups` verifies
that. Everything is integer cents; nothing is rounded or compared against a
tolerance.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from housefin import balances, db

PAID = "paid"
SHARE = "share"

Side = Literal["paid", "share"]


@dataclass(frozen=True)
class Item:
    """One deposit/withdrawal (``'paid'``) or share (``'share'``) of a person.

    ``month`` is the effective month (``applies_month`` else the date's).
    ``amount_cents`` is the whole transaction in bank sign (for a share, the
    bill it is a share of; for paid it equals ``cents``). ``purpose`` is the
    purpose name for shares, None for paid items.
    """

    transaction_id: int
    side: Side
    date: str
    month: str
    description: str
    cents: int
    amount_cents: int
    purpose: str | None = None

    @property
    def key(self) -> tuple[int, str]:
        return (self.transaction_id, self.side)


def _order(item: Item) -> tuple:
    """Date order; ties broken by transaction id, then side."""
    return (item.date, item.transaction_id, item.side)


@dataclass(frozen=True)
class Group:
    """Items that pay for each other. ``months`` are the share months it covers
    (sorted); a group with deposits but no months is "Paid ahead".

    A *suggested* group has ``id`` None. A *kept* group (see
    :func:`groups_for`) carries its ``match_groups`` row: ``id``, ``note`` and
    the totals recorded when it was last kept. Its ``items`` are only those in
    the selected range; ``later_items`` are its items after the latest
    selected month, not shown and not counted in the totals.
    """

    items: tuple[Item, ...]
    months: tuple[str, ...]
    id: int | None = None
    note: str | None = None
    kept_paid_cents: int | None = None
    kept_share_cents: int | None = None
    later_items: tuple[Item, ...] = ()

    @property
    def kept(self) -> bool:
        return self.id is not None

    @property
    def key(self) -> str:
        """How the UI names this group in events: ``'k:<id>'`` for a kept
        group, ``'s:'`` plus its items' keys for a suggestion (so an event
        about a suggestion that has since changed is refused, not applied to
        different items)."""
        if self.kept:
            return f"k:{self.id}"
        return "s:" + ",".join(f"{i.transaction_id}-{i.side}" for i in self.items)

    @property
    def changed_since_kept(self) -> bool:
        """A kept group whose live totals (including ``later_items``) differ
        from the totals recorded when it was kept: a split was edited, an item
        went away. The old figures are ``kept_paid_cents`` /
        ``kept_share_cents``."""
        if not self.kept:
            return False
        every = self.items + self.later_items
        return (
            sum(i.cents for i in every if i.side == PAID) != self.kept_paid_cents
            or sum(i.cents for i in every if i.side == SHARE) != self.kept_share_cents
        )

    @property
    def paid_cents(self) -> int:
        return sum(i.cents for i in self.items if i.side == PAID)

    @property
    def share_cents(self) -> int:
        return sum(i.cents for i in self.items if i.side == SHARE)

    @property
    def difference_cents(self) -> int:
        """Paid - shares: positive = credit, negative = owed, 0 = balanced."""
        return self.paid_cents - self.share_cents

    @property
    def paid_ahead(self) -> bool:
        return not self.months


def _through(months: Iterable[str] | None) -> str | None:
    """The latest selected month (None/empty -> None, i.e. all time)."""
    months = set(months or ())
    return max(months) if months else None


def person_items(
    conn: sqlite3.Connection, person_id: int, months: Iterable[str] | None = None
) -> list[Item]:
    """The person's paid and share items behind their Running balance: all
    time through the end of the latest of ``months`` (None/empty = all time),
    Transfers excluded. Sorted by date, then transaction id, then side."""
    through = _through(months)
    items = [
        Item(
            transaction_id=r["id"],
            side=PAID,
            date=r["date"],
            month=r["month"],
            description=r["description"],
            cents=r["amount_cents"],
            amount_cents=r["amount_cents"],
        )
        for r in balances.paid_in_transactions(conn, person_id, through=through)
    ]
    items += [
        Item(
            transaction_id=r["id"],
            side=SHARE,
            date=r["date"],
            month=r["month"],
            description=r["description"],
            cents=r["share_cents"],
            amount_cents=r["amount_cents"],
            purpose=r["purpose"],
        )
        for r in balances.share_transactions(conn, person_id, through=through)
    ]
    return sorted(items, key=_order)


def suggest_groups(items: Iterable[Item]) -> list[Group]:
    """Suggested groups: deposits pay the oldest unpaid month first.

    * Shares are bunched by month.
    * Paid items, in date order (ties by transaction id), go into the open
      group; if there is none, a new group opens on the oldest month not yet
      reached (or with no month, if every month has been reached).
    * While the group's paid exceeds its shares, the next month joins it if
      that makes ``|paid - shares|`` strictly smaller (one deposit covering
      two months).
    * The group closes once it has a month and paid >= shares. A group still
      short stays open, so the next deposit joins it (a short month merges
      with the next).
    * Months no deposit reached become one group each (owed).
    * Deposits after the last month form a group with no months (paid ahead).

    Groups are returned in the order they were opened; the items in each are
    in date order.
    """
    items = list(items)
    pays = sorted((i for i in items if i.side == PAID), key=_order)
    by_month: dict[str, list[Item]] = {}
    for s in items:
        if s.side == SHARE:
            by_month.setdefault(s.month, []).append(s)
    months = sorted(by_month)
    month_cents = {m: sum(s.cents for s in by_month[m]) for m in months}

    groups: list[tuple[list[str], list[Item]]] = []
    nxt = 0  # index of the oldest month not yet in a group

    def add_month(group: tuple[list[str], list[Item]]) -> None:
        nonlocal nxt
        group[0].append(months[nxt])
        group[1].extend(by_month[months[nxt]])
        nxt += 1

    def totals(group: tuple[list[str], list[Item]]) -> tuple[int, int]:
        g_items = group[1]
        return (
            sum(i.cents for i in g_items if i.side == PAID),
            sum(i.cents for i in g_items if i.side == SHARE),
        )

    open_group = None
    for p in pays:
        if open_group is None:
            open_group = ([], [])
            groups.append(open_group)
            if nxt < len(months):
                add_month(open_group)
        open_group[1].append(p)
        while nxt < len(months):
            paid, share = totals(open_group)
            if paid <= share:
                break
            extra = month_cents[months[nxt]]
            if abs(paid - (share + extra)) < abs(paid - share):
                add_month(open_group)
            else:
                break
        paid, share = totals(open_group)
        if open_group[0] and paid >= share:
            open_group = None

    while nxt < len(months):
        group = ([], [])
        add_month(group)
        groups.append(group)

    return [
        Group(items=tuple(sorted(g_items, key=_order)), months=tuple(g_months))
        for g_months, g_items in groups
    ]


def running_balance_cents(
    conn: sqlite3.Connection, person_id: int, months: Iterable[str] | None = None
) -> int:
    """The person's Running balance for ``months``, computed like
    :func:`balances.balances`: paid in - shares, all time through the latest
    selected month."""
    through = _through(months)
    paid = balances.paid_in_totals(conn, through=through).get(person_id, 0)
    share = balances.share_totals(conn, through=through).get(person_id, 0)
    return paid - share


@dataclass(frozen=True)
class Check:
    """Sum of the groups' differences against the running balance."""

    groups_cents: int
    running_cents: int

    @property
    def ok(self) -> bool:
        return self.groups_cents == self.running_cents

    @property
    def off_by_cents(self) -> int:
        return self.groups_cents - self.running_cents


def check_groups(
    conn: sqlite3.Connection,
    person_id: int,
    months: Iterable[str] | None,
    groups: Sequence[Group],
) -> Check:
    """Compare the groups' differences, summed, with the person's running
    balance. ``ok`` False means the matching code is wrong; the page shows an
    error instead of the groups."""
    return Check(
        groups_cents=sum(g.difference_cents for g in groups),
        running_cents=running_balance_cents(conn, person_id, months),
    )


# --- kept groups ----------------------------------------------------------------
#
# Only kept groups are stored (match_groups / match_group_items); what a group
# shows is always read live through person_items. A stored item that no longer
# belongs to the person (re-tagged, left out of a split) is ignored; a kept
# group with no live items left is deleted. Suggestions run over the items in
# no kept group.
#
# The operations below take the person and selected months the page was drawn
# with, plus group keys (Group.key) and item keys (Item.key) from the UI. They
# re-derive the groups first and refuse, with StaleEdit, an event naming a
# group or item that is no longer there.

NEW = "new"  # move() target: put the items in a group of their own


class StaleEdit(LookupError):
    """An edit names a group or item that no longer exists as shown (the page
    is out of date). Nothing was changed."""


def _kept_rows(conn: sqlite3.Connection, person_id: int) -> dict[int, sqlite3.Row]:
    return {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, note, kept_paid_cents, kept_share_cents FROM match_groups"
            " WHERE person_id = ? ORDER BY id",
            (person_id,),
        )
    }


def _kept_items(conn: sqlite3.Connection, person_id: int) -> dict[tuple[int, str], int]:
    """Stored item key -> kept group id, for the person."""
    return {
        (r["transaction_id"], r["side"]): r["group_id"]
        for r in conn.execute(
            "SELECT group_id, transaction_id, side FROM match_group_items WHERE person_id = ?",
            (person_id,),
        )
    }


def _share_months(items: Iterable[Item]) -> tuple[str, ...]:
    return tuple(sorted({i.month for i in items if i.side == SHARE}))


def _delete_empty(
    conn: sqlite3.Connection, person_id: int, live: dict[tuple[int, str], Item]
) -> None:
    """Delete the person's kept groups with no live item (call inside a
    transaction)."""
    has_live = {g for k, g in _kept_items(conn, person_id).items() if k in live}
    for gid in _kept_rows(conn, person_id):
        if gid not in has_live:
            conn.execute("DELETE FROM match_groups WHERE id = ?", (gid,))


def groups_for(
    conn: sqlite3.Connection, person_id: int, months: Iterable[str] | None = None
) -> list[Group]:
    """The person's groups for ``months`` (all time through the latest one):
    their kept groups, built from live items, then suggestions over the
    remaining items; sorted by first item date (ties by key).

    Kept groups with no live items left are deleted here. A kept group with
    no items in the range (all after the latest month) is not shown."""
    with db.transaction(conn):
        live = {i.key: i for i in person_items(conn, person_id)}
        _delete_empty(conn, person_id, live)
        rows = _kept_rows(conn, person_id)
        stored = _kept_items(conn, person_id)
    through = _through(months)

    def in_range(i: Item) -> bool:
        return through is None or i.month <= through

    members: dict[int, list[Item]] = {gid: [] for gid in rows}
    for k, gid in stored.items():
        if k in live:
            members[gid].append(live[k])
    groups = []
    for gid, row in rows.items():
        shown = sorted((i for i in members[gid] if in_range(i)), key=_order)
        if not shown:
            continue
        groups.append(Group(
            items=tuple(shown),
            months=_share_months(shown),
            id=gid,
            note=row["note"],
            kept_paid_cents=row["kept_paid_cents"],
            kept_share_cents=row["kept_share_cents"],
            later_items=tuple(sorted((i for i in members[gid] if not in_range(i)), key=_order)),
        ))
    groups += suggest_groups(i for i in live.values() if i.key not in stored and in_range(i))
    return sorted(groups, key=lambda g: (_order(g.items[0]), g.key))


# --- editing (each runs in one db.transaction) ----------------------------------


def _find(groups: Sequence[Group], key: str) -> Group:
    for g in groups:
        if g.key == key:
            return g
    raise StaleEdit(f"Group {key!r} is no longer there; reload the page and try again.")


def _materialize(conn: sqlite3.Connection, person_id: int, group: Group) -> int:
    """Store a suggested group as kept (its current items); a kept group is
    returned as is. Returns the group id. Snapshot totals are set by
    :func:`_refresh`."""
    if group.kept:
        return group.id
    gid = conn.execute(
        "INSERT INTO match_groups (person_id, kept_paid_cents, kept_share_cents)"
        " VALUES (?, ?, ?)",
        (person_id, group.paid_cents, group.share_cents),
    ).lastrowid
    _add_items(conn, person_id, gid, (i.key for i in group.items))
    return gid


def _new_group(conn: sqlite3.Connection, person_id: int) -> int:
    return conn.execute(
        "INSERT INTO match_groups (person_id, kept_paid_cents, kept_share_cents)"
        " VALUES (?, 0, 0)",
        (person_id,),
    ).lastrowid


def _add_items(
    conn: sqlite3.Connection, person_id: int, gid: int, keys: Iterable[tuple[int, str]]
) -> None:
    conn.executemany(
        "INSERT INTO match_group_items (group_id, person_id, transaction_id, side)"
        " VALUES (?, ?, ?, ?)",
        [(gid, person_id, tid, side) for tid, side in keys],
    )


def _refresh(
    conn: sqlite3.Connection,
    person_id: int,
    gids: Iterable[int],
    live: dict[tuple[int, str], Item],
) -> None:
    """Re-keep groups after an edit: drop their stale items, record their live
    totals as the snapshot, and delete any left with no items."""
    stored = _kept_items(conn, person_id)
    for gid in set(gids):
        keys = [k for k, g in stored.items() if g == gid]
        for k in keys:
            if k not in live:
                conn.execute(
                    "DELETE FROM match_group_items WHERE person_id = ? AND transaction_id = ?"
                    " AND side = ?",
                    (person_id, *k),
                )
        items = [live[k] for k in keys if k in live]
        if not items:
            conn.execute("DELETE FROM match_groups WHERE id = ?", (gid,))
            continue
        conn.execute(
            "UPDATE match_groups SET kept_paid_cents = ?, kept_share_cents = ?,"
            " kept_at = datetime('now') WHERE id = ?",
            (
                sum(i.cents for i in items if i.side == PAID),
                sum(i.cents for i in items if i.side == SHARE),
                gid,
            ),
        )


def _live(conn: sqlite3.Connection, person_id: int) -> dict[tuple[int, str], Item]:
    return {i.key: i for i in person_items(conn, person_id)}


def keep(
    conn: sqlite3.Connection, person_id: int, months: Iterable[str] | None, group_key: str
) -> int:
    """Keep a suggested group as it stands, or keep a kept group again (its
    live totals become the snapshot, clearing "changed since kept"). Returns
    the group id."""
    months = list(months or ())
    with db.transaction(conn):
        group = _find(groups_for(conn, person_id, months), group_key)
        gid = _materialize(conn, person_id, group)
        _refresh(conn, person_id, [gid], _live(conn, person_id))
    return gid


def keep_all_balanced(
    conn: sqlite3.Connection, person_id: int, months: Iterable[str] | None
) -> list[int]:
    """Keep every suggestion whose difference is exactly 0. Returns the new
    group ids."""
    months = list(months or ())
    with db.transaction(conn):
        return [
            _materialize(conn, person_id, g)
            for g in groups_for(conn, person_id, months)
            if not g.kept and g.difference_cents == 0
        ]


def _item_keys(item_keys: Iterable[Sequence]) -> list[tuple[int, str]]:
    """Normalize keys from the UI (``[transaction_id, side]`` lists from JSON
    are fine)."""
    keys = []
    for k in item_keys:
        tid, side = k
        if side not in (PAID, SHARE):
            raise ValueError(f"Unknown item side {side!r}.")
        keys.append((int(tid), side))
    if not keys:
        raise ValueError("No items to move.")
    if len(set(keys)) != len(keys):
        raise ValueError("The same item was given twice.")
    return keys


def move(
    conn: sqlite3.Connection,
    person_id: int,
    months: Iterable[str] | None,
    item_keys: Iterable[Sequence],
    target_key: str,
) -> int:
    """Move items (keys as :attr:`Item.key`) into the group ``target_key``, or
    into a group of their own if it is :data:`NEW`. A suggested target, and
    every suggested group an item leaves, is kept first; kept groups left
    empty are deleted; the changed groups' snapshots are refreshed. Returns
    the target group id."""
    months = list(months or ())
    keys = _item_keys(item_keys)
    with db.transaction(conn):
        groups = groups_for(conn, person_id, months)
        owner = {i.key: g for g in groups for i in g.items}
        missing = [k for k in keys if k not in owner]
        if missing:
            raise StaleEdit(
                f"Item(s) {missing} are no longer shown; reload the page and try again."
            )
        target = None if target_key == NEW else _find(groups, target_key)
        sources = {owner[k].key: owner[k] for k in keys}
        if target is not None:
            sources.pop(target.key, None)
        tid = _new_group(conn, person_id) if target is None else _materialize(
            conn, person_id, target
        )
        source_ids = [_materialize(conn, person_id, s) for s in sources.values()]
        conn.executemany(
            "DELETE FROM match_group_items WHERE person_id = ? AND transaction_id = ?"
            " AND side = ?",
            [(person_id, *k) for k in keys],
        )
        _add_items(conn, person_id, tid, keys)
        _refresh(conn, person_id, [tid, *source_ids], _live(conn, person_id))
    return tid


def split_by_month(
    conn: sqlite3.Connection, person_id: int, months: Iterable[str] | None, group_key: str
) -> list[int]:
    """Break a group into one kept group per share month. Each paid item goes
    to its own month if the group has it, else to the latest earlier month in
    the group, else to the group's first month. The first month keeps the
    group's id and note. Returns the group ids in month order."""
    months = list(months or ())
    with db.transaction(conn):
        group = _find(groups_for(conn, person_id, months), group_key)
        items = group.items + group.later_items
        group_months = _share_months(items)
        if not group_months:
            raise ValueError("This group has no share months to split by.")
        parts: dict[str, list[Item]] = {m: [] for m in group_months}
        for i in items:
            if i.side == SHARE or i.month in parts:
                parts[i.month].append(i)
                continue
            earlier = [m for m in group_months if m <= i.month]
            parts[earlier[-1] if earlier else group_months[0]].append(i)
        first = _materialize(conn, person_id, group)
        conn.execute("DELETE FROM match_group_items WHERE group_id = ?", (first,))
        gids = [first] + [_new_group(conn, person_id) for _ in group_months[1:]]
        for gid, m in zip(gids, group_months):
            _add_items(conn, person_id, gid, (i.key for i in parts[m]))
        _refresh(conn, person_id, gids, _live(conn, person_id))
    return gids


def merge(
    conn: sqlite3.Connection,
    person_id: int,
    months: Iterable[str] | None,
    group_key: str,
    other_key: str,
) -> int:
    """Join two groups (normally a group and the one below it) into one kept
    group, which keeps the first's id. Notes are joined, first's first.
    Returns the group id."""
    months = list(months or ())
    if group_key == other_key:
        raise ValueError("Can't merge a group with itself.")
    with db.transaction(conn):
        groups = groups_for(conn, person_id, months)
        a, b = _find(groups, group_key), _find(groups, other_key)
        gid, other = _materialize(conn, person_id, a), _materialize(conn, person_id, b)
        conn.execute("UPDATE match_group_items SET group_id = ? WHERE group_id = ?", (gid, other))
        note = "\n".join(n for n in (a.note, b.note) if n) or None
        conn.execute("UPDATE match_groups SET note = ? WHERE id = ?", (note, gid))
        conn.execute("DELETE FROM match_groups WHERE id = ?", (other,))
        _refresh(conn, person_id, [gid], _live(conn, person_id))
    return gid


def set_note(
    conn: sqlite3.Connection,
    person_id: int,
    months: Iterable[str] | None,
    group_key: str,
    note: str | None,
) -> int:
    """Set a group's note (blank clears it). A suggestion is kept first, with
    its current totals as the snapshot; a kept group's snapshot is left as it
    is. Returns the group id."""
    months = list(months or ())
    note = (note or "").strip() or None
    with db.transaction(conn):
        group = _find(groups_for(conn, person_id, months), group_key)
        gid = _materialize(conn, person_id, group)
        conn.execute("UPDATE match_groups SET note = ? WHERE id = ?", (note, gid))
    return gid


def unkeep(
    conn: sqlite3.Connection, person_id: int, months: Iterable[str] | None, group_key: str
) -> None:
    """Back to suggestion: delete a kept group; its items return to the pool
    the suggestions run over."""
    months = list(months or ())
    with db.transaction(conn):
        group = _find(groups_for(conn, person_id, months), group_key)
        if not group.kept:
            raise ValueError("Only a kept group can go back to being a suggestion.")
        conn.execute("DELETE FROM match_groups WHERE id = ?", (group.id,))
