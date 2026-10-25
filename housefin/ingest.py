"""Import bank CSV exports into the database with deduplication.

Per file: hash the bytes (skip files imported before), parse, insert rows
whose fingerprint is new, apply remembered rules and purpose default splits
to the inserted rows, and record an ``imports`` row. Each file is one
database transaction: an error part-way leaves nothing from that file.

Deduplication is count-based: if a fingerprint occurs n times in a file and
the database already stores k copies, max(0, n - k) copies are inserted with
occurrence numbers k+1..n. Overlapping exports never double a row, while
genuinely identical rows inside one file are all kept.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import db, rules, splits
from .parsers import ParsedRow, ParseFormatError, RowError, detect_parser
from .parsers.base import normalize_description
from housefin import fmt

IMPORTED = "imported"
SKIPPED = "skipped"  # same file content imported before
FAILED = "failed"


@dataclass(frozen=True)
class NearDuplicate:
    """Rows sharing date and amount but with different (normalized) text."""

    date: str
    amount_cents: int
    new_descriptions: tuple[str, ...]  # text of rows inserted by this import
    other_descriptions: tuple[str, ...]  # differing text already in the DB / file


@dataclass
class FileReport:
    path: str
    status: str = FAILED
    message: str = ""
    file_hash: str = ""
    rows_read: int = 0  # data rows, including bad ones
    rows_new: int = 0
    rows_duplicate: int = 0
    bad_rows: list[RowError] = field(default_factory=list)
    auto_categorized: int = 0
    near_duplicates: list[NearDuplicate] = field(default_factory=list)
    new_ids: list[int] = field(default_factory=list)


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def previous_import(conn: sqlite3.Connection, file_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM imports WHERE file_hash = ? ORDER BY id LIMIT 1", (file_hash,)
    ).fetchone()


def _stored_count(conn: sqlite3.Connection, fp: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(occurrence), 0) FROM transactions WHERE fingerprint = ?",
        (fp,),
    ).fetchone()
    return max(row[0], row[1])


def _insert_rows(
    conn: sqlite3.Connection, rows: list[ParsedRow], source_file: str
) -> tuple[list[int], int]:
    """Insert the copies not yet stored. Returns (new ids, duplicate count)."""
    by_fp: dict[str, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        by_fp[r.fingerprint].append(r)
    new_ids: list[int] = []
    duplicates = 0
    for fp, copies in by_fp.items():
        stored = _stored_count(conn, fp)
        duplicates += min(stored, len(copies))
        for occurrence in range(stored + 1, len(copies) + 1):
            r = copies[occurrence - 1]
            cur = conn.execute(
                "INSERT INTO transactions (fingerprint, occurrence, date, time, amount_cents,"
                " type, description, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fp,
                    occurrence,
                    r.date.isoformat(),
                    r.time.strftime("%H:%M:%S"),
                    r.amount_cents,
                    r.type,
                    r.description,
                    source_file,
                ),
            )
            new_ids.append(cur.lastrowid)
    return new_ids, duplicates


def find_near_duplicates(conn: sqlite3.Connection, new_ids: Iterable[int]) -> list[NearDuplicate]:
    """Groups (date, amount) touched by ``new_ids`` whose rows in the DB (new
    and old alike) carry more than one normalized description."""
    new_ids = set(new_ids)
    keys: dict[tuple[str, int], None] = {}
    for txn_id in sorted(new_ids):
        row = conn.execute(
            "SELECT date, amount_cents FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()
        keys[(row["date"], row["amount_cents"])] = None
    found = []
    for date, amount in keys:
        group = conn.execute(
            "SELECT id, description FROM transactions WHERE date = ? AND amount_cents = ?"
            " ORDER BY id",
            (date, amount),
        ).fetchall()
        texts = {normalize_description(g["description"]) for g in group}
        if len(texts) < 2:
            continue
        new_texts = list(
            dict.fromkeys(normalize_description(g["description"]) for g in group if g["id"] in new_ids)
        )
        others = [t for t in dict.fromkeys(normalize_description(g["description"]) for g in group)
                  if t not in new_texts]
        if not others:  # several differing rows, all new in this file
            others, new_texts = new_texts[1:], new_texts[:1]
        found.append(NearDuplicate(date, amount, tuple(new_texts), tuple(others)))
    return found


def ingest_file(conn: sqlite3.Connection, path: str | Path) -> FileReport:
    """Import one CSV. Never raises for bad input; see ``FileReport.status``."""
    path = Path(path)
    report = FileReport(path=str(path))
    try:
        data = path.read_bytes()
    except OSError as e:
        report.message = f"cannot read file: {e.strerror or e}"
        return report
    report.file_hash = file_sha256(data)

    prev = previous_import(conn, report.file_hash)
    if prev is not None:
        report.status = SKIPPED
        report.message = (
            f"already imported (same content as {prev['file_name']} on {prev['imported_at']}); skipped"
        )
        return report

    try:
        text = data.decode("utf-8-sig")
        result = detect_parser(path).parse_text(text)
    except UnicodeDecodeError as e:
        report.message = f"not UTF-8 text: {e}"
        return report
    except ParseFormatError as e:
        report.message = str(e)
        return report

    report.bad_rows = list(result.errors)
    report.rows_read = len(result.rows) + len(result.errors)

    # One transaction per file (a savepoint if the caller already opened one),
    # so a failure drops only this file.
    try:
        with db.transaction(conn):
            new_ids, duplicates = _insert_rows(conn, result.rows, path.name)
            applied = rules.apply_rules(conn, new_ids)
            splits.sync_after_rules(conn, applied)
            report.near_duplicates = find_near_duplicates(conn, new_ids)
            conn.execute(
                "INSERT INTO imports (file_name, file_hash, rows_read, rows_new, rows_duplicate)"
                " VALUES (?, ?, ?, ?, ?)",
                (path.name, report.file_hash, report.rows_read, len(new_ids), duplicates),
            )
    except Exception as e:
        report.near_duplicates = []
        report.message = f"import failed, nothing from this file was saved: {e!r}"
        return report

    report.status = IMPORTED
    report.new_ids = new_ids
    report.rows_new = len(new_ids)
    report.rows_duplicate = duplicates
    report.auto_categorized = len(applied.updated)
    return report


def ingest_files(conn: sqlite3.Connection, paths: Iterable[str | Path]) -> list[FileReport]:
    return [ingest_file(conn, p) for p in paths]


def all_failed(reports: list[FileReport]) -> bool:
    return bool(reports) and all(r.status == FAILED for r in reports)


# --- formatting ------------------------------------------------------------------


def format_report(report: FileReport) -> str:
    name = Path(report.path).name
    if report.status == SKIPPED:
        return f"{name}: {report.message}"
    if report.status == FAILED:
        return f"{name}: FAILED - {report.message}"
    lines = [
        f"{name}: {report.rows_read} rows read, {report.rows_new} new, "
        f"{report.rows_duplicate} duplicate, {len(report.bad_rows)} bad; "
        f"{report.auto_categorized} auto-categorized"
    ]
    for err in report.bad_rows:
        lines.append(f"  bad row, line {err.line_no}: {err.message}")
    for nd in report.near_duplicates:
        new = "; ".join(f'"{t}"' for t in nd.new_descriptions)
        other = "; ".join(f'"{t}"' for t in nd.other_descriptions)
        lines.append(
            f"  warning: possible near-duplicate on {nd.date} for {fmt.cents(nd.amount_cents)}: "
            f"{new} vs {other}"
        )
    return "\n".join(lines)


def format_totals(reports: list[FileReport]) -> str:
    counts = Counter(r.status for r in reports)
    new = sum(r.rows_new for r in reports)
    dup = sum(r.rows_duplicate for r in reports)
    return (
        f"Total: {len(reports)} file(s) - {counts[IMPORTED]} imported, "
        f"{counts[SKIPPED]} skipped, {counts[FAILED]} failed; {new} new rows, {dup} duplicates"
    )
