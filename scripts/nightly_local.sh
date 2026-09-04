#!/bin/bash
# Nightly fitment sweep, run on mendy's Mac instead of GitHub Actions.
#
# WHY THIS EXISTS: GitHub silently dropped the scheduled trigger on 2026-08-27 and again on
# 2026-08-28 -- two misses in three nights, confirmed both times by the eBay quota counter
# sitting at ~0 hours after the cron should have fired. Moving the minute off :30 did not
# help. A schedule that runs "most nights" is not automation, so the job moved here.
#
# Mirrors .github/workflows/fitment-sweep.yml step for step. The workflow is KEPT for
# manual runs (workflow_dispatch) -- its schedule is removed so the two can never
# double-spend the 5,000/day GetItem quota or race each other's ledger commit.
#
# Installed via ~/Library/LaunchAgents/com.mendy.fitment-sweep.plist
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
BRANCH="claude/ebay-fitment-chassis-rules-3pztn4"
LOG="$ROOT/data/nightly_local.log"
: "${NIGHTLY_LIMIT:=2000}"

say() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

say "=== nightly sweep starting (limit $NIGHTLY_LIMIT) ==="

# A long sweep must not be suspended when the lid closes or the display sleeps.
# -d display, -i idle, -m disk, -s system.
CAF="caffeinate -dims"

# Start from the remote's state -- the GitHub job may still have run manually, and the
# ledger is append-mostly, so rebasing on top of whatever arrived is the right merge.
git fetch -q origin "$BRANCH" && git rebase -q "origin/$BRANCH" 2>&1 | tee -a "$LOG"

$CAF python3 scripts/selftest.py >>"$LOG" 2>&1 || { say "SELFTEST FAILED -- aborting, no writes made"; exit 1; }
say "selftest passed"

# eBay AUTH PRE-FLIGHT. The selftest is offline, so it cannot catch a dead credential.
# The eBay refresh token is shared with the sibling reporting project and was re-minted
# once on 2026-08-28; a future re-consent could revoke the copy we hold. Without this the
# failure lands mid-sweep, unattended, having already pushed to some listings -- so fail
# FAST and LOUD instead, before anything is written.
if ! $CAF python3 scripts/ebay_auth.py --check >>"$LOG" 2>&1; then
  say "EBAY AUTH FAILED -- refresh token rejected. Aborting before any write."
  say "  Someone may have re-consented and revoked this grant. Check ebay_auth.json"
  say "  against the sibling project ~/Documents/GitHub/ebay-listing-reports/."
  exit 1
fi
say "eBay auth OK"

# Donor refresh is BEST-EFFORT but must be VISIBLE: it broke on 2026-08-21 and ran stale
# for three nights because the job stayed green. The script leaves the existing dump
# untouched on failure, so continuing is safe -- but we record it and say so at the end.
DONOR_FAILED=0
if [ -f shopify_token.txt ] || [ -f shopify.env ]; then
  if $CAF python3 scripts/shopify_donor.py --dump >>"$LOG" 2>&1; then
    say "donor dump refreshed from Shopify"
  else
    DONOR_FAILED=1; say "WARNING: Shopify donor refresh FAILED -- swept on the committed dump"
  fi
else
  DONOR_FAILED=1; say "WARNING: no Shopify credentials on this Mac -- donor dump NOT refreshed."
  say "         Cars parted out since the last refresh are invisible to this run."
fi

# SIZE THE RUN TO THE ACTUAL ALLOWANCE, rather than a fixed guess. The old hard limit of
# 2,000 consumed only 1,070 of the 5,000/day GetItem quota (21%) -- most SKUs are decided on
# cheap Inventory reads and never reach the Trading guard. ebay_quota.py asks eBay what is
# left, reserves enough for the two audits, and sizes from there. It falls back to 2,000 if
# the quota endpoint is unreachable, so a failed read can never push the run HIGHER.
LIMIT=$($CAF python3 scripts/ebay_quota.py --limit 2>/dev/null || echo "$NIGHTLY_LIMIT")
case "$LIMIT" in ''|*[!0-9]*) LIMIT="$NIGHTLY_LIMIT" ;; esac
say "quota check: $($CAF python3 scripts/ebay_quota.py 2>/dev/null | tr '\n' ' ')"
say "sweeping (live), limit $LIMIT..."
$CAF python3 scripts/ebay_batch.py apply --from-shopify --from-inventory --live \
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv \
  --etk-fitment data/etk_fitment.csv.gz \
  --limit "$LIMIT" >>"$LOG" 2>&1
say "sweep finished (exit $?)"

# Did what we pushed actually DISPLAY? The ledger only proves we pushed.
$CAF python3 scripts/ebay_display_audit.py --recent 300 --quiet --learn \
  --fail-under 70 --csv data/display_audit.csv >>"$LOG" 2>&1 || say "display audit below threshold"
# Has anyone overwritten fitment we pushed earlier? (Dismantly/PartOutPro/eBay auto-fitment)
$CAF python3 scripts/ebay_display_audit.py --sample 250 --quiet --learn \
  --fail-on-leak --fail-under 60 --csv data/drift_audit.csv >>"$LOG" 2>&1 || say "drift audit flagged"

git add -A data/
if git diff --cached --quiet; then
  say "no state changes to commit"
else
  git -c user.name="fitment-bot" -c user.email="fitment-bot@users.noreply.github.com" \
      commit -q -m "Automated fitment sweep (local): refresh dump + ledger [skip ci]"
  MINE=$(git rev-parse HEAD)
  for attempt in 1 2 3 4 5; do
    if git push -q origin "HEAD:$BRANCH"; then say "state pushed (attempt $attempt)"; break; fi
    say "push rejected -- rebasing and retrying"
    git fetch -q origin "$BRANCH"
    if ! git -c core.editor=true rebase FETCH_HEAD; then
      git checkout "$MINE" -- data/ 2>/dev/null || true
      git add data/ 2>/dev/null || true
      git -c core.editor=true rebase --continue || git rebase --abort
    fi
    sleep $((attempt * 5))
  done
fi

[ "$DONOR_FAILED" = "1" ] && say "NOTE: donor dump was NOT refreshed this run."
say "=== done ==="
