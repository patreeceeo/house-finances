"""Import bank CSV exports: python ingest.py [file.csv ...]

With no paths, a file picker opens to choose one or more CSVs (needs a
display; under WSL that means WSLg). Re-importing the same or overlapping
exports adds nothing new.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from housefin import db
from housefin.ingest import all_failed, format_report, format_totals, ingest_files

NO_PICKER_MSG = (
    "No CSV paths given and no file picker is available ({reason}).\n"
    "Pass the files on the command line instead, e.g.:\n"
    "  uv run python ingest.py ~/Downloads/transactions.csv"
)


class PickerUnavailable(RuntimeError):
    pass


def pick_files() -> list[str]:
    """Native multi-file picker. Raises PickerUnavailable without tkinter/display."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise PickerUnavailable("tkinter is not installed") from None
    try:
        root = tkinter.Tk()
    except tkinter.TclError as e:
        raise PickerUnavailable(f"no display: {e}") from None
    try:
        root.withdraw()
        chosen = filedialog.askopenfilenames(
            parent=root,
            title="Choose bank CSV exports to import",
            filetypes=[("CSV files", "*.csv"), ("All files", "*")],
        )
    finally:
        root.destroy()
    return list(chosen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import bank CSV exports into the finances database.")
    parser.add_argument("files", nargs="*", help="CSV files to import (omit to pick in a file browser)")
    parser.add_argument(
        "--db", default=str(db.DEFAULT_DB_PATH), help="database file (default: %(default)s)"
    )
    args = parser.parse_args(argv)

    paths = args.files
    if not paths:
        try:
            paths = pick_files()
        except PickerUnavailable as e:
            print(NO_PICKER_MSG.format(reason=e), file=sys.stderr)
            return 2
        if not paths:
            print("No files selected; nothing imported.")
            return 0

    conn = db.connect(Path(args.db))
    try:
        reports = ingest_files(conn, paths)
    finally:
        conn.close()
    for report in reports:
        print(format_report(report))
    if len(reports) > 1:
        print(format_totals(reports))
    return 1 if all_failed(reports) else 0


if __name__ == "__main__":
    sys.exit(main())
