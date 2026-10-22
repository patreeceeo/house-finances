"""Import bank CSV exports: python ingest.py [file.csv ...]

With no paths, a file picker opens to choose one or more CSVs: under WSL the
Windows Open dialog (via powershell.exe), elsewhere tkinter (needs a
display). Re-importing the same or overlapping
exports adds nothing new.
"""

from __future__ import annotations

import argparse
import os
import subprocess
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


# Windows Open dialog, one chosen path per output line. The owner form is
# TopMost so the dialog is not hidden behind the terminal.
_WINDOWS_PICKER_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = 'Choose bank CSV exports to import'
$d.Filter = 'CSV files (*.csv)|*.csv|All files (*.*)|*.*'
$d.Multiselect = $true
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { $d.FileNames }
"""


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def pick_files() -> list[str]:
    """Native multi-file picker: the Windows dialog under WSL, else tkinter.
    Raises PickerUnavailable when neither can be shown."""
    if is_wsl():
        return pick_files_windows()
    return pick_files_tk()


def pick_files_windows() -> list[str]:
    """Windows Open dialog from WSL; paths come back as WSL paths."""
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", _WINDOWS_PICKER_PS],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
    except FileNotFoundError:
        raise PickerUnavailable("powershell.exe is not on PATH") from None
    except subprocess.CalledProcessError as e:
        raise PickerUnavailable(f"the Windows file dialog failed: {e.stderr.strip()}") from None
    windows_paths = [line.strip() for line in out.splitlines() if line.strip()]
    return [
        subprocess.run(
            ["wslpath", "-u", wp], capture_output=True, text=True, check=True
        ).stdout.strip()
        for wp in windows_paths
    ]


def pick_files_tk() -> list[str]:
    """tkinter multi-file picker. Raises PickerUnavailable without tkinter/display."""
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
