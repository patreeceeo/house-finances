#!/usr/bin/env bash
# Nightly backup: a consistent copy of the database (SQLite's backup API, so
# never a half-written file), checked, compressed, copied off the server with
# rclone, and old local copies pruned. Settings come from the environment
# (see house-finances.env.example).
set -euo pipefail

cd "$(dirname "$0")/.."
db="${HOUSEFIN_DB:-finances.db}"
dir="${BACKUP_DIR:-/var/backups/house-finances}"
out="$dir/finances-$(date +%F).db"
mkdir -p "$dir"

uv run --frozen python -I - "$db" "$out.tmp" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True), sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
sys.exit(0 if ok == "ok" else f"integrity check failed: {ok}")
PY
mv -f "$out.tmp" "$out"
gzip -f "$out"

if [[ -n "${RCLONE_REMOTE:-}" ]]; then
    rclone copy "$out.gz" "$RCLONE_REMOTE"
fi
find "$dir" -name 'finances-*.db.gz' -mtime "+${KEEP_DAYS:-14}" -delete
echo "Backed up to $out.gz"
