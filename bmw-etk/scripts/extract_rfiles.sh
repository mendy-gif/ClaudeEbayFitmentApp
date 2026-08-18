#!/usr/bin/env bash
# Extract the Transbase ROM database files from the ETK jetarch, and inspect the
# Linux Transbase build so we know what a container needs.
#
#   bash bmw-etk/scripts/extract_rfiles.sh ["/Volumes/BMW ETK 2020-01"]
#
# Writes ~5.7 GB into bmw-etk/dump/rfiles/ (gitignored). Read-only on the ISO.
set -uo pipefail

MP="${1:-/Volumes/BMW ETK 2020-01}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/dump/rfiles"

[ -d "$MP" ] || { echo "ISO not mounted at: $MP" >&2; exit 2; }

echo "========== FREE SPACE BEFORE =========="
df -h "$ROOT" | tail -1

echo
echo "========== EXTRACTING ROM FILES (~5.7 GB, takes a few minutes) =========="
mkdir -p "$OUT"
python3 "$HERE/jetarch.py" extract "$MP" -o "$OUT" --only "rfile*"

echo
echo "========== WHAT LANDED =========="
ls -la "$OUT/files" 2>/dev/null || ls -la "$OUT"
echo
du -sh "$OUT"

echo
echo "========== LINUX TRANSBASE BUILD =========="
TB="$MP/transbase_linux/transbase_linux.tar.gz"
if [ -f "$TB" ]; then
  echo "--- archive contents ---"
  tar -tzf "$TB" | head -60
  echo
  echo "--- total entries ---"
  tar -tzf "$TB" | wc -l
  echo
  echo "--- looking for the tools postinstallDataDB.cmd uses (tbadm / tbi) ---"
  tar -tzf "$TB" | grep -iE 'tbadm|tbi|tbserver|libtb|\.so' | head -30
else
  echo "(not found: $TB)"
fi

echo
echo "========== createdb.sh (how a database is made on Linux) =========="
sed -n '1,60p' "$MP/transbase_linux/createdb.sh" 2>/dev/null

echo
echo "========== rc.tbenv (environment the server expects) =========="
sed -n '1,40p' "$MP/transbase_linux/rc.tbenv" 2>/dev/null

echo
echo "========== FREE SPACE AFTER =========="
df -h "$ROOT" | tail -1
