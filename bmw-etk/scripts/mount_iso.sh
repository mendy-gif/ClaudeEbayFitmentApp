#!/usr/bin/env bash
# Mount a BMW ETK ISO read-only on macOS and report what is inside.
#
#   bash bmw-etk/scripts/mount_iso.sh "/path/to/BMW ETK 2020-01.iso"
#
# Mounting is read-only and copies nothing -- it costs no disk space and cannot
# modify the ISO. Unmount later with:  hdiutil detach /Volumes/<name>
set -uo pipefail

ISO="${1:-}"
if [ -z "$ISO" ]; then
  echo "usage: bash $0 \"/path/to/BMW ETK 2020-01.iso\"" >&2
  exit 2
fi
if [ ! -f "$ISO" ]; then
  echo "No such file: $ISO" >&2
  exit 2
fi

echo "ISO: $ISO"
ls -lh "$ISO" | awk '{print "Size: " $5}'
echo

# Already mounted? Reuse it rather than mounting a second copy.
MP=$(hdiutil info 2>/dev/null | awk -v iso="$ISO" '
  $0 ~ /^image-path[[:space:]]*:/ { path=$0; sub(/^image-path[[:space:]]*:[[:space:]]*/, "", path) }
  path == iso && /\/Volumes\// { for (i=1; i<=NF; i++) if ($i ~ /^\/Volumes\//) { print $i; exit } }')

if [ -n "$MP" ]; then
  echo "Already mounted at: $MP"
else
  echo "Mounting (read-only)..."
  MP=$(hdiutil attach -readonly -nobrowse "$ISO" | grep -o '/Volumes/.*' | head -1)
  if [ -z "$MP" ]; then
    echo "Mount failed. The ISO may be a format macOS cannot read (some are UDF" >&2
    echo "or have a proprietary layout). Tell Claude and we will try another route." >&2
    exit 1
  fi
  echo "Mounted at: $MP"
fi

echo
echo "=== Top level ==="
ls -la "$MP"

echo
echo "=== Size of each top-level item ==="
du -sh "$MP"/* 2>/dev/null | sort -h

echo
echo "=== Deep scan (identify the actual database) ==="
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/identify_dump.py" ]; then
  python3 "$HERE/identify_dump.py" "$MP"
else
  echo "(identify_dump.py not found next to this script; skipping)"
fi

echo
echo "Done. The ISO stays mounted at: $MP"
echo "Unmount when finished with:  hdiutil detach \"$MP\""
