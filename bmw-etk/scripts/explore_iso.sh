#!/usr/bin/env bash
# Reconnaissance on a mounted BMW ETK ISO.
#
#   bash bmw-etk/scripts/explore_iso.sh "/Volumes/BMW ETK 2020-01"
#
# Read-only: prints config files, directory trees, and file signatures so we can
# work out how the ETK data archive is restored and which database engine serves it.
set -uo pipefail

MP="${1:-/Volumes/BMW ETK 2020-01}"
[ -d "$MP" ] || { echo "Not mounted: $MP" >&2; exit 2; }

hr() { printf '\n========== %s ==========\n' "$1"; }
# Print a file only if it exists and is small enough to be worth dumping.
show() {
  local f="$MP/$1" limit="${2:-200}"
  [ -f "$f" ] || { echo "(missing: $1)"; return; }
  echo "--- $1 ---"
  head -c 200000 "$f" | head -n "$limit"
  echo
}

hr "THIS MAC"
echo "Architecture : $(uname -m)     (arm64 = Apple Silicon, x86_64 = Intel)"
echo "macOS        : $(sw_vers -productVersion 2>/dev/null)"
echo "Free space   :"
df -h / "$HOME" 2>/dev/null | sort -u

hr "VERSION / README"
show version.txt
show version.ini
show cdrun.ini
show Readme.txt 120

hr "SERVER INSTALL SCRIPT (how the data gets loaded)"
show install_server.sh 100

hr "Daten/ (German for 'data')"
ls -la "$MP/Daten" 2>/dev/null
for f in "$MP"/Daten/*; do
  [ -f "$f" ] || continue
  echo "--- Daten/$(basename "$f") ---"
  head -c 4000 "$f"
  echo
done

hr "TRANSBASE (the database engine)"
echo "--- transbase/ ---"
find "$MP/transbase" -maxdepth 3 2>/dev/null | head -60
echo
echo "--- transbase_linux/ ---"
find "$MP/transbase_linux" -maxdepth 3 2>/dev/null | head -60

hr "INSTALL DIR"
find "$MP/install" -maxdepth 2 2>/dev/null | head -60

hr "STANDALONE DIR (a no-server mode would be ideal)"
find "$MP/standalone" -maxdepth 3 2>/dev/null | head -50

hr "MIGRATION DIR"
find "$MP/migration" -maxdepth 2 2>/dev/null | head -40

hr "WHO MENTIONS 'jetarch'? (how the 6 parts are reassembled)"
grep -ril 'jetarch' "$MP/install" "$MP/standalone" "$MP/migration" "$MP/admintool" \
     "$MP/etk_nutzer" "$MP"/*.sh "$MP"/*.ini "$MP"/*.txt 2>/dev/null | head -20
echo "--- context lines ---"
grep -rih -m3 -A2 -B2 'jetarch' "$MP/install" "$MP/standalone" "$MP"/*.sh "$MP"/*.txt 2>/dev/null | head -40

hr "TRANSBASE JDBC DRIVER? (would let us query without the ETK app)"
find "$MP" -maxdepth 4 -iname '*transbase*' 2>/dev/null | head -30
echo "--- jars mentioning transbase/tb ---"
find "$MP/javaserver" "$MP/javaclient" "$MP/standalone" -maxdepth 3 -iname '*.jar' 2>/dev/null \
  | grep -iE 'transbase|tbjdbc|/tb' | head -20

hr "SIGNATURE OF THE DATA ARCHIVE"
P1=$(ls "$MP"/ETK-Data_*.jetarch.part1 2>/dev/null | head -1)
if [ -n "$P1" ]; then
  echo "First 64 bytes of $(basename "$P1"):"
  xxd -l 64 "$P1" 2>/dev/null || od -A x -t x1z -N 64 "$P1"
  echo
  echo "Printable strings near the start:"
  head -c 4096 "$P1" | strings | head -20
fi
echo
echo "MD5 sidecar files (expected checksums):"
for m in "$MP"/ETK-Data_*.md5.part*; do
  [ -f "$m" ] && echo "  $(basename "$m"): $(cat "$m")"
done

hr "DONE"
echo "Paste everything above back to Claude."
