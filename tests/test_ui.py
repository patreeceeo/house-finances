import pytest

from housefin import db, fmt, ui


@pytest.mark.parametrize(
    "locale, pattern, moment, shown",
    [
        ("en-US", "MM/dd/yyyy", "MM/DD/YYYY", "07/09/2026"),
        ("en-GB", "dd/MM/yyyy", "DD/MM/YYYY", "09/07/2026"),
        ("de-DE", "dd.MM.yyyy", "DD.MM.YYYY", "09.07.2026"),
        ("ja", "yyyy/MM/dd", "YYYY/MM/DD", "2026/07/09"),
        # CLDR puts a narrow no-break space before Bulgarian's 'г.'
        ("bg", "dd.MM.yyyy\u202f'г'.", "DD.MM.YYYY\u202f[г].", "09.07.2026\u202fг."),
    ],
)
def test_dates_follow_the_browser_locale(locale, pattern, moment, shown):
    # Day and month padded, four-digit year: how browsers' date inputs show it,
    # so a date cell reads the same while being edited.
    assert fmt.date_pattern(locale) == pattern
    assert ui.date_column_format(locale) == moment
    assert fmt.date("2026-07-09", locale) == shown


def test_dates_without_a_browser_stay_iso():
    assert fmt.date_pattern(None) is None
    assert ui.date_column_format(None) == "YYYY-MM-DD"
    assert fmt.date("2026-07-09") == "2026-07-09"
    assert fmt.date("2026-07-09", "zz-ZZ") == "2026-07-09"
    assert fmt.date(None) == ""


def test_normalize_month_selection():
    assert ui.normalize_month_selection(ui.ALL, ["2026-01"]) is None
    assert ui.normalize_month_selection("Months", []) is None
    assert ui.normalize_month_selection("Months", ["2026-02", "2026-01"]) == ["2026-01", "2026-02"]


def test_describe_months():
    assert ui.describe_months(None) == "All time"
    assert ui.describe_months(["2026-01", "2026-02"]) == "Jan 2026, Feb 2026"


def test_db_path_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HOUSEFIN_DB", raising=False)
    assert ui.db_path() == db.DEFAULT_DB_PATH
    monkeypatch.setenv("HOUSEFIN_DB", str(tmp_path / "x.db"))
    assert ui.db_path() == tmp_path / "x.db"
    assert db.db_path() == tmp_path / "x.db"


def test_connect_threadsafe(tmp_path):
    import threading

    conn = ui.connect_threadsafe(tmp_path / "t.db")
    out = []
    t = threading.Thread(target=lambda: out.append(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]))
    t.start()
    t.join()
    assert out == [1]  # the seeded House


# --- AppTest smoke tests: every page renders without exceptions -------------

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["app.py"] + [
    f"pages/{p}.py"
    for p in ("0_Import", "1_Categorize", "2_Rules", "3_People", "4_Purposes", "5_Reports", "6_Balances", "7_Rooms")
]


def _add_tx(conn, n, date, applies_month=None):
    conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description, applies_month)"
        " VALUES (?, ?, '00:00:00', -100, 'Withdrawal', 'X', ?)",
        (f"fp{n}", date, applies_month),
    )


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "app.db"
    c = db.connect(path)
    _add_tx(c, 1, "2026-01-15")
    c.commit()
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return path


@pytest.mark.parametrize("page", PAGES)
def test_pages_render(app_db, page):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / page), default_timeout=30).run()
    assert not at.exception, at.exception


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_for_a_viewer(app_db, page, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("HOUSEFIN_ROLE", "viewer")
    at = AppTest.from_file(str(ROOT / page), default_timeout=30).run()
    assert not at.exception, at.exception
    assert any("View only" in c.value for c in at.sidebar.caption)
    assert not at.button  # no edit controls


def test_hosted_page_without_sign_in_stops(app_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("HOUSEFIN_ACCESS_TEAM_DOMAIN", "myhouse.cloudflareaccess.com")
    monkeypatch.setenv("HOUSEFIN_ACCESS_AUD", "aud")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not at.exception, at.exception
    assert "No sign-in token" in at.error[0].value
    assert not at.title  # nothing of the page shown


def test_home_shows_counts(app_db):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    values = {m.label: m.value for m in at.metric}
    assert values["Transactions"] == "1"
    assert values["Uncategorized"] == "1"


def test_month_selector_widget(app_db, tmp_path):
    from streamlit.testing.v1 import AppTest

    def script():
        import streamlit as st

        from housefin import ui

        sel = ui.month_selector(ui.get_conn())
        st.write(f"sel={sel}")

    at = AppTest.from_function(script, default_timeout=30).run()
    assert not at.exception
    assert at.markdown[0].value == "sel=None"
    at.sidebar.radio[0].set_value("Months").run()
    at.sidebar.multiselect[0].select("2026-01").run()
    assert at.markdown[0].value == "sel=['2026-01']"

def test_read_only_connection_refuses_writes(tmp_path):
    import sqlite3

    path = tmp_path / "ro.db"
    ui.connect_threadsafe(path).close()
    conn = ui.connect_threadsafe(path, read_only=True)
    assert conn.execute("SELECT name FROM people").fetchone()["name"] == db.HOUSE_NAME
    for sql in (
        "INSERT INTO people (name) VALUES ('Lee')",
        "UPDATE people SET name = 'Lee'",
        "DELETE FROM people",
    ):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(sql)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with db.transaction(conn):
            db.add_person(conn, "Lee")
