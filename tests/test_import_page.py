"""Import page (pages/0_Import.py) and ingest_bytes, on temp databases."""

from pathlib import Path

import pytest

from housefin import db, ingest

ROOT = Path(__file__).resolve().parent.parent
PAGE = str(ROOT / "pages" / "0_Import.py")

CSV = (
    "Date, Time, Amount, Type, Description\n"
    "2026-03-05,09:15:00,250.00,Deposit,Zelle payment from TEST PERSON\n"
    "2026-03-04,18:02:41,-42.17,Withdrawal,GROCERYCO\n"
).encode()
OVERLAP = CSV + b"2026-03-06,10:00:00,-5.00,Withdrawal,COFFEE\n"


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    db.connect(path).close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path


def count(path):
    c = db.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        c.close()


def test_ingest_bytes_matches_ingest_file(tmp_path):
    conn = db.connect(":memory:")
    r = ingest.ingest_bytes(conn, "upload.csv", CSV)
    assert (r.status, r.rows_new) == (ingest.IMPORTED, 2)
    assert conn.execute("SELECT DISTINCT source_file FROM transactions").fetchone()[0] == "upload.csv"
    assert ingest.ingest_bytes(conn, "again.csv", CSV).status == ingest.SKIPPED
    r = ingest.ingest_bytes(conn, "more.csv", OVERLAP)
    assert (r.rows_new, r.rows_duplicate) == (1, 2)
    bad = ingest.ingest_bytes(conn, "bad.csv", b"nope\n")
    assert bad.status == ingest.FAILED and "bad.csv" in bad.message


def _page():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(PAGE, default_timeout=30).run()


def test_upload_imports_and_reports(app_db):
    at = _page()
    assert at.button[0].disabled  # nothing chosen yet
    at.get("file_uploader")[0].upload("march.csv", CSV, "text/csv")
    at.get("file_uploader")[0].upload("bad.csv", b"nope\n", "text/csv")
    at.run()
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert count(app_db) == 2
    assert "march.csv: 2 rows read, 2 new" in at.success[0].value
    assert "bad.csv: FAILED" in at.error[0].value
    assert any("1 imported" in m.value for m in at.markdown)
    # The uploader is cleared; uploading the same export again adds nothing.
    assert not at.get("file_uploader")[0].value
    at.get("file_uploader")[0].upload("march.csv", CSV, "text/csv")
    at.run()
    at.button[0].click().run()
    assert count(app_db) == 2
    assert "already imported" in at.info[0].value


def test_viewer_cannot_import(app_db, monkeypatch):
    monkeypatch.setenv("HOUSEFIN_ROLE", "viewer")
    at = _page()
    assert not at.exception, at.exception
    assert not at.button and not at.get("file_uploader")
    assert "View only" in at.info[0].value
