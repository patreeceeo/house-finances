"""The "Match deposits to shares" view on the Balances page (pages/6_Balances.py).

A custom component (``st.components.v2``, inline HTML/CSS/JS) draws a
person's groups from :func:`housefin.matching.groups_for`: shares on the left,
deposits on the right, each group's Deposits / Shares / Difference, and the
differences summed against the Running balance. It does no grouping or
arithmetic of its own; everything it shows is serialized here
(:func:`component_data`), with dates, months and money formatted in Python so
they follow the browser's locale like the rest of the app.

Each edit in the component (Keep, Move, Split by month, Merge with next, a
note, Back to suggestion, Keep all balanced suggestions) comes back as one
``edit`` trigger event::

    {"id": "<unique per event>", "person": person_id, "action": "move",
     "items": [[transaction_id, side], ...], "target": group_key | "new"}

The trigger's callback runs once, before the next script run, with the
person and months the page was drawn with, and hands the event to
:func:`apply_edit`, which calls the housefin.matching operation. An event
naming a group or item that has since changed is refused (StaleEdit) and
nothing is saved.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, MutableMapping, Sequence

from housefin import matching, ui
from housefin.matching import Group, Item

STALE = "The groups changed since the page was drawn; nothing was saved."

ACTIONS = ("keep", "keep_all_balanced", "move", "split", "merge", "note", "unkeep")


# --- serialization (no Streamlit) ----------------------------------------------------


def tone(cents: int) -> str:
    """'owed' (negative), 'credit' (positive) or 'balanced' (exactly 0)."""
    return "owed" if cents < 0 else "credit" if cents > 0 else "balanced"


def signed_cents(cents: int) -> str:
    """'+$1.00', '-$1.00', '$0.00'."""
    return ("+" if cents > 0 else "") + ui.format_cents(cents)


def group_title(group: Group, locale: str | None = None) -> str:
    """'Oct 2026', 'Sep 2026 – Oct 2026' (first and last share month), or
    'Paid ahead' for a group with no shares."""
    m = group.months
    if not m:
        return "Paid ahead"
    if len(m) == 1:
        return ui.format_month(m[0], locale)
    return f"{ui.format_month(m[0], locale)} – {ui.format_month(m[-1], locale)}"


def item_key(item: Item) -> str:
    """The item's key as the component writes it: '<transaction_id>-<side>'."""
    return f"{item.transaction_id}-{item.side}"


def item_data(item: Item, locale: str | None = None) -> dict:
    """One item row. A share shows its purpose ('refund' for a negative
    share) over the description, and the bill it is a share of."""
    if item.side == matching.SHARE:
        what = (item.purpose or item.description) + (" refund" if item.cents < 0 else "")
        detail = item.description if item.purpose else ""
        of = f"of {ui.format_cents(-item.amount_cents)}"
    else:
        what, detail, of = item.description, "", ""
    return {
        "key": item_key(item),
        "tid": item.transaction_id,
        "side": item.side,
        "date": ui.format_date(item.date, locale),
        "what": what,
        "detail": detail,
        "amount": ui.format_cents(item.cents),
        "of": of,
        "cents": item.cents,
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def group_data(groups: Sequence[Group], locale: str | None = None) -> list[dict]:
    """The groups as the component draws them, in order. ``next`` is the key
    of the group below (for Merge with next), None for the last."""
    out = []
    for n, g in enumerate(groups):
        shares = [i for i in g.items if i.side == matching.SHARE]
        paid = [i for i in g.items if i.side == matching.PAID]
        title = group_title(g, locale)
        diff = g.difference_cents
        changed = None
        if g.changed_since_kept:
            changed = (
                f"Changed since kept: deposits were {ui.format_cents(g.kept_paid_cents)}, "
                f"shares were {ui.format_cents(g.kept_share_cents)}"
            )
        later = None
        if g.later_items:
            later = (
                f"Also has {_plural(len(g.later_items), 'item')} after the latest selected "
                "month, not shown or counted here."
            )
        counts = f"{_plural(len(shares), 'share')}, {_plural(len(paid), 'deposit')}"
        out.append({
            "key": g.key,
            "kept": g.kept,
            "note": g.note or "",
            "title": title,
            "multi_month": len(g.months) > 1,
            "paid_cents": g.paid_cents,
            "share_cents": g.share_cents,
            "diff_cents": diff,
            "paid": ui.format_cents(g.paid_cents),
            "shares_total": ui.format_cents(g.share_cents),
            "shares_neg": ui.format_cents(-g.share_cents),
            "diff": signed_cents(diff),
            "tone": tone(diff),
            "counts": counts,
            "summary": f"{counts} · {ui.format_cents(g.share_cents)} · balanced",
            "changed": changed,
            "later": later,
            "option": f"{title}{'' if g.kept else ' (suggested)'} · {signed_cents(diff)}",
            "next": groups[n + 1].key if n + 1 < len(groups) else None,
            "share_items": [item_data(i, locale) for i in shares],
            "paid_items": [item_data(i, locale) for i in paid],
        })
    return out


def equation_data(
    groups: Sequence[Group], running_cents: int, locale: str | None = None
) -> dict:
    """The sum line: every non-zero group's difference, how many balanced
    groups there are, the total, and whether it equals the Running balance."""
    total = sum(g.difference_cents for g in groups)
    return {
        "terms": [
            {"label": group_title(g, locale), "text": ui.format_cents(g.difference_cents),
             "tone": tone(g.difference_cents)}
            for g in groups if g.difference_cents
        ],
        "balanced": sum(1 for g in groups if not g.difference_cents),
        "total": ui.format_cents(total),
        "total_tone": tone(total),
        "running": ui.format_cents(running_cents),
        "ok": total == running_cents,
    }


def component_data(
    person_id: int,
    name: str,
    groups: Sequence[Group],
    running_cents: int,
    locale: str | None = None,
    last_event: str | None = None,
) -> dict:
    """Everything the component shows, JSON-serializable. ``last_event`` is
    the id of the last edit handled, so the data changes (and the component
    redraws and clears its busy state) even when an edit changed nothing."""
    return {
        "person": person_id,
        "last_event": last_event,
        "name": name,
        "running": ui.format_cents(running_cents),
        "running_tone": tone(running_cents),
        "equation": equation_data(groups, running_cents, locale),
        "groups": group_data(groups, locale),
        "can_keep_balanced": any(not g.kept and g.difference_cents == 0 for g in groups),
    }


# --- events (no Streamlit) ---------------------------------------------------------------


def apply_edit(
    conn: sqlite3.Connection,
    person_id: int,
    months: Sequence[str] | None,
    event: Mapping,
) -> str | None:
    """Apply one edit event from the component for the person and months the
    page was drawn with. Returns None when it was saved, else the message to
    show (nothing was saved): :data:`STALE` when the groups or person have
    changed since, or the refusal from housefin.matching."""
    if event.get("person") != person_id:
        return STALE
    action = event.get("action")
    group = event.get("group")
    try:
        if action == "keep":
            matching.keep(conn, person_id, months, group)
        elif action == "keep_all_balanced":
            matching.keep_all_balanced(conn, person_id, months)
        elif action == "move":
            matching.move(conn, person_id, months, event.get("items") or [], event.get("target"))
        elif action == "split":
            matching.split_by_month(conn, person_id, months, group)
        elif action == "merge":
            matching.merge(conn, person_id, months, group, event.get("other"))
        elif action == "note":
            matching.set_note(conn, person_id, months, group, event.get("note"))
        elif action == "unkeep":
            matching.unkeep(conn, person_id, months, group)
        else:
            return f"Unknown edit {action!r}; nothing was saved."
    except matching.StaleEdit:
        return STALE
    except (ValueError, TypeError) as exc:
        return f"{exc} Nothing was saved."
    return None


def take_event(seen: MutableMapping, key: str, event) -> bool:
    """True the first time an event (by its ``id``) is seen for component
    ``key``; records it in ``seen`` (session state). Guards against an event
    being applied twice."""
    if not isinstance(event, Mapping) or not event.get("id"):
        return False
    mark = f"_{key}_last_event"
    if seen.get(mark) == event["id"]:
        return False
    seen[mark] = event["id"]
    return True


# --- the component ---------------------------------------------------------------------

_HTML = """
<div class="mt">
  <div class="mt-top">
    <span class="label">Running balance</span>
    <span class="balance-fig"></span>
  </div>
  <div class="equation" aria-live="polite"></div>
  <div class="toolbar">
    <label><input type="checkbox" class="show-balanced"> Show balanced groups in full</label>
    <span class="spacer"></span>
    <button type="button" class="keep-balanced">Keep all balanced suggestions</button>
  </div>
  <div class="cols-head" aria-hidden="true">
    <span class="label head-shares"></span>
    <span class="label head-paid"></span>
    <span class="label">Deposits − shares</span>
  </div>
  <div class="groups"></div>
  <div class="selbar" hidden>
    <span><b class="sel-count"></b> selected</span>
    <label class="label sel-label">Move to
      <select class="move-to"></select>
    </label>
    <button type="button" class="primary move-btn">Move</button>
    <button type="button" class="new-btn">Make a new group</button>
    <button type="button" class="link clear-btn">Clear</button>
  </div>
</div>
"""

_CSS = """
.mt {
  --fg: var(--st-text-color, #1d2420);
  --surface: var(--st-background-color, #ffffff);
  --band: var(--st-secondary-background-color, #f3f5f4);
  --line: var(--st-border-color, rgba(128, 128, 128, .3));
  --muted: color-mix(in srgb, var(--fg) 62%, transparent);
  --accent: var(--st-green-text-color, #2c6a4d);
  --accent-line: var(--st-green-color, #2c6a4d);
  --accent-soft: var(--st-green-background-color, #e3efe8);
  --owed: var(--st-red-text-color, #a3392a);
  --owed-line: var(--st-red-color, #a3392a);
  --credit: var(--st-blue-text-color, #2b5d8c);
  --credit-line: var(--st-blue-color, #2b5d8c);
  --warn: var(--st-orange-text-color, #8a5a00);
  --warn-soft: var(--st-orange-background-color, #fbf0d9);
  --drop: var(--st-green-background-color, #cfe6da);
  --font-body: var(--st-font, system-ui, sans-serif);
  --font-data: var(--st-code-font, ui-monospace, monospace);
  container-type: inline-size;
  display: flex; flex-direction: column;
  border: 1px solid var(--line); border-radius: 8px; background: var(--surface);
  color: var(--fg); font: 1rem/1.5 var(--font-body);
}
.mt.busy { opacity: .6; pointer-events: none; }
.label { font: 600 .7rem/1.2 var(--font-body); letter-spacing: .07em; text-transform: uppercase; color: var(--muted); }
select, input[type="text"], button { font: inherit; font-size: .9rem; color: var(--fg); }
select, input[type="text"] { background: var(--surface); border: 1px solid var(--line); border-radius: 5px; padding: .3rem .5rem; }
button { background: var(--surface); border: 1px solid var(--line); border-radius: 5px; padding: .3rem .65rem; cursor: pointer; white-space: nowrap; }
button:hover { border-color: var(--muted); }
button:disabled { opacity: .5; cursor: default; }
button.primary { background: var(--accent-line); border-color: var(--accent-line); color: var(--surface); font-weight: 600; }
button.link { border: 0; background: none; padding: .15rem .3rem; color: var(--accent); font-weight: 600; }
button.link:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent-line); outline-offset: 2px; }
.neg { color: var(--owed); }
.pos { color: var(--credit); }
.zero { color: var(--accent); }
.muted { color: var(--muted); }
.mt-top { display: flex; flex-wrap: wrap; align-items: baseline; gap: .25rem 1rem; padding: .75rem 1.25rem; border-bottom: 1px solid var(--line); }
.balance-fig { font: 600 1.4rem/1.1 var(--font-data); font-variant-numeric: tabular-nums; }
.equation { padding: .6rem 1.25rem; border-bottom: 1px solid var(--line); background: var(--band); font-size: .9rem; display: flex; flex-wrap: wrap; align-items: baseline; gap: .35rem .5rem; }
.equation .term { font-family: var(--font-data); font-size: .85rem; white-space: nowrap; font-variant-numeric: tabular-nums; }
.equation .op { color: var(--muted); }
.equation .ok { color: var(--accent); font-weight: 600; }
.toolbar { display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: center; padding: .55rem 1.25rem; border-bottom: 1px solid var(--line); font-size: .875rem; }
.toolbar label { display: inline-flex; gap: .4rem; align-items: center; cursor: pointer; }
.toolbar .spacer { flex: 1; }
.cols-head { display: grid; grid-template-columns: 1fr 1fr 9.5rem; gap: 0 1.25rem; padding: .5rem 1.25rem .4rem 1.75rem; border-bottom: 1px solid var(--line); }
.cols-head .label:last-child { text-align: right; }
.group { position: relative; border-bottom: 1px solid var(--line); padding: .85rem 1.25rem .85rem 1.75rem; display: flex; flex-direction: column; gap: .6rem; transition: background-color .12s; }
.group:last-child { border-bottom: 0; }
.group::before { content: ""; position: absolute; left: .7rem; top: .9rem; bottom: .9rem; width: .3rem; border: 2px solid var(--line); border-right: 0; border-radius: 3px 0 0 3px; }
.group.owed::before { border-color: var(--owed-line); }
.group.credit::before { border-color: var(--credit-line); }
.group.balanced::before { border-color: var(--accent-line); }
.group.dragover { background: var(--drop); }
.g-head { display: flex; flex-wrap: wrap; align-items: center; gap: .35rem .75rem; }
.g-title { font-weight: 600; font-size: 1.02rem; }
.pill { font: 600 .68rem/1 var(--font-body); letter-spacing: .07em; text-transform: uppercase; padding: .3rem .45rem; border-radius: 4px; border: 1px solid var(--line); color: var(--muted); }
.pill.kept { background: var(--accent-soft); border-color: transparent; color: var(--accent); }
.g-actions { display: flex; flex-wrap: wrap; gap: .1rem .25rem; margin-left: auto; }
.g-body { display: grid; grid-template-columns: 1fr 1fr 9.5rem; gap: .25rem 1.25rem; align-items: start; }
.side { display: flex; flex-direction: column; min-width: 0; }
/* The shorter column and the sums stay in view while the taller column scrolls past
   (top clears Streamlit's fixed header). */
.g-body > .side, .g-body > .sums { position: sticky; top: 4rem; }
.side-label { display: none; }
.item { display: grid; grid-template-columns: auto 5.6rem 1fr auto; gap: .5rem; align-items: baseline; padding: .28rem .35rem; border-radius: 4px; font-size: .875rem; cursor: grab; min-width: 0; }
.item:hover { background: var(--band); }
.item.sel { background: var(--accent-soft); }
.item input { margin: .15rem 0 0; align-self: start; }
.item .d { font-family: var(--font-data); font-size: .78rem; color: var(--muted); white-space: nowrap; }
.item .what { min-width: 0; overflow-wrap: anywhere; }
.item .what small { color: var(--muted); display: block; font-size: .78rem; line-height: 1.3; }
.item .amt { font-family: var(--font-data); font-size: .82rem; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.item .amt small { display: block; color: var(--muted); font-size: .72rem; }
.empty-side { font-size: .85rem; color: var(--muted); font-style: italic; padding: .28rem .35rem; }
.sums { display: grid; grid-template-columns: auto auto; gap: .1rem .6rem; justify-content: end; font-size: .82rem; font-variant-numeric: tabular-nums; }
.sums span:nth-child(odd) { color: var(--muted); text-align: right; }
.sums span:nth-child(even) { font-family: var(--font-data); text-align: right; }
.sums .diff { font-weight: 600; font-size: .95rem; padding-top: .2rem; border-top: 1px solid var(--line); }
.side-total { display: flex; justify-content: space-between; font-size: .8rem; color: var(--muted); padding: .2rem .35rem 0; border-top: 1px dashed var(--line); margin-top: .2rem; font-variant-numeric: tabular-nums; }
.side-total b { font-family: var(--font-data); font-weight: 600; color: var(--fg); }
.flag { font-size: .85rem; color: var(--warn); background: var(--warn-soft); border-radius: 4px; padding: .3rem .5rem; }
.later { font-size: .82rem; color: var(--muted); }
.note-row { display: flex; gap: .5rem; align-items: center; font-size: .85rem; }
.note-row input { flex: 1; min-width: 0; font-size: .85rem; }
.collapsed { flex-direction: row; flex-wrap: wrap; align-items: center; gap: .35rem .75rem; padding-block: .55rem; }
.collapsed .g-title { font-size: .95rem; }
.collapsed .summary { color: var(--muted); font-size: .85rem; }
.empty { padding: .85rem 1.25rem; color: var(--muted); font-size: .9rem; }
.selbar { position: sticky; bottom: 0; z-index: 2; display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .75rem; padding: .65rem 1.25rem; background: var(--surface); border-top: 2px solid var(--accent-line); border-radius: 0 0 8px 8px; font-size: .9rem; box-shadow: 0 -4px 14px rgb(0 0 0 / .08); }
.selbar[hidden], .selbar [hidden] { display: none; }
.selbar b { font-weight: 600; }
.sel-label { display: inline-flex; align-items: center; gap: .4rem; margin: 0; flex: 0 1 22rem; min-width: 0; }
.sel-label select { text-transform: none; letter-spacing: 0; font-weight: 400; flex: 1; min-width: 0; width: 100%; }
@container (max-width: 46rem) {
  .cols-head { display: none; }
  .g-body { grid-template-columns: 1fr; gap: .6rem; }
  .g-body > .side, .g-body > .sums { position: static; }
  .side-label { display: block; margin: 0 0 .15rem .35rem; }
  .sums { justify-content: start; }
  .sums span:nth-child(odd) { text-align: left; }
}
@container (max-width: 30rem) {
  .group { padding-inline: 1.5rem .75rem; }
  .item { grid-template-columns: auto 1fr auto; gap: .35rem; }
  .item .d { grid-column: 2 / 4; grid-row: 1; }
  .item .what { grid-column: 2; }
  .item .amt { grid-column: 3; }
}
@media (prefers-reduced-motion: reduce) { .group { transition: none; } }
"""

_JS = """
const UI = new WeakMap();  // per mounted instance: what is selected, unfolded, dragged

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const textTone = (t) => (t === "owed" ? "neg" : t === "credit" ? "pos" : "zero");

function itemRow(it, ui) {
  const sel = ui.selected.has(it.key);
  const what = esc(it.what) + (it.detail ? `<small>${esc(it.detail)}</small>` : "");
  const of = it.of ? `<small>${esc(it.of)}</small>` : "";
  return `<div class="item${sel ? " sel" : ""}" draggable="true" data-item="${esc(it.key)}">
    <input type="checkbox" ${sel ? "checked" : ""} aria-label="Select ${esc(it.what)} ${esc(it.date)}">
    <span class="d">${esc(it.date)}</span>
    <span class="what">${what}</span>
    <span class="amt">${esc(it.amount)}${of}</span>
  </div>`;
}

function sideHtml(items, total, empty, title, ui) {
  const rows = items.length ? items.map((it) => itemRow(it, ui)).join("")
    : `<div class="empty-side">${empty}</div>`;
  const tot = items.length > 1
    ? `<div class="side-total"><span>${items.length} items</span><b>${esc(total)}</b></div>` : "";
  return `<div class="side"><span class="label side-label">${esc(title)}</span>${rows}${tot}</div>`;
}

function groupHtml(g, ui) {
  const pill = g.kept ? `<span class="pill kept">Kept</span>` : `<span class="pill">Suggested</span>`;
  const folded = g.diff_cents === 0 && !ui.showAll && !ui.expanded.has(g.key);
  if (folded) {
    return `<div class="group balanced collapsed" data-key="${esc(g.key)}">
      <span class="g-title">${esc(g.title)}</span>${pill}
      <span class="summary">${esc(g.summary)}</span>
      <div class="g-actions"><button type="button" class="link" data-act="expand">Show</button></div>
    </div>`;
  }
  const acts = [];
  if (!g.kept || g.changed) acts.push(`<button type="button" class="link" data-act="keep">Keep</button>`);
  if (g.multi_month) acts.push(`<button type="button" class="link" data-act="split">Split by month</button>`);
  if (g.next) acts.push(`<button type="button" class="link" data-act="merge">Merge with next</button>`);
  if (g.kept) acts.push(`<button type="button" class="link" data-act="unkeep">Back to suggestion</button>`);
  if (g.diff_cents === 0 && !ui.showAll) acts.push(`<button type="button" class="link" data-act="fold">Fold</button>`);
  const flag = g.changed ? `<div class="flag">${esc(g.changed)}</div>` : "";
  const later = g.later ? `<div class="later">${esc(g.later)}</div>` : "";
  const note = g.kept
    ? `<div class="note-row"><span class="label">Note</span><input type="text" class="note" value="${esc(g.note)}" aria-label="Note for ${esc(g.title)}" placeholder="Why this group is off, or what it covers"></div>`
    : "";
  return `<div class="group ${esc(g.tone)}" data-key="${esc(g.key)}">
    <div class="g-head"><span class="g-title">${esc(g.title)}</span>${pill}<div class="g-actions">${acts.join("")}</div></div>
    ${flag}
    <div class="g-body">
      ${sideHtml(g.share_items, g.shares_total, "No shares", "Shares of withdrawals", ui)}
      ${sideHtml(g.paid_items, g.paid, "No deposits", "Deposits", ui)}
      <div class="sums">
        <span>Deposits</span><span>${esc(g.paid)}</span>
        <span>Shares</span><span>${esc(g.shares_neg)}</span>
        <span class="diff">Difference</span><span class="diff ${textTone(g.tone)}">${esc(g.diff)}</span>
      </div>
    </div>
    ${later}
    ${note}
  </div>`;
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const root = parentElement.querySelector(".mt");
  if (!root || !data) return;
  let ui = UI.get(parentElement);
  if (!ui) {
    ui = { selected: new Set(), expanded: new Set(), showAll: false, seq: 0, drag: [] };
    UI.set(parentElement, ui);
  }
  if (ui.person !== data.person) {
    ui.selected.clear();
    ui.expanded.clear();
    ui.person = data.person;
  }
  const q = (sel) => root.querySelector(sel);
  const groups = data.groups || [];
  const byKey = new Map(groups.map((g) => [g.key, g]));
  const items = new Map();
  for (const g of groups) for (const it of [...g.share_items, ...g.paid_items]) items.set(it.key, { it, g });
  for (const k of [...ui.selected]) if (!items.has(k)) ui.selected.delete(k);
  root.classList.remove("busy");

  const send = (action, extra = {}) => {
    root.classList.add("busy");
    setTriggerValue("edit", {
      id: `${Date.now()}-${++ui.seq}-${Math.random().toString(36).slice(2, 8)}`,
      person: data.person, action, ...extra,
    });
  };
  const keysOf = (ks) => ks.map((k) => { const { it } = items.get(k); return [it.tid, it.side]; });
  // True when every item in ks is already in the group with this key.
  const isHome = (key, ks) => ks.length > 0 && ks.every((k) => items.get(k)?.g.key === key);

  function render() {
    q(".balance-fig").textContent = data.running;
    q(".balance-fig").className = "balance-fig " + textTone(data.running_tone);
    const eq = data.equation;
    const terms = eq.terms.map((t, i) =>
      `${i ? '<span class="op">+</span>' : ""}<span class="term"><span class="muted">${esc(t.label)}</span> <span class="${textTone(t.tone)}">${esc(t.text)}</span></span>`).join("");
    q(".equation").innerHTML =
      (terms || '<span class="term zero">$0.00</span>') +
      (eq.balanced ? `<span class="op">+</span><span class="muted">${eq.balanced} balanced group${eq.balanced === 1 ? "" : "s"} at $0.00</span>` : "") +
      `<span class="op">=</span><span class="term"><b class="${textTone(eq.total_tone)}">${esc(eq.total)}</b></span>` +
      (eq.ok ? '<span class="ok">✓ matches Running balance</span>'
        : `<span class="neg">≠ Running balance ${esc(eq.running)}</span>`);
    q(".head-shares").textContent = `${data.name}’s shares of withdrawals`;
    q(".head-paid").textContent = `${data.name}’s deposits`;
    q(".show-balanced").checked = ui.showAll;
    q(".keep-balanced").disabled = !data.can_keep_balanced;
    q(".groups").innerHTML = groups.length
      ? groups.map((g) => groupHtml(g, ui)).join("")
      : '<div class="empty">No deposits or shares in this range.</div>';

    const bar = q(".selbar");
    bar.hidden = ui.selected.size === 0;
    q(".sel-count").textContent = ui.selected.size;
    // Leave out the group that already holds every selected item.
    const targets = groups.filter((g) => !isHome(g.key, [...ui.selected]));
    const moveTo = q(".move-to");
    const prev = moveTo.value;
    moveTo.innerHTML = targets.map((g) => `<option value="${esc(g.key)}">${esc(g.option)}</option>`).join("");
    if (targets.some((g) => g.key === prev)) moveTo.value = prev;
    q(".sel-label").hidden = q(".move-btn").hidden = !targets.length;
  }

  const groupsEl = q(".groups");
  groupsEl.onclick = (e) => {
    const groupEl = e.target.closest(".group");
    if (!groupEl) return;
    const g = byKey.get(groupEl.dataset.key);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "keep") send("keep", { group: g.key });
    else if (act === "split") send("split", { group: g.key });
    else if (act === "merge") send("merge", { group: g.key, other: g.next });
    else if (act === "unkeep") send("unkeep", { group: g.key });
    else if (act === "expand") { ui.expanded.add(g.key); render(); }
    else if (act === "fold") { ui.expanded.delete(g.key); render(); }
    else {
      const row = e.target.closest(".item");
      if (!row) return;
      const k = row.dataset.item;
      ui.selected.has(k) ? ui.selected.delete(k) : ui.selected.add(k);
      render();
    }
  };
  // Notes are sent when the field loses focus or on Enter, not per keystroke.
  const sendNote = (input) => {
    const g = byKey.get(input.closest(".group")?.dataset.key);
    if (!g || input.dataset.sent === "1" || input.value.trim() === g.note.trim()) return;
    input.dataset.sent = "1";
    send("note", { group: g.key, note: input.value });
  };
  groupsEl.onkeydown = (e) => {
    if (e.target.classList?.contains("note") && e.key === "Enter") { e.preventDefault(); sendNote(e.target); }
  };
  groupsEl.onfocusout = (e) => { if (e.target.classList?.contains("note")) sendNote(e.target); };

  // Drag an item (or the whole selection, if it is selected) onto a group.
  groupsEl.ondragstart = (e) => {
    const row = e.target.closest?.(".item");
    if (!row) return;
    const k = row.dataset.item;
    ui.drag = ui.selected.has(k) ? [...ui.selected] : [k];
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", ui.drag.join(","));
  };
  const clearOver = () => groupsEl.querySelectorAll(".group.dragover").forEach((x) => x.classList.remove("dragover"));
  groupsEl.ondragover = (e) => {
    const el = e.target.closest?.(".group");
    if (!el || !ui.drag.length) return;
    e.preventDefault();
    groupsEl.querySelectorAll(".group.dragover").forEach((x) => x !== el && x.classList.remove("dragover"));
    el.classList.add("dragover");
  };
  groupsEl.ondragleave = (e) => {
    const el = e.target.closest?.(".group");
    if (el && !el.contains(e.relatedTarget)) el.classList.remove("dragover");
  };
  groupsEl.ondrop = (e) => {
    const el = e.target.closest?.(".group");
    if (!el || !ui.drag.length) return;
    e.preventDefault();
    const ks = ui.drag; ui.drag = []; clearOver();
    const target = el.dataset.key;
    if (isHome(target, ks)) return;  // dropped where they are
    ui.selected.clear();
    send("move", { items: keysOf(ks), target });
  };
  groupsEl.ondragend = () => { ui.drag = []; clearOver(); };

  q(".move-btn").onclick = () => {
    const ks = [...ui.selected];
    const target = q(".move-to").value;
    if (!ks.length || !target || isHome(target, ks)) return;
    ui.selected.clear();
    send("move", { items: keysOf(ks), target });
  };
  q(".new-btn").onclick = () => {
    const ks = [...ui.selected];
    if (!ks.length) return;
    ui.selected.clear();
    send("move", { items: keysOf(ks), target: "new" });
  };
  q(".clear-btn").onclick = () => { ui.selected.clear(); render(); };
  q(".show-balanced").onchange = (e) => { ui.showAll = e.target.checked; render(); };
  q(".keep-balanced").onclick = () => send("keep_all_balanced");

  render();
}
"""

_component = None


def _mount():
    """The component's mount callable, registered once per process."""
    global _component
    if _component is None:
        import streamlit as st

        _component = st.components.v2.component(
            "housefin_match_groups", html=_HTML, css=_CSS, js=_JS
        )
    return _component


def _edit_callback(
    conn: sqlite3.Connection, person_id: int, months: Sequence[str] | None, key: str
) -> Callable[[], None]:
    """The ``edit`` trigger's callback, bound to what this run drew. It runs
    once per event, before the next script run."""

    def on_edit() -> None:
        import streamlit as st

        state = st.session_state.get(key) or {}
        event = state.get("edit")
        if not take_event(st.session_state, key, event):
            return
        message = apply_edit(conn, person_id, months, event)
        if message:
            st.session_state[f"_{key}_flash"] = message

    return on_edit


def show(
    conn: sqlite3.Connection,
    person_id: int,
    name: str,
    months: Sequence[str] | None,
    *,
    key: str = "match_groups",
) -> None:
    """Draw the person's groups for ``months`` (all time through the latest
    one), or an error instead if their differences don't add up to the
    Running balance."""
    import streamlit as st

    message = st.session_state.pop(f"_{key}_flash", None)
    if message:
        st.warning(message)
    months = list(months or ())
    groups = matching.groups_for(conn, person_id, months)
    check = matching.check_groups(conn, person_id, months, groups)
    if not check.ok:
        st.error(
            f"The groups' differences add up to {ui.md_cents(check.groups_cents)}, but "
            f"{name}'s Running balance is {ui.md_cents(check.running_cents)} (off by "
            f"{ui.md_cents(check.off_by_cents)}). This is a bug in the matching code, so the "
            "groups aren't shown.",
            title="Groups don't add up to the Running balance",
        )
        return
    _mount()(
        key=key,
        data=component_data(
            person_id, name, groups, check.running_cents,
            last_event=st.session_state.get(f"_{key}_last_event"),
        ),
        on_edit_change=_edit_callback(conn, person_id, months, key),
    )
