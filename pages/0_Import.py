"""Import: upload bank CSV exports in the browser (editors only).

Same import as ingest.py: files imported before are skipped, overlapping
exports add only new rows, and each file is saved whole or not at all.
"""

import streamlit as st

from housefin import ingest, ui

st.set_page_config(page_title="Import · House Finances", layout="wide")
conn = ui.get_conn()
ss = st.session_state

st.title("Import")
st.caption(
    "Upload one or more CSV exports from the bank. Re-uploading an export, or one that "
    "overlaps an earlier one, is safe: transactions already stored are skipped. "
    "Remembered rules tag the new rows."
)

if not ui.can_edit():
    st.info("View only: an editor can import bank exports.")
    st.stop()

TONE = {ingest.IMPORTED: "success", ingest.SKIPPED: "info", ingest.FAILED: "error"}

reports = ss.pop("_import_reports", None)
if reports:
    for r in reports:
        lines = ingest.format_report(r).splitlines()
        getattr(st, TONE[r.status])(
            "\n\n".join([f"**{lines[0]}**", *(f"`{ln.strip()}`" for ln in lines[1:])])
        )
    if len(reports) > 1:
        st.markdown(ingest.format_totals(reports))
    if any(r.rows_new for r in reports):
        st.markdown("Tag the new transactions on the [Categorize page](Categorize).")

uploader_key = f"import_files_{ss.get('_import_ver', 0)}"
files = st.file_uploader(
    "Bank CSV exports", type="csv", accept_multiple_files=True, key=uploader_key
)
if st.button("Import", type="primary", disabled=not files):
    ss["_import_reports"] = [ingest.ingest_bytes(conn, f.name, f.getvalue()) for f in files]
    ss["_import_ver"] = ss.get("_import_ver", 0) + 1  # clear the uploader
    st.rerun()
