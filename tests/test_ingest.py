import os
from pathlib import Path

import pytest

import ingest as cli
from housefin import db, ingest, rules, splits

HEADER = "Date, Time, Amount, Type, Description\n"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def write_csv(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text(HEADER + "".join(line + "\n" for line in lines), encoding="utf-8")
    return p


def count(conn, table="transactions"):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


NEWER = [
    "2026-03-05,09:15:00,250.00,Deposit,Zelle payment from TEST PERSON",
    "2026-03-04,18:02:41,-42.17,Withdrawal,\"GROCERYCO 1 Main St, Suite 2\"",
    "2026-03-01,00:00:01,0.03,Deposit,Interest Paid",
]
OLDER = [
    "2026-03-04,18:02:41,-42.17,Withdrawal,\"GROCERYCO 1 Main St, Suite 2\"",
    "2026-03-01,00:00:01,0.03,Deposit,Interest Paid",
    "2026-02-28,10:00:00,-15.00,Withdrawal,EXAMPLE UTILITY",
    "2026-02-27,11:00:00,-9.99,Withdrawal,STREAMING CO",
]


def test_basic_import_and_reimport(conn, tmp_path):
    f = write_csv(tmp_path, "a.csv", NEWER)
    r = ingest.ingest_file(conn, f)
    assert r.status == ingest.IMPORTED
    assert (r.rows_read, r.rows_new, r.rows_duplicate) == (3, 3, 0)
    # Seeded Interest Paid -> House rule tags one row.
    assert r.auto_categorized == 1
    imp = conn.execute("SELECT * FROM imports").fetchone()
    assert (imp["file_name"], imp["rows_read"], imp["rows_new"], imp["rows_duplicate"]) == (
        "a.csv", 3, 3, 0)

    again = ingest.ingest_file(conn, f)
    assert again.status == ingest.SKIPPED
    assert "already imported" in again.message
    assert count(conn) == 3
    assert count(conn, "imports") == 1


def test_overlapping_export_adds_only_new_rows(conn, tmp_path):
    ingest.ingest_file(conn, write_csv(tmp_path, "new.csv", NEWER))
    r = ingest.ingest_file(conn, write_csv(tmp_path, "old.csv", OLDER))
    assert (r.rows_new, r.rows_duplicate) == (2, 2)
    assert count(conn) == 5
    # Same rows, different bytes (reordered) -> not hash-skipped, still adds 0.
    r = ingest.ingest_file(conn, write_csv(tmp_path, "old2.csv", list(reversed(OLDER))))
    assert r.status == ingest.IMPORTED
    assert (r.rows_new, r.rows_duplicate) == (0, 4)
    assert count(conn) == 5


def test_identical_rows_in_one_file_kept_but_not_doubled(conn, tmp_path):
    twin = "2026-03-02,12:00:00,-50.00,Withdrawal,Zelle payment to SOMEONE"
    r = ingest.ingest_file(conn, write_csv(tmp_path, "a.csv", [twin, twin]))
    assert r.rows_new == 2
    occ = [row[0] for row in conn.execute("SELECT occurrence FROM transactions ORDER BY occurrence")]
    assert occ == [1, 2]
    # Overlapping file with just one copy adds nothing.
    r = ingest.ingest_file(conn, write_csv(tmp_path, "b.csv", [twin, NEWER[0]]))
    assert (r.rows_new, r.rows_duplicate) == (1, 1)
    # A file with three copies adds exactly one more.
    r = ingest.ingest_file(conn, write_csv(tmp_path, "c.csv", [twin, twin, twin]))
    assert (r.rows_new, r.rows_duplicate) == (1, 2)
    assert count(conn) == 4


def test_whitespace_differences_are_duplicates(conn, tmp_path):
    ingest.ingest_file(conn, write_csv(tmp_path, "a.csv", ["2026-03-02,12:00:00,-5.00,Withdrawal,A  B"]))
    r = ingest.ingest_file(conn, write_csv(tmp_path, "b.csv", ["2026-03-02,12:00:00,-5.00,Withdrawal,A B"]))
    assert (r.rows_new, r.rows_duplicate) == (0, 1)


def test_bad_rows_reported_and_rest_imported(conn, tmp_path):
    f = write_csv(tmp_path, "a.csv", [NEWER[0], "2026-03-02,12:00:00,abc,Withdrawal,X",
                                       "2026-03-02,12:00:00,5.00,Withdrawal,Y"])
    r = ingest.ingest_file(conn, f)
    assert r.status == ingest.IMPORTED
    assert r.rows_read == 3 and r.rows_new == 1
    assert [e.line_no for e in r.bad_rows] == [3, 4]
    text = ingest.format_report(r)
    assert "line 3" in text and "line 4" in text


def test_bad_header_fails_file(conn, tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("foo,bar\n1,2\n")
    r = ingest.ingest_file(conn, p)
    assert r.status == ingest.FAILED
    assert count(conn, "imports") == 0
    missing = ingest.ingest_file(conn, tmp_path / "nope.csv")
    assert missing.status == ingest.FAILED


def test_crash_mid_file_leaves_nothing(conn, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(splits, "sync_after_rules", boom)
    f = write_csv(tmp_path, "a.csv", NEWER)
    r = ingest.ingest_file(conn, f)
    assert r.status == ingest.FAILED
    assert count(conn) == 0
    assert count(conn, "imports") == 0
    assert conn.execute("SELECT category_source FROM transactions").fetchall() == []
    # After the crash is fixed the same file imports normally (not hash-skipped).
    monkeypatch.undo()
    assert ingest.ingest_file(conn, f).rows_new == 3


def test_crash_after_rules_commit_attempt_rolls_back(conn, tmp_path, monkeypatch):
    # rules.apply_rules opens its own db.transaction; it must not commit the import early.
    orig = rules.apply_rules

    def apply_then_crash(c, ids):
        orig(c, ids)
        raise RuntimeError("after rules")

    monkeypatch.setattr(rules, "apply_rules", apply_then_crash)
    r = ingest.ingest_file(conn, write_csv(tmp_path, "a.csv", NEWER))
    assert r.status == ingest.FAILED
    assert count(conn) == 0


def test_rules_and_default_split_applied_to_new_rows(conn, tmp_path):
    a = db.add_person(conn, "Alice")
    b = db.add_person(conn, "Bob")
    util = db.get_purpose_id(conn, "Water")
    splits.set_purpose_split(conn, util, [(a, 1), (b, 3)])
    rules.create_rule(conn, "EXAMPLE UTILITY", "withdrawal", purpose_id=util)
    r = ingest.ingest_file(conn, write_csv(tmp_path, "old.csv", OLDER))
    assert r.auto_categorized == 2  # utility + interest
    assert r.splits_written == 1
    txn = conn.execute("SELECT * FROM transactions WHERE description = 'EXAMPLE UTILITY'").fetchone()
    assert txn["purpose_id"] == util and txn["category_source"] == "rule"
    shares = dict(conn.execute(
        "SELECT person_id, share_cents FROM splits WHERE transaction_id = ?", (txn["id"],)).fetchall())
    assert shares == {a: 375, b: 1125}


def test_manual_tags_on_existing_rows_untouched(conn, tmp_path):
    ingest.ingest_file(conn, write_csv(tmp_path, "a.csv", NEWER))
    conn.execute("UPDATE transactions SET person_id = NULL, category_source = 'manual'"
                 " WHERE description = 'Interest Paid'")
    conn.commit()
    r = ingest.ingest_file(conn, write_csv(tmp_path, "b.csv", OLDER))
    assert r.auto_categorized == 0
    row = conn.execute("SELECT category_source FROM transactions WHERE description = 'Interest Paid'").fetchone()
    assert row[0] == "manual"


def test_near_duplicate_warning_vs_db_and_within_file(conn, tmp_path):
    ingest.ingest_file(conn, write_csv(tmp_path, "a.csv", [
        "2026-03-02,12:00:00,-20.00,Withdrawal,STRUCTURE PROPER Future Amount"]))
    r = ingest.ingest_file(conn, write_csv(tmp_path, "b.csv", [
        "2026-03-02,14:00:00,-20.00,Withdrawal,STRUCTURE PROPER",
        "2026-03-03,09:00:00,-7.00,Withdrawal,CAFE ONE",
        "2026-03-03,10:00:00,-7.00,Withdrawal,CAFE TWO",
        "2026-03-04,10:00:00,-8.00,Withdrawal,SAME",
        "2026-03-04,11:00:00,-8.00,Withdrawal,SAME",
    ]))
    assert r.rows_new == 5
    by_date = {nd.date: nd for nd in r.near_duplicates}
    assert set(by_date) == {"2026-03-02", "2026-03-03"}
    assert by_date["2026-03-02"].new_descriptions == ("STRUCTURE PROPER",)
    assert by_date["2026-03-02"].other_descriptions == ("STRUCTURE PROPER Future Amount",)
    assert set(by_date["2026-03-03"].new_descriptions + by_date["2026-03-03"].other_descriptions) == {
        "CAFE ONE", "CAFE TWO"}
    assert "near-duplicate on 2026-03-02 for -$20.00" in ingest.format_report(r)


def test_no_near_duplicate_warning_for_exact_duplicates(conn, tmp_path):
    ingest.ingest_file(conn, write_csv(tmp_path, "a.csv", NEWER))
    r = ingest.ingest_file(conn, write_csv(tmp_path, "b.csv", OLDER))
    assert r.near_duplicates == []


# --- CLI -------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path, capsys):
    dbfile = tmp_path / "cli.db"
    good = write_csv(tmp_path, "good.csv", NEWER)
    bad = tmp_path / "bad.csv"
    bad.write_text("nope\n")
    assert cli.main(["--db", str(dbfile), str(good), str(bad)]) == 0
    out = capsys.readouterr().out
    assert "good.csv: 3 rows read, 3 new" in out and "bad.csv: FAILED" in out
    # Re-run: good is skipped (not a failure), bad fails -> still 0.
    assert cli.main(["--db", str(dbfile), str(good), str(bad)]) == 0
    assert cli.main(["--db", str(dbfile), str(bad)]) == 1


def test_cli_without_paths_and_no_picker(tmp_path, capsys, monkeypatch):
    def unavailable():
        raise cli.PickerUnavailable("tkinter is not installed")

    monkeypatch.setattr(cli, "pick_files", unavailable)
    assert cli.main(["--db", str(tmp_path / "x.db")]) == 2
    assert "Pass the files on the command line" in capsys.readouterr().err


def test_cli_picker_used(tmp_path, monkeypatch, capsys):
    good = write_csv(tmp_path, "good.csv", NEWER)
    monkeypatch.setattr(cli, "pick_files", lambda: [str(good)])
    assert cli.main(["--db", str(tmp_path / "x.db")]) == 0
    assert "3 new" in capsys.readouterr().out
    monkeypatch.setattr(cli, "pick_files", lambda: [])
    assert cli.main(["--db", str(tmp_path / "x.db")]) == 0


@pytest.mark.skipif(not os.environ.get("HOUSEFIN_SAMPLE_CSV"), reason="HOUSEFIN_SAMPLE_CSV not set")
def test_sample_reimport_adds_nothing(conn):
    sample = Path(os.environ["HOUSEFIN_SAMPLE_CSV"])
    first = ingest.ingest_file(conn, sample)
    assert first.status == ingest.IMPORTED and first.rows_new > 0
    assert ingest.ingest_file(conn, sample).status == ingest.SKIPPED
    conn.execute("DELETE FROM imports")
    conn.commit()
    again = ingest.ingest_file(conn, sample)
    assert again.rows_new == 0 and again.rows_duplicate == first.rows_new
