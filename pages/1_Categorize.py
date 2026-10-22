"""Categorize: tag deposits with a person (or a refund purpose) and withdrawals
with a purpose and split (or a person they paid); optionally remember the
description as a rule.

Layout: filters, a table of matching transactions (newest first), and an
editor for the selected row (pick it in the table or the Transaction box).
Data logic lives in housefin/categorize.py.
"""

from __future__ import annotations

import streamlit as st

from housefin import categorize, db, rules, splits, ui

st.set_page_config(page_title="Categorize · House Finances", layout="wide")
conn = ui.get_conn()
ss = st.session_state

NEW = -1  # selectbox option: add a new person/purpose inline

months = ui.month_selector(conn)

st.title("Categorize")
if flash := ss.pop("_cat_flash", None):
    st.success(flash)

# --- filters -----------------------------------------------------------------

f1, f2, f3 = st.columns([1, 1.3, 2])
status = f1.radio("Show", ["Uncategorized", "All"], horizontal=True, key="cat_status")
kind = f2.radio("Type", ["All", "Deposits", "Withdrawals"], horizontal=True, key="cat_kind")
search = f3.text_input(
    "Search", key="cat_search", placeholder="Description, person or purpose"
)

rows = categorize.list_transactions(
    conn,
    status=categorize.UNCATEGORIZED if status == "Uncategorized" else categorize.ALL,
    kind={"All": categorize.ALL, "Deposits": categorize.DEPOSITS}.get(kind, categorize.WITHDRAWALS),
    months=months,
    search=search,
)
st.caption(f"{len(rows)} transaction(s) · {ui.describe_months(months)}")
if not rows:
    st.info(
        "Nothing left to categorize here." if status == "Uncategorized" else "No transactions match."
    )
    st.stop()

ids = [r["id"] for r in rows]
by_id = {r["id"]: r for r in rows}


def tag_label(r) -> str:
    if r["person_name"]:
        return r["person_name"] + (" (paid to)" if r["amount_cents"] < 0 else "")
    if r["purpose_name"]:
        return r["purpose_name"] + (" (refund)" if r["amount_cents"] > 0 else "")
    return ""


def row_label(tid: int) -> str:
    r = by_id[tid]
    desc = r["description"] if len(r["description"]) <= 60 else r["description"][:57] + "..."
    return f"{r['date']} · {ui.format_cents(r['amount_cents'])} · {desc}"


# --- table -------------------------------------------------------------------

ss["_cat_ids"] = ids
table_key = f"cat_table_{ss.get('_cat_table_ver', 0)}"


def _on_table_select() -> None:
    picked = ss[table_key]["selection"]["rows"]
    prev_ids = ss.get("_cat_ids", [])
    if picked and picked[0] < len(prev_ids):
        ss["_cat_next"] = prev_ids[picked[0]]


st.dataframe(
    [
        {
            "Date": r["date"],
            "Amount": ui.cents_to_dollars(r["amount_cents"]),
            "Description": r["description"],
            "Person / purpose": tag_label(r),
            "Source": r["category_source"] or "",
            "Applies to": r["applies_month"] or "",
        }
        for r in rows
    ],
    column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
    hide_index=True,
    height=min(38 + 35 * len(rows), 360),
    width="stretch",
    key=table_key,
    on_select=_on_table_select,
    selection_mode="single-row",
)

if (nxt := ss.pop("_cat_next", None)) in by_id:
    ss["cat_selected"] = nxt
if ss.get("cat_selected") not in by_id:
    ss["cat_selected"] = ids[0]
tid = st.selectbox("Transaction", ids, format_func=row_label, key="cat_selected")

# --- editor ------------------------------------------------------------------

txn = categorize.get_transaction(conn, tid)
amount = txn["amount_cents"]
is_deposit = amount > 0
month = splits.transaction_month(txn)
k = f"cat_{tid}"


def shift_month(m: str, delta: int) -> str:
    y, mo = map(int, m.split("-"))
    idx = y * 12 + (mo - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def pick_name(rows_, current_id, placeholder, new_label, *, key, extra_label=None):
    """Selectbox of ids plus NEW; returns the chosen id (or None/NEW)."""
    names = {r["id"]: r["name"] for r in rows_}
    options = [r["id"] for r in rows_] + [NEW]
    return st.selectbox(
        placeholder,
        options,
        index=options.index(current_id) if current_id in options else None,
        format_func=lambda i: new_label if i == NEW else (extra_label or (lambda _i, n: n))(i, names[i]),
        placeholder=f"Choose a {placeholder.lower()}",
        key=key,
    )


with st.container(border=True):
    st.subheader(f"{txn['date']} · {ui.format_cents(amount)}")
    st.code(txn["description"], language=None)
    source = {"rule": "tagged by a rule", "manual": "tagged by hand"}.get(
        txn["category_source"], "uncategorized"
    )
    st.caption(f"{txn['type']} · {source}")

    # Applies-to-month override (spec open question 5).
    date_month = txn["date"][:7]
    month_opts = ["", shift_month(date_month, -1), shift_month(date_month, 1)]
    if txn["applies_month"] and txn["applies_month"] not in month_opts:
        month_opts.append(txn["applies_month"])
    applies = st.selectbox(
        "Applies to month",
        month_opts,
        index=month_opts.index(txn["applies_month"] or ""),
        format_func=lambda m: f"Transaction date ({ui.format_month(date_month)})"
        if not m else ui.format_month(m),
        key=f"{k}_applies",
        help="Counts the transaction toward another month, e.g. next month's rent paid early.",
    )
    month = applies or date_month

    refund = False
    if is_deposit:
        refund = st.toggle(
            "Refund (reduces the shares of the people in its split)",
            value=txn["purpose_id"] is not None,
            key=f"{k}_refund",
        )
        to_person = not refund
    else:
        to_person = st.toggle(
            "Paid to a person (reduces what they have paid in)",
            value=txn["person_id"] is not None,
            key=f"{k}_paid_to",
        )

    person_id = purpose_id = None
    new_name = ""
    weights: list[tuple[int, float]] = []
    split_error = None
    edited = remember_split = False
    purpose_row = None

    if to_person:
        # Active people first; others (moved out / not yet in) after, marked,
        # so a moved-out person can still be credited for paying back.
        everyone = db.list_people(conn)
        active = [p for p in everyone if p["is_house"] or db.is_active(p, month)]
        active_ids = {p["id"] for p in active}
        inactive = [p for p in everyone if p["id"] not in active_ids]
        inactive_ids = {p["id"] for p in inactive}
        person_id = pick_name(
            active + inactive,
            txn["person_id"],
            "Person",
            "+ Add a new person...",
            key=f"{k}_person",
            extra_label=lambda i, n: f"{n} (not active {ui.format_month(month)})"
            if i in inactive_ids else n,
        )
        if person_id == NEW:
            new_name = st.text_input("New person's name", key=f"{k}_new_person")
            st.caption("They are always active; set a move-in month on the People screen.")
    else:
        purpose_rows = db.list_purposes(conn)
        purpose_id = pick_name(
            purpose_rows, txn["purpose_id"], "Purpose", "+ Add a new purpose...",
            key=f"{k}_purpose",
        )
        if purpose_id == NEW:
            new_name = st.text_input("New purpose name", key=f"{k}_new_purpose")
        purpose_row = next((p for p in purpose_rows if p["id"] == purpose_id), None)

        if purpose_id is not None and not (purpose_row and purpose_row["excluded"]):
            purpose_label = purpose_row["name"] if purpose_row else (new_name.strip() or "new purpose")
            pkey = f"{'new' if purpose_id == NEW else purpose_id}_{month}"
            fake = {**dict(txn), "applies_month": applies or None}
            init = categorize.initial_weights(conn, fake, None if purpose_id == NEW else purpose_id)
            init_map = splits.to_percents(init)
            st.markdown("**Split** (percent per person; set 0 to leave someone out)")
            cand = categorize.split_candidates(conn, month, init)
            for p in cand:
                percent = st.number_input(
                    f"{p['name']} (%)", min_value=0.0, max_value=100.0, step=1.0, format="%g",
                    value=float(init_map.get(p["id"], 0.0)),
                    key=f"{k}_w_{pkey}_{p['id']}",
                )
                weights.append((p["id"], percent))
            weights = [(pid, w) for pid, w in weights if w > 0]
            if weights:
                split_error = splits.percent_total_error(w for _, w in weights)
                if split_error:
                    st.warning(split_error)
                else:
                    shares = dict(splits.compute_shares(-amount, weights))
                    st.dataframe(
                        [
                            {
                                "Person": next(p["name"] for p in cand if p["id"] == pid),
                                "%": f"{w:g}%",
                                "Share": ui.format_cents(shares[pid]),
                            }
                            for pid, w in weights
                        ],
                        hide_index=True,
                    )
            else:
                st.warning("Nobody is in the split; save would leave it unsplit.")
            edited = dict(weights) != init_map
            # Mirror categorize._save_split: unedited keeps the stored split's
            # source (or is the purpose default); edited is 'manual' unless it
            # matches the default again.
            origin = None if purpose_id == NEW else categorize.split_origin(conn, fake, purpose_id)
            if not edited:
                saved_as = origin or splits.DEFAULT
            else:
                default_w = init if origin is None else splits.default_split_for(conn, purpose_id, month)
                saved_as = splits.DEFAULT if weights and splits.same_split(default_w, weights) else splits.MANUAL
            if weights:
                if saved_as == splits.MANUAL:
                    st.caption("Split set by hand." if not edited else "Edited: saves as a split set by hand.")
                elif origin == splits.DEFAULT and not edited:
                    st.caption(f"Default split for {purpose_label}, as it was when saved.")
                else:
                    st.caption(f"Default split for {purpose_label}.")
            if edited:
                noun = "refunds" if is_deposit else "withdrawals"
                remember_split = st.checkbox(
                    f"Use this split for future {purpose_label} {noun}",
                    value=True, key=f"{k}_remember_split",
                    help="Updates the purpose's default split; past transactions keep theirs.",
                )
        elif purpose_row is not None and purpose_row["excluded"]:
            st.caption(f"{purpose_row['name']} has no split and is left out of reports and balances.")

    # Remember for this description.
    pattern = txn["description"]
    remember = st.checkbox("Remember for this description", value=True, key=f"{k}_remember")
    if remember:
        pattern = st.text_input(
            "Pattern", value=txn["description"], key=f"{k}_pattern",
            help="The full description makes an exact rule; shorten it to match any "
            "description containing it (case-insensitive).",
        )
        preview = categorize.preview_rule(conn, tid, pattern)
        noun = "deposits" if is_deposit else "withdrawals"
        kind_text = "Exact match" if preview.match_type == rules.EXACT else "Contains (case-insensitive)"
        st.caption(
            f"{kind_text} · matches {preview.total} existing {noun}; "
            f"{preview.will_tag} other uncategorized will be tagged on save."
        )

    b1, b2, _ = st.columns([1, 1, 4])
    save = b1.button("Save", type="primary", key=f"{k}_save")
    clear = txn["category_source"] is not None and b2.button("Clear tags", key=f"{k}_clear")


def _after_change(message: str) -> None:
    """Flash a message, move selection on if the row left the list, rerun."""
    pos = ids.index(tid)
    ss["_cat_next"] = ids[pos + 1] if pos + 1 < len(ids) else (ids[pos - 1] if pos else None)
    if status != "Uncategorized":
        ss["_cat_next"] = tid
    ss["_cat_flash"] = message
    ss["_cat_table_ver"] = ss.get("_cat_table_ver", 0) + 1
    st.rerun()


if clear:
    categorize.clear_tags(conn, tid)
    _after_change("Tags cleared; the transaction is uncategorized again.")

if save:
    try:
        if (person_id if to_person else purpose_id) is None:
            raise ValueError("Pick a person." if to_person else "Pick a purpose.")
        if split_error:
            raise ValueError(split_error)
        if remember and not pattern.strip():
            raise ValueError("Enter a pattern, or untick Remember.")
        with db.transaction(conn):  # an inline-added value is kept only if the save works
            if person_id == NEW or purpose_id == NEW:
                name = new_name.strip()
                if not name:
                    raise ValueError("Enter a name for the new value.")
                if person_id == NEW:
                    existing = db.get_person_by_name(conn, name)
                    person_id = existing["id"] if existing else db.add_person(conn, name)
                else:
                    existing = db.get_purpose(conn, name)
                    purpose_id = existing["id"] if existing else db.add_purpose(conn, name)
            kwargs = dict(remember=remember, pattern=pattern, applies_month=applies or None)
            if to_person:
                result = categorize.save_person(conn, tid, person_id, **kwargs)
            else:
                save_fn = categorize.save_refund if is_deposit else categorize.save_withdrawal
                result = save_fn(
                    conn, tid, purpose_id, weights,
                    remember_split=edited and remember_split, **kwargs,
                )
    except ValueError as exc:
        st.error(str(exc))
    else:
        msg = "Saved."
        if result.rule_id is not None:
            msg += f" Rule remembered; {result.rule_tagged} other transaction(s) updated."
        _after_change(msg)
