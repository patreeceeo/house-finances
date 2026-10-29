"""Streamlit helpers shared by the pages: the session's database connection,
the sidebar month picker and date columns. Streamlit is imported inside the
functions that need it, so the rest can be tested without it.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

from housefin import auth, dates, db, fmt

ALL = "All"


def db_path() -> Path:
    """Database file: see :func:`housefin.db.db_path`."""
    return db.db_path()


def get_conn() -> sqlite3.Connection:
    """One DB connection per browser session (Streamlit reruns reuse it),
    read-only for viewers. Every page calls this first, so it also signs the
    session in (:func:`get_user`)."""
    import streamlit as st

    user = get_user()
    show_user(user)
    path = str(db_path())
    conn = st.session_state.get("_housefin_conn")
    if conn is None or st.session_state.get("_housefin_conn_path") != path:
        conn = connect_threadsafe(path, read_only=not user.can_edit)
        st.session_state["_housefin_conn"] = conn
        st.session_state["_housefin_conn_path"] = path
    return conn


def connect_threadsafe(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Like db.connect, but usable from the different threads Streamlit may run
    a session's reruns on (only one rerun per session runs at a time).
    ``read_only``: SQLite refuses every write on the connection."""
    db.connect(path).close()  # migrate + seed
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    return conn


# --- signed-in user ------------------------------------------------------------


def get_user() -> auth.User:
    """The session's user (see :mod:`housefin.auth`), checked once per browser
    session. Without a valid sign-in the page stops with an error."""
    import streamlit as st

    user = st.session_state.get("_housefin_user")
    if user is None:
        try:
            config = auth.access_config()
            key = _signing_key(config) if config else None
            user = auth.current_user(st.context.headers, signing_key=key)
        except auth.AuthError as e:
            st.error(f"Can't sign you in. {e}")
            st.stop()
        st.session_state["_housefin_user"] = user
    return user


def can_edit() -> bool:
    """Whether this session's user may change data."""
    return get_user().can_edit


def show_user(user: auth.User) -> None:
    """Who is signed in, in the sidebar (nothing for a local editor)."""
    import streamlit as st

    if user.email:
        st.sidebar.caption(
            f"Signed in as {user.email}" + ("" if user.can_edit else " (view only)")
        )
    elif not user.can_edit:
        st.sidebar.caption("View only (HOUSEFIN_ROLE)")


def _signing_key(config: auth.AccessConfig):
    """Cloudflare's published keys, shared by all sessions."""
    import streamlit as st

    @st.cache_resource
    def keys(team_domain: str, audience: str):
        return auth.jwks_signing_key(auth.AccessConfig(team_domain, audience))

    return keys(config.team_domain, config.audience)


# --- messages after a rerun ---------------------------------------------------


def flash(page: str, text: str, kind: str = "success") -> None:
    """Show ``text`` on ``page``'s next run, e.g. after st.rerun(). ``kind``
    is the Streamlit call that shows it: success, info, warning or error."""
    import streamlit as st

    st.session_state[f"_{page}_flash"] = (kind, text)


def show_flash(page: str) -> None:
    """Show (once) the message :func:`flash` left for ``page``, if any."""
    import streamlit as st

    message = st.session_state.pop(f"_{page}_flash", None)
    if message:
        kind, text = message
        getattr(st, kind)(text)


# --- month picker ------------------------------------------------------------


def month_selector(
    conn: sqlite3.Connection, *, key: str = "months", container=None
) -> list[str] | None:
    """Sidebar month picker. Returns None for All (default) or a sorted month list.

    The choice persists across pages: Streamlit drops widget state for widgets
    not rendered on the current page, so it is mirrored in non-widget keys.
    """
    import streamlit as st

    where = container if container is not None else st.sidebar
    ss = st.session_state
    mode_key, picked_key = f"_{key}_mode", f"_{key}_picked"
    present = dates.months_present(conn)
    # Restore from the mirror (dropping months no longer in the data).
    if mode_key not in ss:
        ss[mode_key] = ss.get(f"{key}_saved_mode", ALL)
    if picked_key not in ss:
        ss[picked_key] = [m for m in ss.get(f"{key}_saved_picked", []) if m in present]

    choice = where.radio(
        "Month", [ALL, "Months"], horizontal=True, key=mode_key,
        help="All time, or pick one or more months present in the data.",
    )
    picked: list[str] = []
    if choice != ALL:
        picked = where.multiselect(
            "Months", present, format_func=fmt.month, key=picked_key,
            placeholder="Choose months",
        )
        if not picked:
            where.caption("No months picked; showing all time.")
    ss[f"{key}_saved_mode"] = choice
    ss[f"{key}_saved_picked"] = list(ss.get(picked_key, []))
    return normalize_month_selection(choice, picked)


def normalize_month_selection(choice: str, picked: list[str]) -> list[str] | None:
    """Selection from the month widget: None for All, else sorted months.

    Picking "Months" with nothing selected falls back to All.
    """
    if choice == ALL or not picked:
        return None
    return sorted(picked)


def describe_months(selection: list[str] | None) -> str:
    """'All time', or the selected months: 'Sep 2026, Oct 2026'."""
    if not selection:
        return "All time"
    return ", ".join(fmt.month(m) for m in selection)


# --- date columns ------------------------------------------------------------


def date_column_format(locale: str | None = None) -> str:
    """The browser's date pattern as a moment.js format for
    st.column_config.DateColumn, so table dates read like fmt.date."""
    pattern = fmt.date_pattern(locale)
    if pattern is None:
        return "YYYY-MM-DD"
    out, literal = [], False
    for part in pattern.split("'"):
        if literal:
            out.append(f"[{part}]" if part else "'")
        else:
            out.append(part.replace("dd", "DD").replace("yyyy", "YYYY"))
        literal = not literal
    return "".join(out)


def date_column(label: str, **kwargs):
    """A DateColumn shown in the browser's date format."""
    import streamlit as st

    return st.column_config.DateColumn(label, format=date_column_format(), **kwargs)


def to_dates(values) -> list:
    """'YYYY-MM-DD' strings -> datetime.date (None stays None), for date columns."""
    return [None if not v else _dt.date.fromisoformat(str(v)[:10]) for v in values]
