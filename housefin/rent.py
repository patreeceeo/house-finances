"""Rent: split by who slept in which room each month, and the rent editor.

For the purpose with ``split_by_rooms`` (Rent), each person's weight is the
sum, over the rooms they slept in that month, of room percent x nights; each
vacant span weighs room percent x vacant nights. All weights total 100 x the
days in the month. A fully occupied month is written as a default split
(:func:`splits.apply_default_split`). Any vacancy, or room percents that
don't total exactly 100%, leaves the payment with no split
(:data:`NEEDS_REVIEW_SQL`) until a person says who covers the vacant share:
the House or named people. Nothing is assigned automatically and nobody
else's share is scaled up.

How a chosen vacant assignment is stored: as an ordinary split whose weights
are the final shares in cents (as positive numbers), source 'manual'. Each
person's share is their room share (exactly as :func:`room_split` computed
it, vacant share counted as one entry) plus their piece of the vacant share;
several people taking the vacant share divide it evenly by the usual share
rules (:func:`splits.compute_shares`: round down, leftover cents one each,
rotating by transaction id). Because the weights total the payment,
compute_shares reproduces those shares to the cent, so what the editor
showed is exactly what is saved, and the shares total the full payment.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from housefin import dates, db, fmt, rooms, splits


# True for a transaction (alias ``t``) that is a rent payment with no split
# yet: its month had a vacancy (or unusable room percents) when it was tagged.
NEEDS_REVIEW_SQL = (
    "(EXISTS (SELECT 1 FROM purposes rp WHERE rp.id = t.purpose_id"
    " AND rp.split_by_rooms = 1 AND rp.excluded = 0)"
    " AND NOT EXISTS (SELECT 1 FROM splits rs WHERE rs.transaction_id = t.id))"
)

_VACANT_ID = -1  # stand-in id for the vacant share in compute_shares


def is_split_by_rooms(conn: sqlite3.Connection, purpose_id: int | None) -> bool:
    """Whether a purpose's split comes from room assignments (Rent)."""
    if purpose_id is None:
        return False
    row = conn.execute(
        "SELECT split_by_rooms FROM purposes WHERE id = ?", (purpose_id,)
    ).fetchone()
    return row is not None and bool(row["split_by_rooms"])


@dataclass
class RoomSplitPerson:
    """One person's line in a rent split: the room(s) they slept in that
    month, nights, weight (exact) and share in cents."""

    person_id: int
    name: str
    room: str  # room name; several joined with ", " if they moved rooms
    nights: int
    weight: Decimal
    share_cents: int


@dataclass
class RoomSplitVacancy:
    """One vacant span of a room: nights ``first``..``last`` (inclusive)."""

    room_id: int
    room: str
    first: str  # 'YYYY-MM-DD'
    last: str
    nights: int
    weight: Decimal
    amount_cents: int


@dataclass
class RoomSplit:
    """A rent amount split by rooms for one month (see :func:`room_split`).

    ``amount_cents`` and every share carry the sign they were given (pass
    ``-transaction.amount_cents`` for shares as stored; see the module
    docstring). With ``error`` set (room percents unusable) there are no rows.
    """

    month: str
    days_in_month: int
    amount_cents: int
    total_weight: Decimal  # 100 x days in month
    people: list[RoomSplitPerson] = field(default_factory=list)
    vacant: list[RoomSplitVacancy] = field(default_factory=list)
    error: str | None = None

    @property
    def vacant_weight(self) -> Decimal:
        return sum((v.weight for v in self.vacant), Decimal(0))

    @property
    def vacant_cents(self) -> int:
        return sum(v.amount_cents for v in self.vacant)

    @property
    def fully_occupied(self) -> bool:
        """True when this split can be written as is: valid percents, nobody
        missing on any night."""
        return self.error is None and bool(self.people) and not self.vacant

    def weights(self) -> list[tuple[int, float]]:
        """``[(person_id, weight)]`` for storing (floats of exact decimals)."""
        return [(p.person_id, float(p.weight)) for p in self.people]

    def warnings(self) -> list[str]:
        """User-facing reasons this split can't be written as is, e.g.
        'Attic vacant 11–31 Jul (420 of 3,100)'."""
        if self.error is not None:
            return [f"Rent can't be split by rooms: {self.error}"]
        out = [
            f"{v.room} vacant {fmt.day_span(v.first, v.last)}"
            f" ({fmt.weight(v.weight)} of {fmt.weight(self.total_weight)})"
            for v in self.vacant
        ]
        if not self.people and not self.vacant:
            out.append("Nobody was in a room this month.")
        return out


def room_split(
    conn: sqlite3.Connection, month: str, amount_cents: int, *, rotate: int = 0
) -> RoomSplit:
    """Split ``amount_cents`` by who was in each room in ``month`` ('YYYY-MM').

    Weights are exact decimals (room percent as entered x nights). Shares use
    :func:`compute_shares` over the people *and* one entry for the whole
    vacant share, so people's shares are exactly what they would be in the
    final split whoever ends up covering the vacancy; the vacant share is then
    divided over its spans the same way. Pass the transaction id as
    ``rotate`` to match the split that gets stored. Nothing is written.
    """
    first, last = dates.month_bounds(month)
    days = dates.nights(first, last)
    total = Decimal(100) * days
    error = rooms.percent_error(conn)
    if error is not None:
        return RoomSplit(month, days, amount_cents, total, error=error)

    occ = rooms.month_occupancy(conn, month)
    names = {p["id"]: p["name"] for p in db.list_people(conn)}
    person: dict[int, list] = {}  # pid -> [room names, nights, weight]
    vacant: list[RoomSplitVacancy] = []
    for room in occ.rooms:
        pct = Decimal(str(room.percent))
        for pid, n in sorted(room.days.items()):
            entry = person.setdefault(pid, [[], 0, Decimal(0)])
            entry[0].append(room.name)
            entry[1] += n
            entry[2] += pct * n
        for s, e in room.vacant:
            n = dates.nights(s, e)
            vacant.append(RoomSplitVacancy(room.room_id, room.name, s, e, n, pct * n, 0))

    weight_sum = sum((w for _, _, w in person.values()), Decimal(0))
    weight_sum += sum((v.weight for v in vacant), Decimal(0))
    if weight_sum != total:  # cannot happen with valid percents; never rescale
        return RoomSplit(
            month, days, amount_cents, total,
            error=f"room weights total {fmt.weight(weight_sum)}, not {fmt.weight(total)}.",
        )

    entries = [(pid, w) for pid, (_, _, w) in person.items()]
    vacant_weight = sum((v.weight for v in vacant), Decimal(0))
    if vacant_weight:
        entries.append((_VACANT_ID, vacant_weight))
    shares = dict(splits.compute_shares(amount_cents, entries, rotate=rotate)) if entries else {}
    if vacant_weight:
        by_span = splits.compute_shares(
            shares[_VACANT_ID], [(i, v.weight) for i, v in enumerate(vacant)], rotate=rotate
        )
        for i, cents in by_span:
            vacant[i].amount_cents = cents

    people = [
        RoomSplitPerson(pid, names.get(pid, str(pid)), ", ".join(rs), n, w, shares[pid])
        for pid, (rs, n, w) in person.items()
    ]
    people.sort(key=lambda p: (-p.weight, p.name))
    return RoomSplit(month, days, amount_cents, total, people, vacant)


# --- the rent editor ----------------------------------------------------------


@dataclass
class RentLine:
    """One person's line in a rent split: their room share plus any part of
    the vacant share they cover (cents, signed as stored)."""

    person_id: int
    name: str
    room_cents: int
    vacant_cents: int

    @property
    def total_cents(self) -> int:
        return self.room_cents + self.vacant_cents


@dataclass
class RentPlan:
    """What a rent payment's split would be (see :func:`plan`).

    ``error`` says why it can't be saved yet (room percents unusable, nobody
    in a room, or a vacant share nobody covers); ``weights`` is empty then.
    """

    split: RoomSplit
    vacant_to: list[int]
    lines: list[RentLine]
    weights: list[tuple[int, float]]
    error: str | None = None

    @property
    def needs_vacant_choice(self) -> bool:
        return self.split.error is None and bool(self.split.vacant)

    @property
    def source(self) -> str:
        """The split source saving this plan records."""
        return splits.DEFAULT if self.split.fully_occupied else splits.MANUAL


def plan(
    conn: sqlite3.Connection,
    txn: sqlite3.Row | dict,
    month: str | None = None,
    vacant_to: list[int] | tuple[int, ...] = (),
) -> RentPlan:
    """The rent split for transaction ``txn`` in ``month`` (default: its
    transaction month), with the vacant share (if any) covered by the people
    in ``vacant_to`` (person ids; the House allowed). Nothing is written.

    See the module docstring for how the result is stored.
    """
    month = month or dates.month_of(txn)
    rs = room_split(conn, month, -txn["amount_cents"], rotate=txn["id"])
    vacant_to = list(dict.fromkeys(int(p) for p in vacant_to))
    if rs.error is not None:
        return RentPlan(rs, vacant_to, [], [], rs.warnings()[0])
    lines = {p.person_id: RentLine(p.person_id, p.name, p.share_cents, 0) for p in rs.people}
    if not rs.people and not rs.vacant:
        return RentPlan(rs, vacant_to, [], [], "Nobody was in a room this month.")
    if not rs.vacant:
        return RentPlan(rs, vacant_to, list(lines.values()), rs.weights())
    if not vacant_to:
        return RentPlan(
            rs, vacant_to, list(lines.values()), [],
            f"Choose who covers the vacant share ({fmt.cents(rs.vacant_cents)}): "
            "the House or named people.",
        )
    names = {p["id"]: p["name"] for p in db.list_people(conn)}
    unknown = [pid for pid in vacant_to if pid not in names]
    if unknown:
        raise ValueError(f"unknown person id(s) {unknown}")
    pieces = splits.compute_shares(rs.vacant_cents, [(pid, 1) for pid in vacant_to],
                                   rotate=txn["id"])
    for pid, cents in pieces:
        line = lines.setdefault(pid, RentLine(pid, names[pid], 0, 0))
        line.vacant_cents += cents
    ordered = list(lines.values())
    weights = [(ln.person_id, float(abs(ln.total_cents))) for ln in ordered if ln.total_cents]
    return RentPlan(rs, vacant_to, ordered, weights)


def comparison(
    conn: sqlite3.Connection, txn_id: int, new: RentPlan
) -> list[tuple[str, int | None, int | None]]:
    """``[(name, old_share_cents, new_share_cents)]`` for "Recalculate from
    rooms": the stored split next to ``new`` (None = not in that split).
    People in the new split first (its order), then anyone only in the old."""
    old = {r["person_id"]: (r["name"], r["share_cents"])
           for r in splits.get_transaction_split(conn, txn_id)}
    out = []
    for ln in new.lines:
        if ln.total_cents or ln.person_id in old:
            out.append((ln.name, old.pop(ln.person_id, (None, None))[1], ln.total_cents))
    out += [(name, cents, None) for name, cents in old.values()]
    return out


def recalculate(
    conn: sqlite3.Connection, txn_id: int, vacant_to: list[int] | tuple[int, ...] = ()
) -> RentPlan:
    """Replace a rent payment's stored split with its split from rooms now
    (one payment, on a person's confirmation). ``vacant_to`` covers any
    vacant share; ValueError if the split can't be made (see :func:`plan`).
    Tags and the applies-to month are unchanged. Returns the plan saved."""
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if txn is None:
        raise KeyError(txn_id)
    if not is_split_by_rooms(conn, txn["purpose_id"]):
        raise ValueError("only a rent payment can be recalculated from rooms")
    result = plan(conn, txn, None, vacant_to)
    if result.error:
        raise ValueError(result.error)
    splits.set_transaction_split(conn, txn_id, result.weights, source=result.source)
    return result
