"""House Finances: Streamlit entry point.  Run with `uv run streamlit run app.py`.

Pages live in pages/ and are picked up by Streamlit's multipage navigation.
The database is finances.db at the repo root, or the path in env HOUSEFIN_DB.
"""

import streamlit as st

from housefin import ui

st.set_page_config(page_title="House Finances", page_icon=":house:", layout="wide")

conn = ui.get_conn()

st.title("House Finances")
st.caption(f"Database: `{ui.db_path()}` (all data stays on this machine)")

stats = conn.execute(
    """
    SELECT COUNT(*) AS n,
           SUM(CASE WHEN amount_cents > 0 THEN 1 ELSE 0 END) AS deposits,
           SUM(CASE WHEN amount_cents < 0 THEN 1 ELSE 0 END) AS withdrawals,
           SUM(CASE WHEN person_id IS NULL AND purpose_id IS NULL THEN 1 ELSE 0 END) AS uncategorized,
           MIN(date) AS first, MAX(date) AS last
    FROM transactions
    """
).fetchone()
imports = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]

if not stats["n"]:
    st.info("No transactions yet. Import a bank CSV export to get started.")
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transactions", f"{stats['n']:,}")
    c2.metric("Deposits / withdrawals", f"{stats['deposits'] or 0:,} / {stats['withdrawals'] or 0:,}")
    c3.metric("Uncategorized", f"{stats['uncategorized'] or 0:,}")
    c4.metric("Imports", f"{imports:,}")
    st.write(f"Dates: **{ui.format_date(stats['first'])}** to **{ui.format_date(stats['last'])}**")
    if stats["uncategorized"]:
        st.page_link("pages/1_Categorize.py", label="Categorize uncategorized transactions", icon=":material/label:")

st.subheader("Importing")
st.markdown(
    "Run the importer from a terminal in the project folder, then reload this page:\n\n"
    "```bash\nuv run python ingest.py path/to/transactions.csv\n```\n"
    "Re-importing an overlapping export is safe: transactions already stored are skipped."
)

st.subheader("Pages")
st.markdown(
    "- **Categorize**: tag new transactions with a person or purpose\n"
    "- **Rules**: remembered descriptions and what they map to\n"
    "- **People**: housemates, move-in and move-out months\n"
    "- **Purposes**: spending categories and their default splits\n"
    "- **Reports**: totals by person and purpose, with CSV export\n"
    "- **Balances**: who is ahead and who is behind"
)
