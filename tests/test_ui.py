import pytest

from housefin import db, ui


@pytest.mark.parametrize(
    "cents, text",
    [
        (0, "$0.00"),
        (5, "$0.05"),
        (-5, "-$0.05"),
        (123456, "$1,234.56"),
        (-123456789, "-$1,234,567.89"),
        (100, "$1.00"),
        (None, ""),
    ],
)
def test_format_cents(cents, text):
    assert ui.format_cents(cents) == text


@pytest.mark.parametrize(
    "text, cents",
    [
        ("$1,234.56", 123456),
        ("-12.5", -1250),
        ("(3.00)", -300),
        ("0.005", 1),
        (" 7 ", 700),
        (12.34, 1234),
        (5, 500),
    ],
)
def test_parse_dollars(text, cents):
    assert ui.parse_dollars(text) == cents


def test_parse_dollars_round_trip():
    for c in (0, 1, -1, 99, 123456, -987654321):
        assert ui.parse_dollars(ui.format_cents(c)) == c


def test_parse_dollars_rejects_junk():
    with pytest.raises(ValueError):
        ui.parse_dollars("abc")


def test_cents_to_dollars():
    assert ui.cents_to_dollars(-1250) == -12.5
    assert ui.cents_to_dollars(None) is None


def test_format_month():
    assert ui.format_month("2026-10") == "Oct 2026"
    assert ui.format_month("weird") == "weird"
    assert ui.format_month("2026-07", "de-DE") == "Juli 2026"


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
    assert ui.date_pattern(locale) == pattern
    assert ui.date_column_format(locale) == moment
    assert ui.format_date("2026-07-09", locale) == shown


def test_dates_without_a_browser_stay_iso():
    assert ui.date_pattern(None) is None
    assert ui.date_column_format(None) == "YYYY-MM-DD"
    assert ui.format_date("2026-07-09") == "2026-07-09"
    assert ui.format_date("2026-07-09", "zz-ZZ") == "2026-07-09"
    assert ui.format_date(None) == ""


def test_format_day_span():
    assert ui.format_day_span("2026-07-11", "2026-07-31") == "11–31 Jul"
    assert ui.format_day_span("2026-07-11", "2026-07-31", "en-US") == "Jul 11\u2009–\u200931"  # thin spaces, per CLDR
    assert ui.format_day_span("2026-07-11", "2026-07-11", "en-GB") == "11 Jul"


def test_normalize_month_selection():
    assert ui.normalize_month_selection(ui.ALL, ["2026-01"]) is None
    assert ui.normalize_month_selection("Months", []) is None
    assert ui.normalize_month_selection("Months", ["2026-02", "2026-01"]) == ["2026-01", "2026-02"]


def test_describe_months():
    assert ui.describe_months(None) == "All time"
    assert ui.describe_months(["2026-01", "2026-02"]) == "Jan 2026, Feb 2026"


def _add_tx(conn, n, date, applies_month=None):
    conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description, applies_month)"
        " VALUES (?, ?, '00:00:00', -100, 'Withdrawal', 'X', ?)",
        (f"fp{n}", date, applies_month),
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    _add_tx(c, 1, "2026-01-15")
    _add_tx(c, 2, "2026-01-31", applies_month="2026-02")  # counts toward Feb
    _add_tx(c, 3, "2026-03-02")
    _add_tx(c, 4, "2026-03-20")
    c.commit()
    return c


def test_months_present_uses_applies_month(conn):
    assert ui.months_present(conn) == ["2026-03", "2026-02", "2026-01"]


def test_months_present_empty():
    assert ui.months_present(db.connect(":memory:")) == []


def test_month_filter_sql(conn):
    where, params = ui.month_filter_sql(None)
    assert (where, params) == ("1", [])
    assert ui.month_filter_sql([]) == ("1", [])

    where, params = ui.month_filter_sql(["2026-02", "2026-03"])
    ids = [r[0] for r in conn.execute(f"SELECT id FROM transactions WHERE {where} ORDER BY id", params)]
    assert ids == [2, 3, 4]

    where, params = ui.month_filter_sql(["2026-01"], alias="t")
    ids = [r[0] for r in conn.execute(f"SELECT t.id FROM transactions t WHERE {where}", params)]
    assert ids == [1]


def test_db_path_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HOUSEFIN_DB", raising=False)
    assert ui.db_path() == db.DEFAULT_DB_PATH
    monkeypatch.setenv("HOUSEFIN_DB", str(tmp_path / "x.db"))
    assert ui.db_path() == tmp_path / "x.db"


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
    for p in ("1_Categorize", "2_Rules", "3_People", "4_Purposes", "5_Reports", "6_Balances", "7_Rooms")
]


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
