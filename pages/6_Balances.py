"""Balances: paid in, share of spending, net and running balance per person."""

import pandas as pd
import streamlit as st

from housefin import balances, ui

st.set_page_config(page_title="Balances · House Finances", layout="wide")
conn = ui.get_conn()

st.title("Balances")
months = ui.month_selector(conn)
st.caption(
    f"{ui.describe_months(months)}. Running balance is through the end of "
    + (ui.format_month(months[-1]) if months else "all time")
    + "; positive is credit, negative is owed. Transfers are left out."
)

count, total = balances.uncategorized_withdrawals(conn, months)
if count:
    st.warning(
        f"{count} withdrawal{'s' if count != 1 else ''} totalling "
        f"{ui.format_cents(-total)} in the selected months "
        f"{'are' if count != 1 else 'is'} uncategorized, so that money is in "
        "no one's share yet. Categorize them on the Categorize page."
    )

rows = balances.balances(conn, months)
if not rows:
    st.info("No one to show for the selected months.")
    st.stop()

COLS = {
    "Paid in ($)": "paid",
    "Share of spending ($)": "share",
    "Net ($)": "net",
    "Running balance ($)": "running",
}
table = pd.DataFrame(
    {
        "Person": [r.name + (" (moved out)" if r.moved_out else "") for r in rows],
        "Paid in ($)": [ui.cents_to_dollars(r.paid_in_cents) for r in rows],
        "Share of spending ($)": [ui.cents_to_dollars(r.share_cents) for r in rows],
        "Net ($)": [ui.cents_to_dollars(r.net_cents) for r in rows],
        "Running balance ($)": [ui.cents_to_dollars(r.running_cents) for r in rows],
    }
)
money = st.column_config.NumberColumn(format="dollar")
event = st.dataframe(
    table,
    hide_index=True,
    column_config={c: money for c in COLS},
    on_select="rerun",
    selection_mode="single-cell",
    key="balances_table",
)
st.caption("Click an amount to list the transactions behind it.")

cells = event.selection.cells if event is not None else []
if not cells:
    st.stop()
row_idx, col = cells[0]
if col not in COLS or row_idx >= len(rows):
    st.stop()
person = rows[row_idx]
kind = COLS[col]

# Which months the clicked amount covers.
if kind == "running":
    sel_months, through = None, (months[-1] if months else None)
    period = "all time" + (f" through {ui.format_month(through)}" if through else "")
else:
    sel_months, through = months, None
    period = ui.describe_months(months) if months else "all time"


def _deposits_df():
    data = balances.paid_in_transactions(conn, person.person_id, sel_months, through=through)
    return pd.DataFrame(
        {
            "Date": [r["date"] for r in data],
            "Month": [ui.format_month(r["month"]) for r in data],
            "Description": [r["description"] for r in data],
            "Amount ($)": [ui.cents_to_dollars(r["amount_cents"]) for r in data],
        }
    )


def _shares_df():
    data = balances.share_transactions(conn, person.person_id, sel_months, through=through)
    return pd.DataFrame(
        {
            "Date": [r["date"] for r in data],
            "Month": [ui.format_month(r["month"]) for r in data],
            "Description": [r["description"] for r in data],
            "Purpose": [r["purpose"] or "" for r in data],
            "Amount ($)": [ui.cents_to_dollars(r["amount_cents"]) for r in data],
            "Share ($)": [ui.cents_to_dollars(r["share_cents"]) for r in data],
        }
    )


def _show(title: str, df: pd.DataFrame) -> None:
    st.markdown(f"**{title}** ({len(df)})")
    if df.empty:
        st.caption("None.")
    else:
        cfg = {c: money for c in df.columns if c.endswith("($)")}
        st.dataframe(df, hide_index=True, column_config=cfg)


st.subheader(f"{person.name}: {col.removesuffix(' ($)').lower()}, {period}")
if kind == "paid":
    _show("Deposits", _deposits_df())
elif kind == "share":
    _show("Shares of withdrawals and refunds", _shares_df())
else:
    _show("Deposits (paid in)", _deposits_df())
    _show("Shares of withdrawals and refunds", _shares_df())
