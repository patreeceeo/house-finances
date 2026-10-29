"""People: add housemates, rename, colors, delete."""

import pandas as pd
import streamlit as st

from housefin import colors, dates, db, fmt, people, ui

st.set_page_config(page_title="People · House Finances", layout="wide")
conn = ui.get_conn()
ss = st.session_state

st.title("People")
st.caption(
    "Who is in the house comes from room assignments: someone counts for a month "
    "when they are in a room on any night of it. Delete is only possible for "
    "someone with no transactions, rules or room assignments. New people are not "
    "added to remembered splits; edit a split to include them."
)

ui.show_flash("people")


def done(msg: str) -> None:
    ui.flash("people", msg)
    st.rerun()


now = dates.current_month()
everyone = db.list_people(conn)

# --- list ------------------------------------------------------------------------

STATUS_HELP = {
    people.IN_THE_HOUSE: "in a room on at least one night this month",
    people.AWAY: "had a room before this month and has one after it, but none this month",
    people.NOT_YET_ARRIVED: "their first room starts after this month",
    people.MOVED_OUT: "had a room before this month and has none from this month on",
    people.NOT_IN_A_ROOM: "has never been put in a room, so they don't count as in the house",
}

rows = []
for p in everyone:
    u = people.usage(conn, p["id"])
    room = people.current_room(conn, p["id"])
    rows.append(
        {
            "Name": p["name"],
            "Color": p["color"] or "",
            "Status": people.status(conn, p, now),
            "Room now": room["name"] if room else "",
            "Transactions": u["transactions"],
            "Split shares": u["splits"],
            "Rules": u["rules"],
        }
    )
table = pd.DataFrame(rows)


def _swatch(col: pd.Series) -> list[str]:
    return [
        f"background-color: {c}; color: {colors.text_color_for(c)}" if c else ""
        for c in col
    ]


status_label = f"Status in {fmt.month(now)}"
st.dataframe(
    table.rename(columns={"Status": status_label}).style.apply(_swatch, subset=["Color"]),
    hide_index=True,
    width="stretch",
    column_config={
        "Color": st.column_config.TextColumn(
            help="Used for them on the Rooms page. Pick another with New color below."
        ),
        status_label: st.column_config.TextColumn(
            help="From room assignments (Rooms page). "
            + " ".join(f"{k}: {v}." for k, v in STATUS_HELP.items())
        ),
        "Room now": st.column_config.TextColumn(help="The room they are in today."),
    },
)
shown = {r["Status"] for r in rows}
notes = [f"**{k}**: {v}." for k, v in STATUS_HELP.items() if k in shown and k != people.IN_THE_HOUSE]
if notes:
    st.caption(" ".join(notes) + " Rooms and dates are set on the [Rooms page](Rooms).")

if not ui.can_edit():
    st.caption("View only: an editor can add, rename or delete people.")
    st.stop()

# --- add ---------------------------------------------------------------------------

with st.form("add_person", clear_on_submit=True):
    st.subheader("Add a person")
    new_name = st.text_input("Name")
    if st.form_submit_button("Add person", type="primary"):
        try:
            people.add_person(conn, new_name)
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

if st.button("New color", key=f"color_{pid}", help="Pick another random color for them."):
    people.new_color(conn, pid)
    done(f"New color for {person['name']}.")

if person["is_house"]:
    st.info("The House is always present: it can be renamed, but not put in a room or deleted.")
    st.stop()

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
