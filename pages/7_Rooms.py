"""Rooms: each room's percent of the rent, who was in it when, and a timeline."""

import pandas as pd
import streamlit as st

from housefin import db, people, rooms, splits, ui
from housefin import rooms_ui as ru

st.set_page_config(page_title="Rooms · House Finances", layout="wide")
conn = ui.get_conn()
ss = st.session_state

st.title("Rooms")
st.caption(
    "Each room carries a fixed percent of the rent, and a rent payment is split by "
    "who was in each room on each night of its month. Dates are nights, both ends "
    "included: someone moving out on the 10th has the 9th as their last night. "
    "A blank last night means still there."
)


def _flash(kind: str, text: str) -> None:
    ss["_rooms_flash"] = (kind, text)


def _bump(name: str) -> None:
    ss[f"_rooms_ver_{name}"] = ss.get(f"_rooms_ver_{name}", 0) + 1


def _ver(name: str) -> int:
    return ss.get(f"_rooms_ver_{name}", 0)


flash = ss.pop("_rooms_flash", None)
if flash:
    getattr(st, flash[0])(flash[1])

everyone = db.list_people(conn)
people_by_name = {p["name"]: p["id"] for p in everyone}  # House refused by rooms.py
# Colored name chips: option i is shown in color i.
chip_names = [p["name"] for p in everyone]
chip_colors = [p["color"] or "#9e9e9e" for p in everyone]

# --- room percents ---------------------------------------------------------------------

st.header("Room percents")
st.caption(
    "Add, rename or delete rooms and set their percents here; everything is saved "
    "together and must total exactly 100%. Changes apply to rent splits written "
    "afterwards; existing splits keep theirs. A room with assignments can't be "
    "deleted until they are."
)

room_rows = ru.room_rows(conn)
rooms_key = f"rooms_table_{_ver('rooms')}"


def _save_rooms(key: str) -> None:
    rows = ru.room_rows(conn)
    try:
        ru.save_room_table(conn, rows, ss.get(key))
    except rooms.RoomError as exc:
        _flash("error", str(exc))
        return
    _bump("rooms")
    _flash("success", "Saved the rooms.")


st.data_editor(
    pd.DataFrame(room_rows, columns=["id", ru.ROOM, ru.PERCENT, ru.NOW]),
    key=rooms_key,
    num_rows="dynamic",
    hide_index=True,
    column_order=[ru.ROOM, ru.PERCENT, ru.NOW],
    disabled=[ru.NOW],
    column_config={
        ru.ROOM: st.column_config.TextColumn(ru.ROOM, required=True),
        ru.PERCENT: st.column_config.NumberColumn(
            ru.PERCENT, min_value=0.0, max_value=100.0, format="plain",
            help="Exactly as entered; nothing is rounded or rescaled.",
        ),
        ru.NOW: st.column_config.MultiselectColumn(
            ru.NOW, options=chip_names, color=chip_colors,
            help="Who is in the room tonight (blank = vacant).",
        ),
    },
    alt="Rooms with their percent of the rent and current occupant",
)
room_changes = ss.get(rooms_key)
percents = ru.room_percents_after(room_rows, room_changes)
if any(ru.is_blank(p) for p in percents):
    total_error = "Give every room a percent."
elif percents:
    total_error = splits.percent_total_error(percents)
else:
    total_error = rooms.percent_error(conn) if not ru.has_changes(room_changes) else None
st.markdown(f"**Total: {ru.exact_total(percents)}%**")
if total_error:
    st.warning(total_error)
c1, c2, _ = st.columns([1, 1, 4])
c1.button(
    "Save rooms", key="rooms_save", type="primary", on_click=_save_rooms, args=(rooms_key,),
    disabled=not ru.has_changes(room_changes) or bool(total_error), width="stretch",
)
c2.button(
    "Discard changes", key="rooms_discard", on_click=_bump, args=("rooms",),
    disabled=not ru.has_changes(room_changes), width="stretch",
)

# --- assignments per room ------------------------------------------------------------------

st.header("Who was in each room")
st.caption(
    "Newest first by first night. Add, edit or delete rows in place, then save the "
    "room's table: all its changes are saved together, or none if anything is refused. "
    "Pick one person per row. Rows the upgrade filled in from the old active months "
    "say \"seeded, check\" until you edit or confirm them. Vacant spans are worked "
    "out from the gaps and listed under each table."
)


def _save_assignments(room_id: int, key: str) -> None:
    rows = ru.assignment_rows(conn, room_id)
    try:
        ru.save_assignment_table(conn, room_id, rows, ss.get(key), people_by_name)
    except rooms.RoomError as exc:
        ss[f"_rooms_error_{room_id}"] = str(exc)
        return
    ss.pop(f"_rooms_error_{room_id}", None)
    _bump(f"room_{room_id}")
    _flash("success", f"Saved {rooms.get_room(conn, room_id)['name']}.")


def _discard_assignments(room_id: int) -> None:
    ss.pop(f"_rooms_error_{room_id}", None)
    _bump(f"room_{room_id}")


def _confirm(room_id: int, name: str) -> None:
    n = ru.confirm_seeded(conn, room_id)
    _bump(f"room_{room_id}")
    _flash("success", f"Confirmed {n} row{'s' if n != 1 else ''} in {name}.")


all_rooms = rooms.list_rooms(conn)
if not all_rooms:
    st.info("No rooms yet. Add one above.")
for room in all_rooms:
    rid, name = room["id"], room["name"]
    rows = ru.assignment_rows(conn, rid)
    seeded = sum(r["seeded"] for r in rows)
    key = f"rooms_assign_{rid}_{_ver(f'room_{rid}')}"
    with st.container(border=True, key=f"rooms_room_{rid}"):
        st.subheader(name)
        error = ss.get(f"_rooms_error_{rid}")
        if error:
            st.error(error)
        table = pd.DataFrame(rows, columns=["id", ru.PERSON, ru.FIRST, ru.LAST, ru.STATUS])
        for col in (ru.FIRST, ru.LAST):  # real date columns, even when all blank
            table[col] = pd.to_datetime(table[col])
        st.data_editor(
            table,
            key=key,
            num_rows="dynamic",
            hide_index=True,
            column_order=[ru.PERSON, ru.FIRST, ru.LAST, ru.STATUS],
            disabled=[ru.STATUS],
            column_config={
                ru.PERSON: st.column_config.MultiselectColumn(
                    ru.PERSON, options=chip_names, color=chip_colors, required=True,
                    help="Pick one person.",
                ),
                ru.FIRST: ui.date_column(ru.FIRST, required=True),
                ru.LAST: ui.date_column(
                    ru.LAST, help="Last night in the room; blank = still there."
                ),
                ru.STATUS: st.column_config.TextColumn(ru.STATUS),
            },
            alt=f"Who was in {name}, newest first",
        )
        changes = ss.get(key)
        b1, b2, b3, _ = st.columns([1, 1, 1, 2])
        b1.button(
            "Save", key=f"rooms_assign_save_{rid}", type="primary",
            on_click=_save_assignments, args=(rid, key),
            disabled=not ru.has_changes(changes), width="stretch",
        )
        b2.button(
            "Discard changes", key=f"rooms_assign_discard_{rid}",
            on_click=_discard_assignments, args=(rid,),
            disabled=not ru.has_changes(changes), width="stretch",
        )
        b3.button(
            f"Confirm seeded rows ({seeded})", key=f"rooms_confirm_{rid}",
            on_click=_confirm, args=(rid, name), disabled=not seeded, width="stretch",
            help="The seeded rows look right as they are.",
        )
        gaps = ru.vacant_rows(conn, rid)
        if gaps:
            st.markdown(":gray[**Vacant** (worked out from the gaps, not editable)]")
            st.container(key=f"rooms_vacant_{rid}").dataframe(
                pd.DataFrame(gaps, columns=[ru.FIRST, ru.LAST, "Nights"]),
                hide_index=True,
                column_config={
                    ru.FIRST: ui.date_column(ru.FIRST),
                    ru.LAST: ui.date_column(ru.LAST, help="Blank = vacant from then on."),
                    "Nights": st.column_config.NumberColumn("Nights", format="%d"),
                },
                alt=f"Vacant spans in {name}",
            )

# --- timeline ---------------------------------------------------------------------------

st.header("Timeline")
st.caption(
    "One row per date any room changes hands, newest first: who is in each room from "
    "that night until the next row's date."
)
names, _ = ru.timeline_frame(conn)
if names.empty:
    st.info("Nobody has been assigned to a room yet.")
else:
    st.container(key="rooms_timeline").dataframe(
        ru.timeline_styler(conn).format_index(ui.format_date, axis=0),
        lazy=False,
        alt="Timeline of who was in each room",
    )

# --- not in a room --------------------------------------------------------------------------

missing = people.not_in_a_room(conn)
if missing:
    st.header("Not in a room")
    st.caption(
        "The upgrade could not give these people a room (they weren't in the Rent "
        "default split). Until they are put in a room above they don't count as in "
        "the house. Dismiss someone who doesn't need a room; their old months are "
        "then forgotten."
    )

    def _dismiss(pid: int, pname: str) -> None:
        people.dismiss_not_in_a_room(conn, pid)
        _flash("success", f"Dismissed {pname}.")

    for p in missing:
        start, until = p["active_from"], p["active_until"]
        if start and until:
            old = f"{ui.format_month(start)} to {ui.format_month(until)}"
        elif start:
            old = f"{ui.format_month(start)} onwards"
        elif until:
            old = f"until {ui.format_month(until)}"
        else:
            old = "none set"
        with st.container(horizontal=True, vertical_alignment="center", key=f"rooms_missing_{p['id']}"):
            st.markdown(f"**{p['name']}** · old active months: {old}")
            st.button(
                "Dismiss", key=f"rooms_dismiss_{p['id']}", on_click=_dismiss,
                args=(p["id"], p["name"]), help="Forget their old months; give them no room.",
            )
