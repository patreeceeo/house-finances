"""Balances: paid in, share of spending, net and running balance per person.

Paid in nets deposits from a person against withdrawals paid to them.
"""

import pandas as pd
import streamlit as st

from housefin import balances, fmt, matching_ui, ui

st.set_page_config(page_title="Balances · House Finances", layout="wide")
conn = ui.get_conn()

st.title("Balances")
months = ui.month_selector(conn)
st.caption(
    f"{ui.describe_months(months)}. Running balance is through the end of "
    + (fmt.month(months[-1]) if months else "all time")
    + "; positive is credit, negative is owed. Paid in is deposits minus withdrawals"
    " paid to the person. Transfers are left out."
)

count, total = balances.uncategorized_withdrawals(conn, months)
if count:
    n_rent, rent_total = balances.rent_awaiting_review(conn, months)
    n_other = count - n_rent
    parts = []
    if n_other:
        parts.append(
            f"{n_other} {'are' if n_other != 1 else 'is'} uncategorized or "
            f"{'have' if n_other != 1 else 'has'} no split "
            f"({fmt.md_cents(-(total - rent_total))}): categorize "
            f"{'them' if n_other != 1 else 'it'} on the Categorize page."
        )
    if n_rent:
        parts.append(
            f"{n_rent} {'are rent payments' if n_rent != 1 else 'is a rent payment'} "
            f"({fmt.md_cents(-rent_total)}) for a month when a room was empty for "
            "some nights (or the room percents need fixing), so nobody has said who "
            "covers the empty room's share yet. Review "
            f"{'them' if n_rent != 1 else 'it'} on the Categorize page "
            "(Show: Needs review)."
        )
    st.warning(
        f"{count} withdrawal{'s' if count != 1 else ''} totalling "
        f"{fmt.md_cents(-total)} in the selected months "
        f"{'count' if count != 1 else 'counts'} toward no one's share yet. "
        + " ".join(parts)
    )

count, total = balances.splits_pending_review(conn, months)
if count:
    st.warning(
        f"{count} split{'s' if count != 1 else ''} totalling {fmt.md_cents(-total)} in the "
        "selected months left out someone who wasn't in the house that month, so everyone else's "
        "share was scaled up. Nobody has confirmed "
        f"{'them' if count != 1 else 'it'} yet: review on the Categorize page (Show: Needs review)."
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
        "Paid in ($)": [fmt.dollars(r.paid_in_cents) for r in rows],
        "Share of spending ($)": [fmt.dollars(r.share_cents) for r in rows],
        "Net ($)": [fmt.dollars(r.net_cents) for r in rows],
        "Running balance ($)": [fmt.dollars(r.running_cents) for r in rows],
    }
)
rows_by_id = {r.person_id: r for r in rows}
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



def _deposits_df(person_id, sel_months, through):
    data = balances.paid_in_transactions(conn, person_id, sel_months, through=through)
    return pd.DataFrame(
        {
            "Date": ui.to_dates(r["date"] for r in data),
            "Month": [fmt.month(r["month"]) for r in data],
            "Description": [r["description"] for r in data],
            "Amount ($)": [fmt.dollars(r["amount_cents"]) for r in data],
        }
    )


def _shares_df(person_id, sel_months, through):
    data = balances.share_transactions(conn, person_id, sel_months, through=through)
    return pd.DataFrame(
        {
            "Date": ui.to_dates(r["date"] for r in data),
            "Month": [fmt.month(r["month"]) for r in data],
            "Description": [r["description"] for r in data],
            "Purpose": [r["purpose"] or "" for r in data],
            "Amount ($)": [fmt.dollars(r["amount_cents"]) for r in data],
            "Share ($)": [fmt.dollars(r["share_cents"]) for r in data],
        }
    )


def _show(title: str, df: pd.DataFrame) -> None:
    st.markdown(f"**{title}** ({len(df)})")
    if df.empty:
        st.caption("None.")
    else:
        cfg = {c: money for c in df.columns if c.endswith("($)")}
        cfg["Date"] = ui.date_column("Date")
        st.dataframe(df, hide_index=True, column_config=cfg)


ss = st.session_state
cells = event.selection.cells if event is not None else []
clicked = tuple(cells[0]) if cells and cells[0][0] < len(rows) else None
# A newly clicked cell (any cell in a row) picks that person for matching below.
if clicked != ss.get("_balances_clicked"):
    ss["_balances_clicked"] = clicked
    if clicked is not None:
        ss["match_person"] = rows[clicked[0]].person_id

if clicked is not None and clicked[1] in COLS:
    person = rows[clicked[0]]
    col = clicked[1]
    kind = COLS[col]
    # Which months the clicked amount covers.
    if kind == "running":
        sel_months, through = None, (months[-1] if months else None)
        period = "all time" + (f" through {fmt.month(through)}" if through else "")
    else:
        sel_months, through = months, None
        period = ui.describe_months(months) if months else "all time"
    args = (person.person_id, sel_months, through)

    st.subheader(f"{person.name}: {col.removesuffix(' ($)').lower()}, {period}")
    if kind == "paid":
        _show("Deposits and withdrawals", _deposits_df(*args))
    elif kind == "share":
        _show("Shares of withdrawals and refunds", _shares_df(*args))
    else:
        _show("Deposits and withdrawals (paid in)", _deposits_df(*args))
        _show("Shares of withdrawals and refunds", _shares_df(*args))

st.subheader("Match deposits to shares")
st.caption(
    "A person's shares of withdrawals next to their deposits, in groups that pay for "
    "each other. Range: all time through the end of "
    + (fmt.month(months[-1]) if months else "the latest month")
    + ", same as Running balance. Each group's difference is deposits minus shares, and the "
    "differences add up to the Running balance, so a balance that isn't zero points at the "
    "groups causing it. Suggested groups aren't saved until you keep or edit them."
)
names = {r.person_id: r.name + (" (moved out)" if r.moved_out else "") for r in rows}
if ss.get("match_person") not in names:
    ss.pop("match_person", None)
person_id = st.selectbox(
    "Person",
    list(names),
    format_func=names.get,
    index=None,
    placeholder="Choose a person, or click their row in the table",
    key="match_person",
)
if person_id is not None:
    matching_ui.show(conn, person_id, rows_by_id[person_id].name, months)
