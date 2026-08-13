#!/usr/bin/env bash
#
# Convenience wrapper for the decisive inventory-item test, made for web
# terminals (Codespaces / Cloud Shell) that mangle pasted commands.
#
# Setup (once): create a file named  token.txt  in this folder (the repo root)
# and paste your eBay OAuth user token into it using the EDITOR (not the
# terminal). token.txt is gitignored, so it never gets committed.
#
# Usage:  bash run.sh SKU1 [SKU2 SKU3 ...]
# Example: bash run.sh 5978
#
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f token.txt ]; then
  echo "ERROR: token.txt not found in $(pwd)." >&2
  echo "Create it via the editor and paste your OAuth token into it." >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  echo "Usage: bash run.sh SKU1 [SKU2 ...]" >&2
  echo "Get a SKU from eBay Seller Hub - Listings - Active - 'Custom label' column." >&2
  exit 1
fi

# Strip ALL whitespace (stray newlines/spaces) — eBay tokens contain none.
TOKEN="$(tr -d '[:space:]' < token.txt)"

if [ -z "$TOKEN" ]; then
  echo "ERROR: token.txt is empty after stripping whitespace." >&2
  exit 1
fi

# Guard against the classic web-terminal bracketed-paste junk landing in the file.
case "$TOKEN" in
  *"[200~"*|*$'\e'*)
    echo "ERROR: token.txt contains paste-escape junk (e.g. '^[[200~')." >&2
    echo "Open token.txt in the editor, delete everything, and re-paste just the token." >&2
    exit 1
    ;;
esac

ARGS=()
for sku in "$@"; do
  ARGS+=(--sku "$sku")
done

python3 scripts/ebay_inventory_test.py test --token "$TOKEN" "${ARGS[@]}"
