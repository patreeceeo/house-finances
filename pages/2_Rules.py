"""Rules: list remembered rules; edit or delete them, optionally re-applying."""

import streamlit as st

from housefin import db, rules, splits, ui

st.set_page_config(page_title="Rules · House Finances", layout="wide")
conn = ui.get_conn()
ss = st.session_state

st.title("Rules")
st.caption(
    "Rules fill in the person (deposits) or purpose (withdrawals and refunds) of "
    "matching transactions. Exact rules match the whole description; contains rules "
    "match a case-insensitive piece of it. Exact beats contains, then the longest "
    "pattern, then the newest rule. Rows you tagged by hand are never changed."
)

flash = ss.pop("_rules_flash", None)
if flash:
    st.success(flash)

LEAVE = "Leave past transactions as they are"
REAPPLY = "Re-apply to past rule-tagged transactions"
REAPPLY_HELP = (
    "Re-apply re-checks every rule-tagged or uncategorized transaction this rule "
    "matched (before or after the change) against all rules; ones no rule matches "
    "any more become uncategorized. Hand-tagged transactions are never touched."
)


def done(msg: str, result: rules.ApplyResult | None, rule_id: int) -> None:
    """Flash and rerun. Call after the transaction (st.rerun raises)."""
    if result is not None:
        n, c = len(result.updated), len(result.cleared)
        msg += f" {n} transaction{'s' if n != 1 else ''} re-tagged"
        msg += f" ({c} now uncategorized)." if c else "."
    for key in list(ss.keys()):
        if isinstance(key, str) and key.startswith("rule_") and key.endswith(f"_{rule_id}"):
            del ss[key]
    ss["_rules_flash"] = msg
    st.rerun()


summaries = rules.list_rules(conn)
if not summaries:
    st.info("No rules yet. Use Remember on the Categorize screen to create one.")
    st.stop()

people_by_id = {p["id"]: p for p in db.list_people(conn)}
purposes_by_id = {p["id"]: p for p in db.list_purposes(conn)}


def target_label(person_id, purpose_id) -> str:
    if person_id is not None:
        p = people_by_id.get(person_id)
        return f"Person: {p['name'] if p else '?'}"
    p = purposes_by_id.get(purpose_id)
    return f"Purpose: {p['name'] if p else '?'}"


def rule_label(rule: rules.Rule) -> str:
    return f"{rule.pattern}  ({rule.match_type}, {rule.applies_to})"


# --- list --------------------------------------------------------------------

st.dataframe(
    [
        {
            "Match type": s.rule.match_type,
            "Pattern": s.rule.pattern,
            "Applies to": s.rule.applies_to,
            "Target": target_label(s.rule.person_id, s.rule.purpose_id),
            "Matches": s.match_count,
        }
        for s in summaries
    ],
    hide_index=True,
    width="stretch",
)

# --- edit ----------------------------------------------------------------------

by_id = {s.rule.id: s.rule for s in summaries}
if ss.get("rules_selected") not in by_id:
    ss.pop("rules_selected", None)
rid = st.selectbox(
    "Rule", list(by_id), format_func=lambda i: rule_label(by_id[i]), key="rules_selected"
)
rule = by_id[rid]

st.subheader("Edit rule")
targets: list[tuple[str, int]] = []
if rule.applies_to == rules.DEPOSIT:
    targets += [("person", pid) for pid in people_by_id]
targets += [("purpose", pid) for pid in purposes_by_id]
current = ("person", rule.person_id) if rule.person_id is not None else ("purpose", rule.purpose_id)
if current not in targets:  # dangling reference; keep it selectable
    targets.insert(0, current)


def fmt_target(t: tuple[str, int]) -> str:
    kind, tid = t
    if kind == "person":
        return target_label(tid, None)
    label = target_label(None, tid)
    return label + (" (refund)" if rule.applies_to == rules.DEPOSIT else "")


c1, c2 = st.columns([3, 1])
pattern = c1.text_input("Pattern", value=rule.pattern, key=f"rule_pattern_{rid}")
match_type = c2.radio(
    "Match type",
    [rules.EXACT, rules.CONTAINS],
    index=0 if rule.match_type == rules.EXACT else 1,
    horizontal=True,
    key=f"rule_type_{rid}",
)
target = st.selectbox(
    "Sets",
    targets,
    index=targets.index(current),
    format_func=fmt_target,
    key=f"rule_target_{rid}",
    help="Deposit rules set a person, or a purpose for refunds. "
    "Withdrawal rules set a purpose.",
)
if pattern.strip():
    n = rules.preview_match_count(conn, pattern, rule.applies_to, match_type)
    st.caption(f"Would match {n} {rule.applies_to}{'s' if n != 1 else ''}.")
edit_mode = st.radio(
    "When saving", [LEAVE, REAPPLY], key=f"rule_edit_mode_{rid}", help=REAPPLY_HELP
)
if st.button("Save rule", type="primary", key=f"rule_save_{rid}"):
    kind, tid = target
    try:
        with db.transaction(conn):  # rule change, re-tags and splits together
            result = rules.update_rule(
                conn,
                rid,
                pattern=pattern,
                match_type=match_type,
                person_id=tid if kind == "person" else None,
                purpose_id=tid if kind == "purpose" else None,
                reapply=edit_mode == REAPPLY,
            )
            splits.sync_after_rules(conn, result)
    except ValueError as exc:
        st.error(str(exc))
    else:
        done("Rule saved.", result if edit_mode == REAPPLY else None, rid)

# --- delete --------------------------------------------------------------------

with st.expander("Delete rule"):
    del_mode = st.radio(
        "When deleting", [LEAVE, REAPPLY], key=f"rule_del_mode_{rid}", help=REAPPLY_HELP
    )
    confirm = st.checkbox(f"Yes, delete “{rule.pattern}”", key=f"rule_del_confirm_{rid}")
    if st.button("Delete rule", disabled=not confirm, key=f"rule_delete_{rid}"):
        with db.transaction(conn):
            result = rules.delete_rule(conn, rid, reapply=del_mode == REAPPLY)
            splits.sync_after_rules(conn, result)
        ss.pop("rules_selected", None)
        done("Rule deleted.", result if del_mode == REAPPLY else None, rid)
