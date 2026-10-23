"""Reports: grouped totals by person or purpose, with CSV export."""

import streamlit as st

from housefin import reports, ui

st.set_page_config(page_title="Reports · House Finances", layout="wide")
conn = ui.get_conn()

st.title("Reports")

months = ui.month_selector(conn)

c1, c2 = st.columns([3, 1])
view = c1.radio("View", reports.VIEWS, horizontal=True, key="report_view")
include_uncat = c2.toggle(
    "Include uncategorized", value=True, key="report_include_uncat",
    help="Show untagged transactions as their own group so nothing silently drops out.",
)

groups = reports.report(conn, view, months, include_uncategorized=include_uncat)
total_label = reports.TOTAL_LABELS[view]

st.caption(
    f"{ui.describe_months(months)}. Transfers are left out."
    + (" Refunds reduce purpose totals and people's shares." if view != reports.DEPOSITS_BY_PERSON
       else " Withdrawals paid to a person reduce their total; refund deposits are"
       " counted under Withdrawals instead.")
)

if not groups:
    st.info("No transactions match these filters.")
    st.stop()

st.dataframe(
    reports.table_rows(groups, view),
    hide_index=True,
    width="stretch",
    column_config={
        "Count": st.column_config.NumberColumn(format="%d"),
        total_label: st.column_config.NumberColumn(format="%.2f"),
    },
)
count, total = reports.grand_total(groups)
st.write(f"**Total:** {ui.format_cents(total)} across {count:,} "
         + ("shares" if view == reports.WITHDRAWALS_BY_PERSON else "transactions"))

month_part = "-".join(months) if months else "all"
st.download_button(
    "Download CSV",
    data=reports.to_csv(groups, view),
    file_name=f"{view.lower().replace(' ', '_')}_{month_part}.csv",
    mime="text/csv",
    icon=":material/download:",
)

st.subheader("Transactions by group")
value_label = "Share" if view == reports.WITHDRAWALS_BY_PERSON else (
    "In" if view == reports.DEPOSITS_BY_PERSON else "Out")
for g in groups:
    with st.expander(f"{g.name} · {g.count} · {ui.format_cents(g.total_cents)}"):
        rows = [
            {
                "Date": ui.to_dates([t["date"]])[0],
                "Month": ui.format_month(t["month"]),
                "Description": t["description"],
                "Amount": ui.format_cents(t["amount_cents"]),
                value_label: ui.format_cents(t["value_cents"]),
            }
            for t in g.transactions
        ]
        st.dataframe(
            rows, hide_index=True, width="stretch",
            column_config={"Date": ui.date_column("Date")},
        )
