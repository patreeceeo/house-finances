"""Reports: grouped totals by person or purpose, and monthly averages by purpose,
with CSV export."""

from datetime import date

import streamlit as st

from housefin import db, fmt, reports, ui

st.set_page_config(page_title="Reports · House Finances", layout="wide")
conn = ui.get_conn()

st.title("Reports")

months = ui.month_selector(conn)

c1, c2 = st.columns([3, 1])
view = c1.radio("View", reports.PAGE_VIEWS, horizontal=True, key="report_view")
include_uncat = c2.toggle(
    "Include uncategorized", value=True, key="report_include_uncat",
    help="Show untagged transactions as their own group so nothing silently drops out.",
)

if view == reports.MONTHLY_BY_PURPOSE:
    names = [p["name"] for p in db.list_purposes(conn, include_excluded=False)]
    chosen = st.multiselect(
        "Purposes", names, default=names, key="report_avg_purposes",
        placeholder="Choose purposes",
    )
    period, rows = reports.monthly_by_purpose(
        conn, months, chosen, include_uncategorized=include_uncat
    )
    if not period or not rows:
        st.caption(f"{ui.describe_months(months)}. Transfers are left out.")
        st.info("No spending matches these filters.")
        st.stop()
    n = len(period)
    span = ui.describe_months(months) if months else (
        f"All time: {fmt.month(period[0])} – {fmt.month(period[-1])}")
    st.caption(
        f"{span}. Average per month = total ÷ "
        f"{n} month{'s' if n != 1 else ''}; months with no spending on a purpose count as "
        "$0.00. Refunds reduce spending; Transfers are left out."
    )
    total = reports.monthly_total(rows, period)
    if any(r.average_is_rounded for r in [*rows, total]):
        summed = sum(r.average_cents for r in rows)
        st.caption(
            "Averages that don't divide evenly are rounded to the nearest cent; "
            "monthly figures and totals are exact."
            + ("" if summed == total.average_cents else
               f" So the rounded averages add up to {fmt.md_cents(summed)}, while the Total "
               f"row's average (its total ÷ {n}) is {fmt.md_cents(total.average_cents)}.")
        )
    if period[-1] >= date.today().strftime("%Y-%m"):
        st.warning(
            f"{fmt.month(period[-1])} isn't over yet, so it pulls the averages down. "
            "Pick months in the sidebar to leave it out."
        )
    money = st.column_config.NumberColumn(format="dollar")
    table = reports.monthly_table_rows([*rows, total], period)
    st.dataframe(
        table, hide_index=True, width="stretch",
        column_config={c: money for c in table[0] if c != "Purpose"},
    )
    month_part = "-".join(months) if months else "all"
    st.download_button(
        "Download CSV",
        data=reports.monthly_csv([*rows, total], period),
        file_name=f"monthly_average_by_purpose_{month_part}.csv",
        mime="text/csv",
        icon=":material/download:",
    )
    st.stop()

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
st.write(f"**Total:** {fmt.md_cents(total)} across {count:,} "
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
    with st.expander(f"{g.name} · {g.count} · {fmt.md_cents(g.total_cents)}"):
        rows = [
            {
                "Date": ui.to_dates([t["date"]])[0],
                "Month": fmt.month(t["month"]),
                "Description": t["description"],
                "Amount": fmt.cents(t["amount_cents"]),
                value_label: fmt.cents(t["value_cents"]),
            }
            for t in g.transactions
        ]
        st.dataframe(
            rows, hide_index=True, width="stretch",
            column_config={"Date": ui.date_column("Date")},
        )
