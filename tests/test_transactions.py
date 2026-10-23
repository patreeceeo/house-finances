"""db.transaction: nesting, savepoint rollback, atomic multi-step actions."""

import pytest

from helpers import add_resident

from housefin import categorize as cat
from housefin import db, rules, splits

from test_categorize import fill

TABLES = ("people", "purposes", "transactions", "rules", "splits", "purpose_splits", "imports")


def snapshot(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in TABLES}


@pytest.fixture
def conn():
    return db.connect(":memory:")


def crash_after(monkeypatch, module, name):
    """Make ``module.name`` do its work, then raise."""
    orig = getattr(module, name)

    def wrapper(*args, **kwargs):
        orig(*args, **kwargs)
        raise RuntimeError(f"boom after {name}")

    monkeypatch.setattr(module, name, wrapper)


def test_nested_calls_commit_only_at_outermost(tmp_path):
    path = tmp_path / "t.db"
    conn, other = db.connect(path), db.connect(path)
    seen = lambda: other.execute("SELECT COUNT(*) FROM people").fetchone()[0]  # noqa: E731
    before = seen()
    with db.transaction(conn):
        a = add_resident(conn, "Alice")  # its own db.transaction joins this one
        splits.set_purpose_split(conn, db.get_purpose_id(conn, "Rent"), [(a, 1)])
        assert conn.in_transaction
        assert seen() == before  # nothing committed yet
    assert not conn.in_transaction
    assert seen() == before + 1
    assert other.execute("SELECT COUNT(*) FROM purpose_splits").fetchone()[0] == 1


def test_outer_failure_rolls_back_completed_inner_steps(conn):
    before = snapshot(conn)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            add_resident(conn, "Alice")
            db.add_purpose(conn, "Snacks")
            raise RuntimeError("later step failed")
    assert snapshot(conn) == before
    assert not conn.in_transaction


def test_failing_inner_step_rolls_back_alone(conn):
    with db.transaction(conn):
        add_resident(conn, "Alice")
        with pytest.raises(RuntimeError):
            with db.transaction(conn):
                add_resident(conn, "Bob")
                raise RuntimeError("inner")
        assert conn.in_transaction  # outer transaction still open
    names = {p["name"] for p in db.list_people(conn)}
    assert "Alice" in names and "Bob" not in names


def test_categorize_save_failure_partway_leaves_db_unchanged(conn, monkeypatch):
    people, tx = fill(conn)
    before = snapshot(conn)
    # Crash at the last step (default splits for rule-tagged rows), after the
    # tag, the split, the remembered default and the rule were all written.
    crash_after(monkeypatch, splits, "apply_default_split")
    with pytest.raises(RuntimeError):
        cat.save_withdrawal(
            conn, tx["rent"], db.get_purpose_id(conn, "Rent"),
            [(people["Alice"], 1), (people["Bob"], 1)],
            remember_split=True, remember=True, applies_month="2026-03",
        )
    assert snapshot(conn) == before
    assert not conn.in_transaction


def test_categorize_deposit_bad_month_keeps_old_tags(conn):
    people, tx = fill(conn)
    before = snapshot(conn)
    with pytest.raises(ValueError):
        cat.save_person(conn, tx["zelle"], people["Alice"], remember=True, applies_month="bad")
    assert snapshot(conn) == before


@pytest.mark.parametrize("action", ["update", "delete"])
def test_rule_reapply_failure_leaves_db_unchanged(conn, monkeypatch, action):
    fill(conn)
    internet = db.get_purpose_id(conn, "Internet")
    rid, applied = rules.create_rule(
        conn, "COMCAST / XFINITY", "withdrawal", match_type=rules.CONTAINS, purpose_id=internet
    )
    splits.apply_default_split(conn, applied.purpose_changed, replace=True)
    assert len(applied.updated) == 2
    before = snapshot(conn)
    crash_after(monkeypatch, rules, "_retag")
    with pytest.raises(RuntimeError):
        if action == "update":
            rules.update_rule(conn, rid, purpose_id=db.get_purpose_id(conn, "Other"), reapply=True)
        else:
            rules.delete_rule(conn, rid, reapply=True)
    assert snapshot(conn) == before
    assert not conn.in_transaction
