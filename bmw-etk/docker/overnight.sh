#!/usr/bin/env bash
# One unattended pass: rebuild the image, attach the ETK catalog, dump the schema.
#
#   bash bmw-etk/docker/overnight.sh
#
# Uses NO Claude usage -- this is just Docker on your Mac. It has a hard wall-clock
# limit and cannot loop forever: every stage is bounded, and a watchdog kills the
# container if a stage overruns. Everything is logged to ~/etk_overnight.log and
# schema dumps land in bmw-etk/data/schema/.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
LOG="$HOME/etk_overnight.log"
SCHEMA="$ROOT/data/schema"
ROM="$ROOT/dump/rfiles"
IMAGE="etk-transbase"
VOLUME="etk-data"
PLATFORM="${ETK_PLATFORM:-linux/amd64}"
HOSTNAME_FIXED="etkdb"
CONTAINER="etk-overnight"
ISO="${ETK_ISO:-/Volumes/BMW ETK 2020-01}"

# Wall-clock ceilings per stage (seconds). Nothing runs longer than these.
ATTACH_MAX="${ATTACH_MAX:-5400}"     # 90 min
EXPLORE_MAX="${EXPLORE_MAX:-18000}"  # 5 hours

exec > >(tee -a "$LOG") 2>&1
echo "================================================================"
echo "ETK overnight run started: $(date)"
echo "================================================================"

DOCKER=""
for c in "$(command -v docker 2>/dev/null)" "$HOME/.docker/bin/docker" \
         /usr/local/bin/docker /opt/homebrew/bin/docker \
         /Applications/Docker.app/Contents/Resources/bin/docker; do
  [ -n "$c" ] && [ -x "$c" ] && { DOCKER="$c"; break; }
done
[ -n "$DOCKER" ] || { echo "FATAL: docker not found"; exit 1; }
"$DOCKER" info >/dev/null 2>&1 || { echo "FATAL: Docker Desktop is not running"; exit 1; }
echo "docker: $DOCKER"

[ -d "$ROM" ] || { echo "FATAL: ROM files missing at $ROM"; exit 1; }
mkdir -p "$SCHEMA"

# Run a container with a watchdog so no stage can hang indefinitely.
run_stage() {
  local name="$1" limit="$2"; shift 2
  echo
  echo "---------- STAGE: $name (limit ${limit}s) ----------"
  "$DOCKER" rm -f "$CONTAINER" >/dev/null 2>&1
  "$DOCKER" run --rm --name "$CONTAINER" --platform "$PLATFORM" \
    --hostname "$HOSTNAME_FIXED" \
    -e DB=etk_publ -e DBUSER=tbadmin -e DBPASS=altabe \
    -e GLOBAL_BUDGET="$limit" \
    -v "$ROM":/rom:ro -v "$VOLUME":/data -v "$SCHEMA":/out \
    -v "$HERE/attach.sh":/attach.sh:ro \
    -v "$HERE/explore.sh":/explore.sh:ro \
    -v "$HERE/diagnose.sh":/diagnose.sh:ro \
    "$IMAGE" "$@" &
  local pid=$!
  ( sleep "$limit"; "$DOCKER" kill "$CONTAINER" >/dev/null 2>&1 \
      && echo "*** WATCHDOG: killed '$name' after ${limit}s ***" ) &
  local dog=$!
  wait "$pid"; local rc=$?
  kill "$dog" >/dev/null 2>&1
  echo "---------- STAGE $name finished, exit $rc ----------"
  return $rc
}

echo
echo "########## 1. BUILD ##########"
if [ -d "$ISO" ]; then
  cp "$ISO/transbase_linux/transbase_linux.tar.gz" "$HERE/" 2>/dev/null
fi
if [ -f "$HERE/transbase_linux.tar.gz" ]; then
  "$DOCKER" build --platform "$PLATFORM" --build-arg BASE_IMAGE=debian:bullseye-slim \
    -t "$IMAGE" "$HERE" || { echo "FATAL: build failed"; exit 1; }
  rm -f "$HERE/transbase_linux.tar.gz"
else
  echo "NOTE: ISO not mounted and no local tarball; reusing the existing image."
  "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1 || { echo "FATAL: no image"; exit 1; }
fi
"$DOCKER" volume create "$VOLUME" >/dev/null

echo
echo "########## 2. ATTACH ##########"
# The registry now lives on the volume, so re-attach cleanly from scratch.
"$DOCKER" run --rm --platform "$PLATFORM" --hostname "$HOSTNAME_FIXED" \
  -v "$VOLUME":/data "$IMAGE" bash -lc 'rm -rf /data/etk_publ /data/_tbconf /data/transbase' >/dev/null 2>&1
run_stage attach "$ATTACH_MAX" bash /attach.sh
ATTACH_RC=$?

if [ "$ATTACH_RC" -ne 0 ]; then
  echo
  echo "Attach did not succeed (exit $ATTACH_RC). Stopping here rather than"
  echo "exploring a database that is not there. Paste $LOG to Claude."
  exit "$ATTACH_RC"
fi

echo
echo "########## 2b. VERIFY REGISTRATION + DIAGNOSE ##########"
# The previous run reported a successful attach while dblist.ini stayed empty,
# so never trust the attach's exit code alone -- check the registry.
run_stage diagnose 600 bash /diagnose.sh

echo
echo "########## 3. EXPLORE SCHEMA ##########"
run_stage explore "$EXPLORE_MAX" bash /explore.sh
EXPLORE_RC=$?

echo
echo "########## 3b. BMW's OWN SERVER INIT SCRIPT (for reference) ##########"
for f in rc.TransBase rc.tbenv rc.tbstop; do
  if [ -f "$ISO/transbase_linux/$f" ]; then
    echo "--- $f ---"; sed -n '1,60p' "$ISO/transbase_linux/$f"; echo
  fi
done

echo
echo "########## 4. RESULTS ##########"
ls -la "$SCHEMA" 2>/dev/null
echo
echo "--- biggest tables (top 40) ---"
head -40 "$SCHEMA/rowcounts.txt" 2>/dev/null || echo "(no row counts)"
[ -f "$SCHEMA/INCOMPLETE.txt" ] && cat "$SCHEMA/INCOMPLETE.txt"

echo
echo "########## 5. SAVE SCHEMA METADATA ##########"
# Commit locally only. NEVER push from here: an unattended run must not block on a
# credential prompt, and this Mac has no stored GitHub credentials -- pushes are
# done from the Claude session. GIT_TERMINAL_PROMPT=0 makes any git operation that
# wants input fail immediately instead of hanging forever.
export GIT_TERMINAL_PROMPT=0
if [ -n "$(ls -A "$SCHEMA" 2>/dev/null | grep -v '^\.gitkeep$')" ]; then
  cd "$REPO" && {
    git add -A bmw-etk/data/schema 2>/dev/null
    git -c user.email=mendy@justsomecarparts.com -c user.name=mendy \
        commit -q -m "BMW ETK: overnight schema dump from the attached catalog" 2>/dev/null \
      && echo "Committed locally. To publish: git push -u origin claude/bmw-etk-database-sqohoo" \
      || echo "(nothing new to commit)"
  }
else
  echo "No schema files were produced, so nothing to commit."
fi

echo
echo "================================================================"
echo "ETK overnight run finished: $(date)   attach=$ATTACH_RC explore=$EXPLORE_RC"
echo "Log: $LOG"
echo "================================================================"
