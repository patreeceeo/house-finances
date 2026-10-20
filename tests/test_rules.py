import pytest

from housefin import db, rules


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    # Drop the seeded Interest Paid rule so tests start from a clean slate;
    # test_seeded_interest_rule covers it separately.
    c.execute("DELETE FROM rules")
    yield c
    c.close()


_seq = iter(range(1, 10**6))


def add_txn(conn, description, amount_cents, *, source=None, person_id=None, purpose_id=None):
    n = next(_seq)
    cur = conn.execute(
        "INSERT INTO transactions (fingerprint, date, time, amount_cents, type, description,"
        " person_id, purpose_id, category_source)"
        " VALUES (?, '2026-10-01', '12:00:00', ?, ?, ?, ?, ?, ?)",
        (
            f"fp{n}",
            amount_cents,
            "Deposit" if amount_cents > 0 else "Withdrawal",
            description,
            person_id,
            purpose_id,
            source,
        ),
    )
    return cur.lastrowid


def txn(conn, tid):
    return conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()


def pid(conn, name):
    return db.get_purpose_id(conn, name)


# --- matching ----------------------------------------------------------------


def test_exact_collapses_whitespace_and_keeps_case():
    assert rules.pattern_matches("exact", "SFPUC WEB_PAY", "SFPUC   WEB_PAY ")
    assert rules.pattern_matches("exact", " SFPUC  WEB_PAY", "SFPUC WEB_PAY")
    assert not rules.pattern_matches("exact", "SFPUC WEB_PAY", "SFPUC WEB_PAY 123")
    assert not rules.pattern_matches("exact", "SFPUC WEB_PAY", "sfpuc web_pay")


def test_contains_is_case_insensitive_substring():
    assert rules.pattern_matches("contains", "instacart", "IC* INSTACART 50 Beale Street")
    assert rules.pattern_matches("contains", "COSTCO", "www costco com")
    assert not rules.pattern_matches("contains", "COSTCO", "TARGET")
    assert not rules.pattern_matches("contains", "   ", "anything")


def test_comcast_contains_rule_matches_varying_numbers(conn):
    lines = [
        add_txn(conn, "COMCAST / XFINITY 8155400012345678", -8000),
        add_txn(conn, "COMCAST / XFINITY  8155400098765432", -8100),
        add_txn(conn, "comcast / xfinity 99", -7900),
    ]
    other = add_txn(conn, "COMCAST CABLE", -5000)
    assert rules.preview_match_count(conn, "COMCAST / XFINITY", "withdrawal", "contains") == 3
    _, res = rules.create_rule(
        conn, "COMCAST / XFINITY", "withdrawal", match_type="contains", purpose_id=pid(conn, "Internet")
    )
    assert sorted(res.updated) == sorted(lines)
    for t in lines:
        assert txn(conn, t)["purpose_id"] == pid(conn, "Internet")
        assert txn(conn, t)["category_source"] == "rule"
    assert txn(conn, other)["category_source"] is None


def test_sign_restriction(conn):
    dep = add_txn(conn, "Zelle payment from ALICE", 50000)
    wd = add_txn(conn, "Zelle payment from ALICE", -50000)
    alice = db.add_person(conn, "Alice")
    _, res = rules.create_rule(conn, "Zelle payment from ALICE", "deposit", person_id=alice)
    assert res.updated == [dep]
    assert txn(conn, dep)["person_id"] == alice
    assert txn(conn, wd)["category_source"] is None
    assert rules.preview_match_count(conn, "Zelle payment from ALICE", "withdrawal") == 1


def test_applies_to_from_source_transaction_sign(conn):
    src = add_txn(conn, "COSTCO WHSE #123", -1000, source="manual", purpose_id=pid(conn, "Costco"))
    rid, _ = rules.create_rule_from_transaction(conn, src, purpose_id=pid(conn, "Costco"))
    rule = rules.get_rule(conn, rid)
    assert rule.applies_to == "withdrawal" and rule.match_type == "exact"

    alice = db.add_person(conn, "Alice")
    dep = add_txn(conn, "Zelle payment from ALICE  X", 100, source="manual", person_id=alice)
    rid2, _ = rules.create_rule_from_transaction(
        conn, dep, pattern="Zelle payment from ALICE", person_id=alice
    )
    rule2 = rules.get_rule(conn, rid2)
    assert rule2.applies_to == "deposit" and rule2.match_type == "contains"


def test_match_type_for():
    assert rules.match_type_for("A  B", "A B") == "exact"
    assert rules.match_type_for("A B C", "A B") == "contains"


def test_specificity_exact_then_longest_then_newest(conn):
    t = add_txn(conn, "IC* COSTCO BY INSTACART", -2000)
    costco, other, rent = pid(conn, "Costco"), pid(conn, "Other"), pid(conn, "Rent")
    r_short, _ = rules.create_rule(conn, "COSTCO", "withdrawal", match_type="contains",
                                   purpose_id=other, apply=False)
    r_long, _ = rules.create_rule(conn, "COSTCO BY INSTACART", "withdrawal",
                                  match_type="contains", purpose_id=costco, apply=False)
    assert rules.find_best_rule(conn, "IC* COSTCO BY INSTACART", -2000).id == r_long

    # Same length: newest wins (created_at ties broken by id).
    r_newer, _ = rules.create_rule(conn, "IC* COSTCO BY INSTACAR", "withdrawal",
                                   match_type="contains", purpose_id=rent, apply=False)
    r_same_len, _ = rules.create_rule(conn, "C* COSTCO BY INSTACART", "withdrawal",
                                      match_type="contains", purpose_id=other, apply=False)
    assert rules.find_best_rule(conn, "IC* COSTCO BY INSTACART", -2000).id == r_same_len

    r_exact, _ = rules.create_rule(conn, "IC* COSTCO BY INSTACART", "withdrawal",
                                   purpose_id=costco, apply=False)
    assert rules.find_best_rule(conn, "IC*  COSTCO BY INSTACART", -2000).id == r_exact
    res = rules.apply_rules(conn)
    assert res.updated == [t] and res.purpose_changed == [t]
    assert txn(conn, t)["purpose_id"] == costco
    assert {r_short, r_newer} <= {r.id for r in rules.all_rules(conn)}


def test_newest_wins_with_explicit_created_at(conn):
    a, _ = rules.create_rule(conn, "PG&E", "withdrawal", match_type="contains",
                             purpose_id=pid(conn, "Other"), apply=False)
    b, _ = rules.create_rule(conn, "WEB_", "withdrawal", match_type="contains",
                             purpose_id=pid(conn, "PG&E"), apply=False)
    conn.execute("UPDATE rules SET created_at = '2030-01-01 00:00:00' WHERE id = ?", (a,))
    assert rules.find_best_rule(conn, "PG&E WEB_ONLINE", -1).id == a


# --- applying ----------------------------------------------------------------


def test_manual_tags_untouched(conn):
    manual = add_txn(conn, "SFPUC WEB_PAY", -6000, source="manual", purpose_id=pid(conn, "Other"))
    plain = add_txn(conn, "SFPUC WEB_PAY", -6100)
    rid, res = rules.create_rule(conn, "SFPUC WEB_PAY", "withdrawal", purpose_id=pid(conn, "Water"))
    assert res.updated == [plain]
    assert txn(conn, manual)["purpose_id"] == pid(conn, "Other")
    assert txn(conn, manual)["category_source"] == "manual"
    # Explicit ids, reapply and delete also leave manual rows alone.
    assert rules.apply_rules(conn, [manual]).updated == []
    assert manual not in rules.reapply_rule(conn, rid).updated
    rules.delete_rule(conn, rid, reapply=True)
    assert txn(conn, manual)["category_source"] == "manual"


def test_apply_rules_to_given_ids_only(conn):
    a = add_txn(conn, "RECOLOGY", -100)
    b = add_txn(conn, "RECOLOGY", -100)
    rules.create_rule(conn, "RECOLOGY", "withdrawal", purpose_id=pid(conn, "Recology"), apply=False)
    res = rules.apply_rules(conn, [a])
    assert res.updated == [a]
    assert txn(conn, b)["category_source"] is None
    assert len(rules.apply_rules(conn)) == 1  # now b


def test_refund_deposit_rule_sets_purpose_and_clears_person(conn):
    alice = db.add_person(conn, "Alice")
    t = add_txn(conn, "COSTCO REFUND", 500)
    conn.execute("UPDATE transactions SET person_id = ? WHERE id = ?", (alice, t))
    _, res = rules.create_rule(conn, "COSTCO REFUND", "deposit", purpose_id=pid(conn, "Costco"))
    row = txn(conn, t)
    assert (row["person_id"], row["purpose_id"], row["category_source"]) == (
        None, pid(conn, "Costco"), "rule")
    assert res.purpose_changed == [t]


def test_rule_validation(conn):
    alice = db.add_person(conn, "Alice")
    with pytest.raises(ValueError):
        rules.create_rule(conn, "X", "withdrawal", person_id=alice)
    with pytest.raises(ValueError):
        rules.create_rule(conn, "X", "deposit")
    with pytest.raises(ValueError):
        rules.create_rule(conn, "X", "deposit", person_id=alice, purpose_id=pid(conn, "Rent"))
    with pytest.raises(ValueError):
        rules.create_rule(conn, "  ", "deposit", person_id=alice)
    with pytest.raises(ValueError):
        rules.create_rule(conn, "X", "both", person_id=alice)


def test_duplicate_rule_updates_target(conn):
    r1, _ = rules.create_rule(conn, "costco", "withdrawal", match_type="contains",
                              purpose_id=pid(conn, "Other"))
    r2, _ = rules.create_rule(conn, "COSTCO", "withdrawal", match_type="contains",
                              purpose_id=pid(conn, "Costco"))
    assert r1 == r2
    assert rules.get_rule(conn, r1).purpose_id == pid(conn, "Costco")
    assert len(rules.all_rules(conn)) == 1


# --- edit / delete -----------------------------------------------------------


def test_update_rule_reapply_retags_rule_rows(conn):
    t1 = add_txn(conn, "PGANDE WEB ONLINE 1", -100)
    t2 = add_txn(conn, "PGANDE WEB ONLINE 2", -100)
    rid, _ = rules.create_rule(conn, "PGANDE", "withdrawal", match_type="contains",
                               purpose_id=pid(conn, "Other"))
    # Without reapply nothing changes.
    assert len(rules.update_rule(conn, rid, purpose_id=pid(conn, "PG&E"))) == 0
    assert txn(conn, t1)["purpose_id"] == pid(conn, "Other")
    res = rules.reapply_rule(conn, rid)
    assert sorted(res.purpose_changed) == sorted([t1, t2])
    assert txn(conn, t1)["purpose_id"] == pid(conn, "PG&E")

    # Narrowing the pattern un-tags rows that no longer match any rule.
    res = rules.update_rule(conn, rid, pattern="PGANDE WEB ONLINE 1", match_type="exact",
                            reapply=True)
    assert res.cleared == [t2] and res.updated == [t2]
    assert txn(conn, t2)["category_source"] is None and txn(conn, t2)["purpose_id"] is None
    assert txn(conn, t1)["category_source"] == "rule"


def test_update_rule_reapply_tags_newly_matching_uncategorized(conn):
    t = add_txn(conn, "WATER BILL", -100)
    rid, res = rules.create_rule(conn, "WATER", "withdrawal", match_type="exact",
                                 purpose_id=pid(conn, "Water"))
    assert len(res) == 0
    res = rules.update_rule(conn, rid, match_type="contains", reapply=True)
    assert res.updated == [t]


def test_delete_rule_falls_back_to_next_rule(conn):
    t = add_txn(conn, "IC* COSTCO BY INSTACART", -100)
    rules.create_rule(conn, "INSTACART", "withdrawal", match_type="contains",
                      purpose_id=pid(conn, "Other"))
    r2, _ = rules.create_rule(conn, "COSTCO BY INSTACART", "withdrawal", match_type="contains",
                              purpose_id=pid(conn, "Costco"), apply=False)
    rules.reapply_rule(conn, r2)
    assert txn(conn, t)["purpose_id"] == pid(conn, "Costco")
    res = rules.delete_rule(conn, r2, reapply=True)
    assert res.purpose_changed == [t] and res.cleared == []
    assert txn(conn, t)["purpose_id"] == pid(conn, "Other")
    assert rules.get_rule(conn, r2) is None


def test_delete_rule_without_reapply_keeps_tags(conn):
    t = add_txn(conn, "RECOLOGY", -100)
    rid, _ = rules.create_rule(conn, "RECOLOGY", "withdrawal", purpose_id=pid(conn, "Recology"))
    assert len(rules.delete_rule(conn, rid)) == 0
    assert txn(conn, t)["category_source"] == "rule"


# --- remember not offered ----------------------------------------------------


@pytest.mark.parametrize(
    "desc",
    [
        "VENMO CASHOUT",
        "Venmo  Cashout 1234",
        "eCheck Deposit",
        "ECHECK DEPOSIT 55",
        "Internet transfer from Spending account XXXXXX0000",
        "Internet transfer from Savings account XXXXXX0000",
    ],
)
def test_rule_not_allowed(conn, desc):
    assert not rules.rule_allowed(desc)
    t = add_txn(conn, desc, 100)
    with pytest.raises(ValueError):
        rules.create_rule_from_transaction(conn, t, person_id=db.get_house_id(conn))
    # A broad contains rule still never tags these.
    rules.create_rule(conn, "e", "deposit", match_type="contains", person_id=db.get_house_id(conn))
    assert txn(conn, t)["category_source"] is None


def test_rule_allowed_for_ordinary_descriptions():
    assert rules.rule_allowed("Zelle payment from SOMEONE")
    assert rules.rule_allowed("VENMO PAYMENT")


# --- listing / seed ----------------------------------------------------------


def test_list_rules_with_match_counts(conn):
    add_txn(conn, "RECOLOGY", -100)
    add_txn(conn, "RECOLOGY", -100)
    add_txn(conn, "RECOLOGY", 100)  # other sign: not counted
    rules.create_rule(conn, "RECOLOGY", "withdrawal", purpose_id=pid(conn, "Recology"))
    alice = db.add_person(conn, "Alice")
    rules.create_rule(conn, "NOBODY", "deposit", person_id=alice)
    summaries = rules.list_rules(conn)
    by_pattern = {s.rule.pattern: s for s in summaries}
    assert by_pattern["RECOLOGY"].match_count == 2
    assert by_pattern["RECOLOGY"].target_name == "Recology"
    assert by_pattern["NOBODY"].match_count == 0
    assert by_pattern["NOBODY"].target_name == "Alice"
    assert summaries[0].rule.pattern == "NOBODY"  # newest first


def test_preview_uncategorized_only(conn):
    add_txn(conn, "RECOLOGY", -100)
    add_txn(conn, "RECOLOGY", -100, source="manual", purpose_id=pid(conn, "Recology"))
    assert rules.preview_match_count(conn, "recology", "withdrawal", "contains") == 2
    assert rules.preview_match_count(conn, "RECOLOGY", "withdrawal", uncategorized_only=True) == 1


def test_seeded_interest_rule(tmp_path):
    path = tmp_path / "f.db"
    c = db.connect(path)
    house = db.get_house_id(c)
    seeded = rules.all_rules(c)
    assert [(r.match_type, r.pattern, r.applies_to, r.person_id) for r in seeded] == [
        ("exact", "Interest Paid", "deposit", house)
    ]
    t = add_txn(c, "Interest Paid", 12)
    w = add_txn(c, "Interest Paid", -12)
    res = rules.apply_rules(c)
    assert res.updated == [t] and res.purpose_changed == []
    assert txn(c, t)["person_id"] == house
    assert txn(c, w)["category_source"] is None
    # A user delete sticks across reconnects.
    rules.delete_rule(c, seeded[0].id)
    c.close()
    c = db.connect(path)
    assert rules.all_rules(c) == []
    assert c.execute("SELECT COUNT(*) FROM people WHERE is_house = 1").fetchone()[0] == 1
    c.close()
