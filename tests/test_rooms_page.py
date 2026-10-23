"""Rooms page: the table-saving helpers (housefin/rooms_ui.py) and AppTest
tests of pages/7_Rooms.py. Synthetic temp databases only."""

import datetime as dt
from pathlib import Path

import pytest

from housefin import colors, db, people, rooms
from housefin import rooms_ui as ru

ROOT = Path(__file__).resolve().parent.parent
PAGE = str(ROOT / "pages" / "7_Rooms.py")


def _house(c):
    front = rooms.add_room(c, "Front", 60)
    back = rooms.add_room(c, "Back", 40)
    ann = db.add_person(c, "Ann")
    bob = db.add_person(c, "Bob")
    cy = db.add_person(c, "Cy")
    dee = db.add_person(c, "Dee")
    a_ann = rooms.add_assignment(c, front, ann, "2026-01-01")
    a_bob = rooms.add_assignment(c, back, bob, "2026-01-01", "2026-06-30")
    a_cy = rooms.add_assignment(c, back, cy, "2026-08-01")
    with db.transaction(c):
        c.execute("UPDATE room_assignments SET seeded = 1 WHERE id = ?", (a_ann,))
        c.execute(
            "INSERT INTO old_active_months (person_id, active_from, active_until)"
            " VALUES (?, '2025-03', '2025-09')",
            (dee,),
        )
    return dict(
        front=front, back=back, ann=ann, bob=bob, cy=cy, dee=dee,
        a_ann=a_ann, a_bob=a_bob, a_cy=a_cy,
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    return _house(conn)


def _names(conn):
    return {p["name"]: p["id"] for p in db.list_people(conn, include_house=False)}


def _spans(conn, room_id):
    return [
        (a["person_name"], a["start_date"], a["end_date"])
        for a in rooms.list_assignments(conn, room_id)
    ]


# --- room table --------------------------------------------------------------------


def test_room_rows_show_current_occupant(conn, house):
    rows = ru.room_rows(conn, "2026-07-15")
    assert [(r[ru.ROOM], r[ru.PERCENT], r[ru.NOW]) for r in rows] == [
        ("Back", 40.0, []),
        ("Front", 60.0, ["Ann"]),
    ]


def test_save_room_table_percents_rename_add(conn, house):
    rows = ru.room_rows(conn)
    changes = {
        "edited_rows": {0: {ru.PERCENT: 33.375, ru.ROOM: "Big back"}, 1: {ru.PERCENT: 40}},
        "added_rows": [{ru.ROOM: "Attic", ru.PERCENT: 26.625}],
        "deleted_rows": [],
    }
    assert ru.room_percents_after(rows, changes) == [33.375, 40, 26.625]
    assert ru.exact_total(ru.room_percents_after(rows, changes)) == "100"
    ru.save_room_table(conn, rows, changes)
    assert [(r["name"], r["percent"]) for r in rooms.list_rooms(conn)] == [
        ("Attic", 26.625), ("Big back", 33.375), ("Front", 40.0),
    ]
    assert rooms.percent_error(conn) is None


def test_save_room_table_refuses_bad_total_and_saves_nothing(conn, house):
    rows = ru.room_rows(conn)
    changes = {"edited_rows": {0: {ru.PERCENT: 39.999, ru.ROOM: "Renamed"}}}
    assert ru.exact_total(ru.room_percents_after(rows, changes)) == "99.999"
    with pytest.raises(rooms.RoomError, match="0.001% is still unassigned"):
        ru.save_room_table(conn, rows, changes)
    assert [(r["name"], r["percent"]) for r in rooms.list_rooms(conn)] == [
        ("Back", 40.0), ("Front", 60.0),
    ]


def test_delete_room_with_assignments_refused_verbatim(conn, house):
    rows = ru.room_rows(conn)
    changes = {"edited_rows": {1: {ru.PERCENT: 100}}, "deleted_rows": [0]}
    with pytest.raises(rooms.RoomError) as exc:
        ru.save_room_table(conn, rows, changes)
    assert str(exc.value) == "Back has 2 assignments; delete those first."
    assert rooms.get_room(conn, house["front"])["percent"] == 60.0


def test_delete_empty_room(conn, house):
    attic = rooms.add_room(conn, "Attic", 10)
    rows = ru.room_rows(conn)
    pos = [r["id"] for r in rows].index(attic)
    ru.save_room_table(conn, rows, {"deleted_rows": [pos]})
    assert [r["name"] for r in rooms.list_rooms(conn)] == ["Back", "Front"]


def test_blank_added_room_row_is_ignored(conn, house):
    rows = ru.room_rows(conn)
    changes = {"added_rows": [{}]}
    assert ru.room_percents_after(rows, changes) == [40.0, 60.0]
    ru.save_room_table(conn, rows, changes)
    assert len(rooms.list_rooms(conn)) == 2


# --- assignment table -------------------------------------------------------------------


def test_assignment_rows_newest_first_with_status(conn, house):
    rows = ru.assignment_rows(conn, house["back"], "2026-07-15")
    assert [(r[ru.PERSON], r[ru.FIRST], r[ru.LAST], r[ru.STATUS]) for r in rows] == [
        (["Cy"], dt.date(2026, 8, 1), None, ru.UPCOMING),
        (["Bob"], dt.date(2026, 1, 1), dt.date(2026, 6, 30), ru.ENDED),
    ]
    (ann,) = ru.assignment_rows(conn, house["front"], "2026-07-15")
    assert ann[ru.STATUS] == "seeded, check"


def test_vacant_rows_newest_first(conn, house):
    assert ru.vacant_rows(conn, house["back"]) == [
        {ru.FIRST: dt.date(2026, 7, 1), ru.LAST: dt.date(2026, 7, 31), "Nights": 31}
    ]
    rooms.update_assignment(conn, house["a_cy"], house["back"], house["cy"], "2026-08-01", "2026-09-30")
    assert ru.vacant_rows(conn, house["back"])[0] == {
        ru.FIRST: dt.date(2026, 10, 1), ru.LAST: None, "Nights": None
    }


def test_save_assignments_edit_add_delete(conn, house):
    back = house["back"]
    rows = ru.assignment_rows(conn, back)
    changes = {
        "edited_rows": {1: {ru.LAST: "2026-07-31"}},  # Bob stays through July
        "added_rows": [
            {ru.PERSON: ["Dee"], ru.FIRST: "2025-12-01T00:00:00", ru.LAST: "2025-12-31"},
            {},  # an empty row the editor added
        ],
        "deleted_rows": [],
    }
    ru.save_assignment_table(conn, back, rows, changes, _names(conn))
    assert _spans(conn, back) == [
        ("Cy", "2026-08-01", None),
        ("Bob", "2026-01-01", "2026-07-31"),
        ("Dee", "2025-12-01", "2025-12-31"),
    ]
    rows = ru.assignment_rows(conn, back)
    ru.save_assignment_table(conn, back, rows, {"deleted_rows": [2]}, _names(conn))
    assert [s[0] for s in _spans(conn, back)] == ["Cy", "Bob"]


def test_save_assignments_order_independent(conn, house):
    """Moving Cy's start earlier only fits once Bob's end moves earlier too,
    whatever order the rows are in."""
    back = house["back"]
    rows = ru.assignment_rows(conn, back)  # [Cy, Bob]
    changes = {
        "edited_rows": {
            0: {ru.FIRST: dt.date(2026, 6, 1)},
            1: {ru.LAST: dt.date(2026, 5, 31)},
        }
    }
    ru.save_assignment_table(conn, back, rows, changes, _names(conn))
    assert _spans(conn, back) == [
        ("Cy", "2026-06-01", None),
        ("Bob", "2026-01-01", "2026-05-31"),
    ]


def test_save_assignments_overlap_refused_nothing_saved(conn, house):
    back = house["back"]
    rows = ru.assignment_rows(conn, back)
    changes = {
        "edited_rows": {1: {ru.LAST: "2026-07-31"}},  # fine on its own
        "added_rows": [{ru.PERSON: ["Dee"], ru.FIRST: "2026-03-01"}],  # clashes with Bob
    }
    with pytest.raises(rooms.RoomError) as exc:
        ru.save_assignment_table(conn, back, rows, changes, _names(conn))
    assert str(exc.value) == (
        "Back already has Bob 2026-01-01 to 2026-07-31; a room holds one person at a time."
    )
    assert _spans(conn, back) == [
        ("Cy", "2026-08-01", None),
        ("Bob", "2026-01-01", "2026-06-30"),
    ]


def test_save_assignments_person_in_two_rooms_refused(conn, house):
    rows = ru.assignment_rows(conn, house["back"])
    changes = {"added_rows": [{ru.PERSON: ["Ann"], ru.FIRST: "2026-07-01", ru.LAST: "2026-07-31"}]}
    with pytest.raises(rooms.RoomError, match="Ann is already in Front"):
        ru.save_assignment_table(conn, house["back"], rows, changes, _names(conn))


@pytest.mark.parametrize(
    "person, message",
    [([], "Pick a person"), (["Ann", "Bob"], "Pick one person per row, not 2 (Ann, Bob)")],
)
def test_save_assignments_needs_exactly_one_person(conn, house, person, message):
    rows = ru.assignment_rows(conn, house["back"])
    changes = {"added_rows": [{ru.PERSON: person, ru.FIRST: "2027-01-01"}]}
    with pytest.raises(rooms.RoomError, match=message.replace("(", r"\(").replace(")", r"\)")):
        ru.save_assignment_table(conn, house["back"], rows, changes, _names(conn))


def test_editing_seeded_row_clears_mark_but_unchanged_edit_does_not(conn, house):
    front = house["front"]
    rows = ru.assignment_rows(conn, front)
    # The editor reports a touched cell even when its value is unchanged.
    ru.save_assignment_table(
        conn, front, rows, {"edited_rows": {0: {ru.FIRST: "2026-01-01"}}}, _names(conn)
    )
    assert rooms.get_assignment(conn, house["a_ann"])["seeded"] == 1
    ru.save_assignment_table(
        conn, front, rows, {"edited_rows": {0: {ru.FIRST: "2026-01-02"}}}, _names(conn)
    )
    assert rooms.get_assignment(conn, house["a_ann"])["seeded"] == 0


def test_confirm_seeded(conn, house):
    assert ru.confirm_seeded(conn, house["front"]) == 1
    assert ru.confirm_seeded(conn, house["front"]) == 0
    assert ru.assignment_rows(conn, house["front"], "2026-07-15")[0][ru.STATUS] == ru.CURRENT


# --- timeline -----------------------------------------------------------------------------


def test_timeline_frame_and_styles(conn, house):
    names, ids = ru.timeline_frame(conn)
    assert names.index.name == "From"
    assert list(names.index) == ["2026-08-01", "2026-07-01", "2026-01-01"]
    assert list(names.columns) == ["Back", "Front"]
    assert names.loc["2026-07-01", "Back"] == "vacant"
    assert names.loc["2026-01-01", "Back"] == "Bob"
    assert ids.loc["2026-07-01", "Back"] is None

    styler = ru.timeline_styler(conn)
    styler._compute()
    styles = {
        (names.index[r], names.columns[c]): "; ".join(f"{k}: {v}" for k, v in css)
        for (r, c), css in styler.ctx.items()
    }
    ann_color = people.get_person(conn, house["ann"])["color"]
    assert styles[("2026-01-01", "Front")] == (
        f"background-color: {ann_color}; color: {colors.text_color_for(ann_color)}"
    )
    assert "italic" in styles[("2026-07-01", "Back")]


# --- page (AppTest) ----------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch, tmp_path):
    path = tmp_path / "rooms.db"
    c = db.connect(path)
    ids = _house(c)
    c.close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    return dict(path=path, **ids)


def _app():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(PAGE, default_timeout=30).run()


def _db(app_db):
    return db.connect(app_db["path"])


def _frames(at):
    return {d.key: d.value for d in at.dataframe if d.key}


def test_page_renders_all_parts(app_db):
    at = _app()
    assert not at.exception, at.exception
    frames = _frames(at)
    room_table = frames["rooms_table_0"]
    assert list(room_table["Room"]) == ["Back", "Front"]
    assert list(room_table["Percent of rent"]) == [40.0, 60.0]
    assert any(m.value == "**Total: 100%**" for m in at.markdown)
    assert not at.warning

    back = frames[f"rooms_assign_{app_db['back']}_0"]
    assert list(back["Person"]) == [["Cy"], ["Bob"]]
    front = frames[f"rooms_assign_{app_db['front']}_0"]
    assert list(front["Status"]) == ["seeded, check"]
    vacant = at.get_by_key(f"rooms_vacant_{app_db['back']}").dataframe[0].value
    assert list(vacant["Nights"]) == [31]

    timeline = at.get_by_key("rooms_timeline").dataframe[0].value
    assert list(timeline.index) == ["2026-08-01", "2026-07-01", "2026-01-01"]
    assert timeline.loc["2026-07-01", "Back"] == "vacant"

    assert any("**Dee**" in m.value and "Mar 2025 to Sep 2025" in m.value for m in at.markdown)
    assert at.button(key="rooms_save").disabled  # nothing edited yet


def test_page_room_percent_edit_shows_total_and_saves(app_db):
    at = _app()
    at.session_state["rooms_table_0"] = {
        "edited_rows": {0: {"Percent of rent": 39.999}}, "added_rows": [], "deleted_rows": [],
    }
    at.run()
    assert any(m.value == "**Total: 99.999%**" for m in at.markdown)
    assert at.warning[0].value == "Percents total 99.999%; 0.001% is still unassigned."
    assert at.button(key="rooms_save").disabled

    at.session_state["rooms_table_0"] = {
        "edited_rows": {0: {"Percent of rent": 39.5}, 1: {"Percent of rent": 60.5}},
        "added_rows": [], "deleted_rows": [],
    }
    at.run()
    assert not at.warning
    at.button(key="rooms_save").click().run()
    assert not at.exception, at.exception
    assert at.success[0].value == "Saved the rooms."
    c = _db(app_db)
    assert [r["percent"] for r in rooms.list_rooms(c)] == [39.5, 60.5]
    c.close()


def test_page_room_delete_refused_message_shown(app_db):
    at = _app()
    at.session_state["rooms_table_0"] = {
        "edited_rows": {1: {"Percent of rent": 100}}, "added_rows": [], "deleted_rows": [0],
    }
    at.run()
    at.button(key="rooms_save").click().run()
    assert at.error[0].value == "Back has 2 assignments; delete those first."
    c = _db(app_db)
    assert len(rooms.list_rooms(c)) == 2
    c.close()


def test_page_assignment_overlap_shown_and_nothing_saved(app_db):
    back = app_db["back"]
    at = _app()
    key = f"rooms_assign_{back}_0"
    at.session_state[key] = {
        "edited_rows": {},
        "added_rows": [{"Person": ["Dee"], "First night": "2026-03-01"}],
        "deleted_rows": [],
    }
    at.run()
    at.button(key=f"rooms_assign_save_{back}").click().run()
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [
        "Back already has Bob 2026-01-01 to 2026-06-30; a room holds one person at a time."
    ]
    c = _db(app_db)
    assert len(rooms.list_assignments(c, back)) == 2
    c.close()


def test_page_assignment_save(app_db):
    back = app_db["back"]
    at = _app()
    key = f"rooms_assign_{back}_0"
    at.session_state[key] = {
        "edited_rows": {},
        "added_rows": [{"Person": ["Dee"], "First night": "2026-07-01", "Last night": "2026-07-31"}],
        "deleted_rows": [],
    }
    at.run()
    at.button(key=f"rooms_assign_save_{back}").click().run()
    assert not at.exception, at.exception
    assert at.success[0].value == "Saved Back."
    frames = _frames(at)
    assert list(frames[f"rooms_assign_{back}_1"]["Person"]) == [["Cy"], ["Dee"], ["Bob"]]
    with pytest.raises(KeyError):  # no gap left
        at.get_by_key(f"rooms_vacant_{back}")
    # Dee now has a room, so she leaves the "Not in a room" list.
    assert not any("**Dee**" in m.value for m in at.markdown)


def test_page_confirm_seeded(app_db):
    front = app_db["front"]
    at = _app()
    at.button(key=f"rooms_confirm_{front}").click().run()
    assert at.success[0].value == "Confirmed 1 row in Front."
    assert list(_frames(at)[f"rooms_assign_{front}_1"]["Status"]) == ["in the room"]
    assert at.button(key=f"rooms_confirm_{front}").disabled


def test_page_dismiss_not_in_a_room(app_db):
    at = _app()
    at.button(key=f"rooms_dismiss_{app_db['dee']}").click().run()
    assert at.success[0].value == "Dismissed Dee."
    assert not any("Not in a room" == h.value for h in at.header)
    c = _db(app_db)
    assert people.not_in_a_room(c) == []
    c.close()


def test_page_without_rooms(monkeypatch, tmp_path):
    path = tmp_path / "empty.db"
    db.connect(path).close()
    monkeypatch.setenv("HOUSEFIN_DB", str(path))
    at = _app()
    assert not at.exception, at.exception
    assert at.warning[0].value == "There are no rooms yet."
    assert any("No rooms yet" in i.value for i in at.info)
