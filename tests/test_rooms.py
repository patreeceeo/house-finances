import pytest

from helpers import house_id

from housefin import db, rooms
from housefin.rooms import RoomError


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    """The rooms and people of README's worked example."""
    ids = {
        "Front": rooms.add_room(conn, "Front", 40),
        "Back": rooms.add_room(conn, "Back", 33.375),
        "Attic": rooms.add_room(conn, "Attic", 20),
        "Nook": rooms.add_room(conn, "Nook", 6.625),
    }
    for name in ("Ann", "Bob", "Cy", "Moana"):
        ids[name] = db.add_person(conn, name)
    return ids


# --- dates ---------------------------------------------------------------------


def person_nights(occ):
    """Nights each person spent in any room in the month."""
    out = {}
    for room in occ.rooms:
        for pid, n in room.days.items():
            out[pid] = out.get(pid, 0) + n
    return out


def test_check_date():
    assert rooms.check_date(None) is None
    assert rooms.check_date("  ") is None
    assert rooms.check_date(" 2026-07-01 ") == "2026-07-01"
    for bad in ("2026-7-1", "July 1", "2026-02-30", "2026-13-01"):
        with pytest.raises(RoomError):
            rooms.check_date(bad)


# --- rooms -----------------------------------------------------------------------


def test_room_crud(conn):
    a = rooms.add_room(conn, "  Front   room ", 60)
    b = rooms.add_room(conn, "Back", 40)
    assert [r["name"] for r in rooms.list_rooms(conn)] == ["Back", "Front room"]
    with pytest.raises(RoomError, match="already a room"):
        rooms.add_room(conn, "front ROOM", 10)
    with pytest.raises(RoomError, match="empty"):
        rooms.add_room(conn, "  ", 10)
    for bad in (0, -5, "abc", float("nan"), None):
        with pytest.raises(RoomError):
            rooms.add_room(conn, "Attic", bad)
    rooms.rename_room(conn, a, "Front")
    rooms.rename_room(conn, a, "front")  # case change of itself is fine
    with pytest.raises(RoomError, match="already a room"):
        rooms.rename_room(conn, b, "FRONT")
    rooms.delete_room(conn, b)
    assert [r["name"] for r in rooms.list_rooms(conn)] == ["front"]
    with pytest.raises(RoomError):
        rooms.get_room(conn, b)


def test_delete_room_with_history_refused(conn, house):
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01", "2026-01-31")
    with pytest.raises(RoomError, match="Front has 1 assignment"):
        rooms.delete_room(conn, house["Front"])


def test_percent_error(conn, house):
    assert rooms.percent_error(conn) is None
    rooms.add_room(conn, "Shed", 1)
    assert rooms.percent_error(conn) == "Percents total 101%; take off 1%."
    empty = db.connect(":memory:")
    assert rooms.percent_error(empty) == "There are no rooms yet."


def test_set_room_percents_exact(conn, house):
    ids = [house[n] for n in ("Front", "Back", "Attic", "Nook")]
    rooms.set_room_percents(conn, dict(zip(ids, (30, 33.3, 30, 6.7))))
    assert {r["name"]: r["percent"] for r in rooms.list_rooms(conn)} == {
        "Front": 30, "Back": 33.3, "Attic": 30, "Nook": 6.7,
    }
    # 0.001% short is still refused; nothing is rescaled or saved.
    with pytest.raises(RoomError, match=r"0\.001% is still unassigned"):
        rooms.set_room_percents(conn, dict(zip(ids, (30, 33.299, 30, 6.7))))
    with pytest.raises(RoomError, match="take off"):
        rooms.set_room_percents(conn, dict(zip(ids, (40, 33.3, 30, 6.7))))
    assert rooms.get_room(conn, house["Back"])["percent"] == 33.3
    with pytest.raises(RoomError, match="missing: Nook"):
        rooms.set_room_percents(conn, dict(zip(ids[:3], (40, 40, 20))))
    with pytest.raises(RoomError, match="more than 0"):
        rooms.set_room_percents(conn, dict(zip(ids, (60, 40, 0, 0))))
    with pytest.raises(RoomError, match="No room"):
        rooms.set_room_percents(conn, {**dict(zip(ids, (40, 30, 20, 10))), 999: 0.5})


# --- assignments -------------------------------------------------------------------


def test_add_and_list_assignments(conn, house):
    a1 = rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01", "2026-03-31")
    a2 = rooms.add_assignment(conn, house["Front"], house["Bob"], "2026-04-01")
    a3 = rooms.add_assignment(conn, house["Back"], house["Ann"], "2026-04-01", None)
    rows = rooms.list_assignments(conn, house["Front"])
    assert [r["id"] for r in rows] == [a2, a1]  # newest first
    assert rows[0]["person_name"] == "Bob" and rows[0]["end_date"] is None
    assert rows[0]["seeded"] == 0
    assert [r["id"] for r in rooms.list_assignments(conn, person_id=house["Ann"])] == [a3, a1]
    assert len(rooms.list_assignments(conn)) == 3
    assert rooms.get_assignment(conn, a3)["room_name"] == "Back"


def test_room_overlap_refused_naming_person_and_dates(conn, house):
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01", "2026-03-31")
    with pytest.raises(RoomError) as e:
        rooms.add_assignment(conn, house["Front"], house["Bob"], "2026-03-31", "2026-05-01")
    assert str(e.value) == (
        "Front already has Ann 2026-01-01 to 2026-03-31; a room holds one person at a time."
    )
    rooms.add_assignment(conn, house["Front"], house["Bob"], "2026-04-01")
    with pytest.raises(RoomError, match="Front already has Bob 2026-04-01 onwards"):
        rooms.add_assignment(conn, house["Front"], house["Cy"], "2030-01-01", "2030-01-01")
    # An ongoing new one overlapping an existing later one.
    rooms.add_assignment(conn, house["Back"], house["Cy"], "2026-06-01", "2026-06-30")
    with pytest.raises(RoomError, match="Back already has Cy 2026-06-01 to 2026-06-30"):
        rooms.add_assignment(conn, house["Back"], house["Moana"], "2026-05-01")


def test_person_overlap_refused(conn, house):
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01")
    with pytest.raises(RoomError) as e:
        rooms.add_assignment(conn, house["Back"], house["Ann"], "2025-12-01", "2026-01-01")
    assert str(e.value) == (
        "Ann is already in Front 2026-01-01 onwards (ongoing); "
        "a person can be in only one room a night."
    )
    rooms.add_assignment(conn, house["Back"], house["Ann"], "2025-12-01", "2025-12-31")


def test_house_and_bad_input_refused(conn, house):
    with pytest.raises(RoomError, match="House can't be assigned"):
        rooms.add_assignment(conn, house["Front"], house_id(conn), "2026-01-01")
    with pytest.raises(RoomError, match="before the first night"):
        rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-02-01", "2026-01-31")
    with pytest.raises(RoomError, match="first night"):
        rooms.add_assignment(conn, house["Front"], house["Ann"], "")
    with pytest.raises(RoomError, match="YYYY-MM-DD"):
        rooms.add_assignment(conn, house["Front"], house["Ann"], "1/2/2026")
    with pytest.raises(RoomError, match="No person"):
        rooms.add_assignment(conn, house["Front"], 999, "2026-01-01")
    with pytest.raises(RoomError, match="No room"):
        rooms.add_assignment(conn, 999, house["Ann"], "2026-01-01")
    assert rooms.list_assignments(conn) == []


def test_one_night_assignment(conn, house):
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-05", "2026-01-05")
    with pytest.raises(RoomError, match="Front already has Ann 2026-01-05;"):
        rooms.add_assignment(conn, house["Front"], house["Bob"], "2026-01-05")
    rooms.add_assignment(conn, house["Front"], house["Bob"], "2026-01-06")


def test_update_assignment(conn, house):
    a = rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01", "2026-03-31")
    b = rooms.add_assignment(conn, house["Front"], house["Bob"], "2026-04-01")
    # Moving its own dates doesn't clash with itself.
    rooms.update_assignment(conn, a, house["Front"], house["Ann"], "2026-01-15", "2026-03-31")
    with pytest.raises(RoomError, match="Front already has Bob"):
        rooms.update_assignment(conn, a, house["Front"], house["Ann"], "2026-01-15", "2026-04-01")
    assert rooms.get_assignment(conn, a)["end_date"] == "2026-03-31"
    rooms.update_assignment(conn, b, house["Back"], house["Cy"], "2026-04-01", None)
    row = rooms.get_assignment(conn, b)
    assert (row["room_name"], row["person_name"]) == ("Back", "Cy")
    with pytest.raises(RoomError, match="No room assignment"):
        rooms.update_assignment(conn, 999, house["Back"], house["Cy"], "2026-04-01")


def test_editing_or_confirming_clears_seeded(conn, house):
    a = rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01")
    b = rooms.add_assignment(conn, house["Back"], house["Bob"], "2026-01-01")
    conn.execute("UPDATE room_assignments SET seeded = 1")
    conn.commit()
    rooms.confirm_assignment(conn, a)
    rooms.update_assignment(conn, b, house["Back"], house["Bob"], "2026-01-02")
    assert [r["seeded"] for r in rooms.list_assignments(conn)] == [0, 0]


def test_delete_assignment(conn, house):
    a = rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01")
    rooms.delete_assignment(conn, a)
    assert rooms.list_assignments(conn) == []
    with pytest.raises(RoomError):
        rooms.delete_assignment(conn, a)


# --- who was where -------------------------------------------------------------------


def test_vacancies(conn, house):
    f = house["Front"]
    assert rooms.vacancies(conn, f) == []
    assert rooms.vacancies(conn, f, start="2026-01-01", end="2026-01-31") == [
        ("2026-01-01", "2026-01-31")
    ]
    rooms.add_assignment(conn, f, house["Ann"], "2026-01-01", "2026-01-31")
    rooms.add_assignment(conn, f, house["Bob"], "2026-02-10", "2026-02-28")
    rooms.add_assignment(conn, f, house["Cy"], "2026-03-01", "2026-03-20")
    assert rooms.vacancies(conn, f) == [("2026-02-01", "2026-02-09"), ("2026-03-21", None)]
    assert rooms.vacancies(conn, f, start="2025-12-25", end="2026-02-05") == [
        ("2025-12-25", "2025-12-31"), ("2026-02-01", "2026-02-05"),
    ]
    assert rooms.vacancies(conn, f, start="2026-02-15") == [("2026-03-21", None)]
    rooms.add_assignment(conn, f, house["Moana"], "2026-04-01")
    assert rooms.vacancies(conn, f) == [
        ("2026-02-01", "2026-02-09"), ("2026-03-21", "2026-03-31"),
    ]
    assert rooms.vacancies(conn, f, start="2026-05-01", end="2026-05-31") == []


def test_occupants_and_room_of(conn, house):
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01", "2026-01-31")
    rooms.add_assignment(conn, house["Back"], house["Ann"], "2026-02-01")
    assert rooms.occupants(conn, "2026-01-31") == {
        house["Front"]: house["Ann"], house["Back"]: None,
        house["Attic"]: None, house["Nook"]: None,
    }
    assert rooms.occupants(conn, "2026-02-01")[house["Back"]] == house["Ann"]
    assert rooms.room_of(conn, house["Ann"], "2026-01-15") == house["Front"]
    assert rooms.room_of(conn, house["Ann"], "2027-01-15") == house["Back"]
    assert rooms.room_of(conn, house["Ann"], "2025-12-31") is None


def test_timeline(conn, house):
    f, b = house["Front"], house["Back"]
    rooms.add_assignment(conn, f, house["Ann"], "2026-01-01", "2026-01-31")
    rooms.add_assignment(conn, f, house["Ann"], "2026-02-01", "2026-02-14")  # no change
    rooms.add_assignment(conn, f, house["Bob"], "2026-03-01")
    rooms.add_assignment(conn, b, house["Cy"], "2026-01-01", "2026-03-01")
    rows = rooms.timeline(conn)
    assert [r["date"] for r in rows] == ["2026-03-02", "2026-03-01", "2026-02-15", "2026-01-01"]
    by_date = {r["date"]: r["rooms"] for r in rows}
    assert by_date["2026-01-01"][f] == house["Ann"] and by_date["2026-01-01"][b] == house["Cy"]
    assert by_date["2026-02-15"][f] is None and by_date["2026-02-15"][b] == house["Cy"]
    assert by_date["2026-03-01"][f] == house["Bob"]
    assert by_date["2026-03-02"][b] is None and by_date["2026-03-02"][f] == house["Bob"]
    assert set(by_date["2026-03-02"]) == {f, b, house["Attic"], house["Nook"]}


def test_timeline_empty(conn, house):
    assert rooms.timeline(conn) == []


def test_month_occupancy_worked_example(conn, house):
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-01-01")
    rooms.add_assignment(conn, house["Back"], house["Bob"], "2026-06-01", "2026-08-31")
    rooms.add_assignment(conn, house["Attic"], house["Cy"], "2026-05-01", "2026-07-10")
    rooms.add_assignment(conn, house["Nook"], house["Moana"], "2026-07-15")
    occ = rooms.month_occupancy(conn, "2026-07")
    assert occ.month == "2026-07" and occ.days_in_month == 31
    got = {r.name: r for r in occ.rooms}
    assert got["Front"].days == {house["Ann"]: 31} and got["Front"].vacant == []
    assert got["Back"].days == {house["Bob"]: 31}
    assert got["Attic"].days == {house["Cy"]: 10}
    assert got["Attic"].vacant == [("2026-07-11", "2026-07-31")]
    assert got["Attic"].vacant_days == 21
    assert got["Nook"].days == {house["Moana"]: 17}
    assert got["Nook"].vacant == [("2026-07-01", "2026-07-14")]
    assert got["Nook"].vacant_days == 14
    assert got["Back"].percent == 33.375
    for r in occ.rooms:
        assert sum(r.days.values()) + r.vacant_days == 31
    assert person_nights(occ) == {
        house["Ann"]: 31, house["Bob"]: 31, house["Cy"]: 10, house["Moana"]: 17,
    }
    assert not occ.fully_occupied
    assert rooms.month_occupancy(conn, "2026-08").rooms[0].name == "Attic"


def test_month_occupancy_same_person_twice_in_room_and_moving(conn, house):
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-02-01", "2026-02-10")
    rooms.add_assignment(conn, house["Front"], house["Ann"], "2026-02-20")
    rooms.add_assignment(conn, house["Back"], house["Ann"], "2026-02-11", "2026-02-19")
    rooms.add_assignment(conn, house["Back"], house["Bob"], "2026-02-20")
    occ = rooms.month_occupancy(conn, "2026-02")
    got = {r.name: r for r in occ.rooms}
    assert occ.days_in_month == 28
    assert got["Front"].days == {house["Ann"]: 19}
    assert got["Front"].vacant == [("2026-02-11", "2026-02-19")]
    assert got["Back"].days == {house["Ann"]: 9, house["Bob"]: 9}
    assert got["Back"].vacant == [("2026-02-01", "2026-02-10")]
    assert person_nights(occ)[house["Ann"]] == 28
    with pytest.raises(ValueError):
        rooms.month_occupancy(conn, "2026-2")


def test_month_occupancy_full_house(conn):
    r = rooms.add_room(conn, "Only", 100)
    p = db.add_person(conn, "Ann")
    rooms.add_assignment(conn, r, p, "2026-01-01")
    occ = rooms.month_occupancy(conn, "2026-03")
    assert occ.fully_occupied and occ.rooms[0].days == {p: 31}
    assert rooms.month_occupancy(conn, "2025-12").rooms[0].vacant_days == 31
