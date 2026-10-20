"""People: add housemates, rename, move out, delete."""

import streamlit as st

from housefin import db, people, ui

st.set_page_config(page_title="People · House Finances", layout="wide")
conn = ui.get_conn()
ss = st.session_state

st.title("People")
st.caption(
    "Moving someone out keeps their history; from the month after their last month "
    "they leave the dropdowns. Delete is only possible for someone with no "
    "transactions or rules. New people are not added to remembered splits; edit a "
    "split to include them."
)

flash = ss.pop("_people_flash", None)
if flash:
    st.success(flash)


def done(msg: str) -> None:
    ss["_people_flash"] = msg
    st.rerun()


NOT_SET = "—"
now = people.current_month()
everyone = db.list_people(conn)
choices = people.month_choices(
    conn, extra=tuple(m for p in everyone for m in (p["active_from"], p["active_until"]))
)

# --- list ------------------------------------------------------------------------

rows = []
for p in everyone:
    u = people.usage(conn, p["id"])
    rows.append(
        {
            "Name": p["name"],
            "Status": people.status(p, now),
            "Active from": ui.format_month(p["active_from"]) if p["active_from"] else "",
            "Active until": ui.format_month(p["active_until"]) if p["active_until"] else "",
            "Deposits": u["transactions"],
            "Split shares": u["splits"],
            "Rules": u["rules"],
        }
    )
st.dataframe(rows, hide_index=True, width="stretch")

# --- add ---------------------------------------------------------------------------

with st.form("add_person", clear_on_submit=True):
    st.subheader("Add a person")
    c1, c2 = st.columns(2)
    new_name = c1.text_input("Name")
    new_from = c2.selectbox(
        "Active from",
        [NOT_SET] + choices,
        index=1 + choices.index(now) if now in choices else 0,
        format_func=lambda m: "Always" if m == NOT_SET else ui.format_month(m),
    )
    if st.form_submit_button("Add person", type="primary"):
        try:
            people.add_person(conn, new_name, None if new_from == NOT_SET else new_from)
        except people.PersonError as exc:
            st.error(str(exc))
        else:
            done(f"Added {' '.join(new_name.split())}.")

# --- edit ----------------------------------------------------------------------------

st.subheader("Edit a person")
by_id = {p["id"]: p for p in everyone}
pid = st.selectbox(
    "Person",
    list(by_id),
    format_func=lambda i: by_id[i]["name"] + (" (House)" if by_id[i]["is_house"] else ""),
    key="people_edit_pid",
)
person = by_id[pid]

with st.form(f"rename_{pid}"):
    name = st.text_input("Name", value=person["name"])
    if st.form_submit_button("Rename"):
        try:
            people.rename_person(conn, pid, name)
        except people.PersonError as exc:
            st.error(str(exc))
        else:
            done(f"Renamed {person['name']} to {' '.join(name.split())}.")

if person["is_house"]:
    st.info("The House is always present: it can be renamed, but not moved out or deleted.")
    st.stop()

with st.form(f"dates_{pid}"):
    c1, c2 = st.columns(2)
    from_opts = [NOT_SET] + choices
    cur_from = person["active_from"] or NOT_SET
    active_from = c1.selectbox(
        "Active from", from_opts, index=from_opts.index(cur_from),
        format_func=lambda m: "Always" if m == NOT_SET else ui.format_month(m),
    )
    until_opts = [NOT_SET] + choices
    cur_until = person["active_until"] or NOT_SET
    active_until = c2.selectbox(
        "Active until (last month, inclusive)", until_opts, index=until_opts.index(cur_until),
        format_func=lambda m: "Still here" if m == NOT_SET else ui.format_month(m),
        help="Move out: from the following month they leave the dropdowns. "
        "Their history, reports and balance stay.",
    )
    if st.form_submit_button("Save dates"):
        new_from_v = None if active_from == NOT_SET else active_from
        new_until_v = None if active_until == NOT_SET else active_until
        try:
            people.set_active_range(conn, pid, new_from_v, new_until_v)
        except people.PersonError as exc:
            st.error(str(exc))
        else:
            if new_until_v and new_until_v != person["active_until"]:
                done(
                    f"{person['name']} moves out after {ui.format_month(new_until_v)}; "
                    f"from {ui.format_month(people.add_months(new_until_v, 1))} "
                    "they leave the dropdowns."
                )
            done(f"Saved dates for {person['name']}.")

blocker = people.delete_blocker(conn, pid)
with st.expander("Delete"):
    if blocker:
        st.caption(blocker)
    else:
        n_ps = people.usage(conn, pid)["purpose_splits"]
        if n_ps:
            st.caption(
                f"They will also be dropped from {n_ps} remembered split"
                f"{'s' if n_ps != 1 else ''}."
            )
        confirm = st.checkbox(f"Yes, delete {person['name']}", key=f"del_confirm_{pid}")
        if st.button("Delete person", disabled=not confirm, key=f"del_{pid}"):
            try:
                people.delete_person(conn, pid)
            except people.PersonError as exc:
                st.error(str(exc))
            else:
                ss.pop("people_edit_pid", None)
                done(f"Deleted {person['name']}.")
