"""Purposes page: add, rename, delete (if unused) and edit default splits."""

import streamlit as st

from housefin import db, fmt, purposes, splits, ui

st.set_page_config(page_title="Purposes · House Finances", layout="wide")
conn = ui.get_conn()
ss = st.session_state

st.title("Purposes")
st.caption(
    "Each withdrawal gets a purpose. A purpose's default split is where a new "
    "withdrawal's split starts; changing it never changes past withdrawals. "
    "Rent is split by rooms instead (see the Rooms page)."
)


def _run(action, ok: str) -> bool:
    try:
        action()
    except purposes.PurposeError as exc:
        ui.flash("purposes", str(exc), "error")
        return False
    ui.flash("purposes", ok)
    return True


def _add() -> None:
    name = ss.get("purpose_new_name", "")
    if _run(lambda: purposes.add(conn, name), f"Added {name.strip()}."):
        ss["purpose_new_name"] = ""


def _rename(pid: int, old: str) -> None:
    new = ss.get(f"purpose_name_{pid}", "")
    _run(lambda: purposes.rename(conn, pid, new), f"Renamed {old} to {new.strip()}.")


def _delete(pid: int, name: str) -> None:
    _run(lambda: purposes.delete(conn, pid), f"Deleted {name}.")


def _bump(pid: int) -> None:
    ss[f"purpose_ver_{pid}"] = ss.get(f"purpose_ver_{pid}", 0) + 1


def _save_split(pid: int, name: str, person_ids: list[int], ver: int) -> None:
    weights = {p: float(ss.get(f"purpose_w_{pid}_{ver}_{p}") or 0) for p in person_ids}
    shown = splits.to_percents(splits.get_purpose_split(conn, pid))
    if all(weights[p] == shown.get(p, 0.0) for p in person_ids):
        # Unchanged: the stored weights are exact; what is shown may be rounded.
        ui.flash("purposes", f"No changes to the default split for {name}.", "info")
        return
    if _run(
        lambda: purposes.save_default_split(conn, pid, weights),
        f"Saved the default split for {name}; it applies to future withdrawals.",
    ):
        _bump(pid)


def _clear_split(pid: int, name: str) -> None:
    if _run(
        lambda: purposes.clear_default_split(conn, pid),
        f"{name} now splits evenly among active housemates.",
    ):
        _bump(pid)


ui.show_flash("purposes")

with st.form("add_purpose", clear_on_submit=False, border=False):
    c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
    c1.text_input("New purpose", key="purpose_new_name", placeholder="e.g. Gas")
    c2.form_submit_button("Add purpose", on_click=_add, width="stretch")

people = {p["id"]: p for p in db.list_people(conn)}
candidates = purposes.split_candidates(conn)
even = purposes.even_split_people(conn)
even_names = ", ".join(p["name"] for p in even) or "nobody (no active housemates)"


def _summary(stored: list[tuple[int, float]]) -> str:
    if not stored:
        return "even split among active housemates"
    pct = splits.to_percents(stored)
    return ", ".join(
        f"{people[pid]['name'] if pid in people else pid} {fmt.percent(pct[pid])}"
        for pid, _ in stored
    )


for p in purposes.list_with_usage(conn):
    pid, name = p["id"], p["name"]
    stored = [] if p["excluded"] else splits.get_purpose_split(conn, pid)
    stored = [] if p["split_by_rooms"] else stored
    if p["excluded"]:
        summary = "built in, no split"
    elif p["split_by_rooms"]:
        summary = "split by rooms"
    else:
        summary = f"default: {_summary(stored)}"
    label = f"**{name}** — {summary}"
    # Keyed + stateful so it stays open when its label (the summary) changes.
    with st.expander(label, key=f"purpose_exp_{pid}", on_change="rerun"):
        if p["protected"]:
            st.caption(
                f"{name} is built in for moving money between the House's own accounts. "
                "It is left out of reports and balances, has no split, and cannot be "
                "renamed or deleted."
            )
        else:
            c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="bottom")
            c1.text_input("Name", value=name, key=f"purpose_name_{pid}")
            c2.button(
                "Rename", key=f"purpose_rename_{pid}", on_click=_rename, args=(pid, name),
                width="stretch",
            )
            used = purposes.describe_usage(p["usage"])
            c3.button(
                "Delete", key=f"purpose_delete_{pid}", on_click=_delete, args=(pid, name),
                disabled=not p["deletable"], width="stretch",
                help=(
                    "Split by rooms; cannot be deleted" if p["split_by_rooms"]
                    else f"In use by {used}" if used else "Delete this unused purpose"
                ),
            )
            if p["split_by_rooms"]:
                st.caption(f"{name} is split by rooms, so it can be renamed but not deleted.")
            elif used:
                st.caption(f"Used by {used}; it can be deleted once nothing uses it.")

        if p["excluded"]:
            continue

        if p["split_by_rooms"]:
            st.markdown("**Split by rooms**")
            st.info(
                f"{name} has no default split. Each payment is split by who was in each "
                "room on each night of its month, using the room percents. Set those and "
                "who is in which room on the [Rooms page](Rooms). A month with a vacant room is "
                "not split automatically: the payment waits for review on the Categorize "
                "page until someone says who covers the vacant share."
            )
            continue

        st.markdown("**Default split**")
        if not stored:
            st.info(f"No default: even split among active housemates ({even_names}).")
        ver = ss.get(f"purpose_ver_{pid}", 0)
        current = splits.to_percents(stored)
        values = {}
        for person in candidates:
            c1, c2, c3 = st.columns([3, 2, 1], vertical_alignment="center")
            c1.write(person["name"] + (" (House)" if person["is_house"] else ""))
            values[person["id"]] = c2.number_input(
                f"Percent for {person['name']}", min_value=0.0, max_value=100.0,
                step=1.0, format="%g", value=float(current.get(person["id"], 0.0)),
                key=f"purpose_w_{pid}_{ver}_{person['id']}", label_visibility="collapsed",
            )
            c3.write("%" if values[person["id"]] > 0 else "not in split")
        error = splits.percent_total_error(values.values()) if any(values.values()) else None
        if error:
            st.warning(error)
        elif not any(values.values()):
            st.caption("All percents are 0: saving means an even split among active housemates.")
        hidden = [i for i, _ in stored if i not in values]
        if hidden:
            st.caption(
                "Also in the stored default but not currently active (kept; left out of "
                "transactions in months they are inactive, which are then flagged for review): " + ", ".join(people[i]["name"] for i in hidden if i in people)
            )
        ids = [c["id"] for c in candidates]
        b1, b2, _ = st.columns([1, 1, 3])
        b1.button(
            "Save default split", key=f"purpose_save_{pid}", on_click=_save_split,
            args=(pid, name, ids, ver), type="primary", disabled=bool(error),
            width="stretch",
        )
        b2.button(
            "Use even split", key=f"purpose_clear_{pid}", on_click=_clear_split,
            args=(pid, name), disabled=not stored, width="stretch",
        )
