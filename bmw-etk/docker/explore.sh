#!/bin/bash
# Explore the attached ETK schema and write everything to /out.
# Runs INSIDE the container. Safe to run unattended -- every query is isolated so
# one failure never stops the rest.
set -u
DB="${DB:-etk_publ}"
DBU="${DBUSER:-tbadmin}"
PASS="${DBPASS:-altabe}"
OUT=/out
mkdir -p "$OUT"

strip() { grep -vE '^\s*$|^@\(#\)|^ *Version:|^ *License:|U\.S\.-Patent|Copyright \(c\)'; }

# --- hard limits, so nothing can spin forever unattended ---------------------
TBI_TIMEOUT="${TBI_TIMEOUT:-120}"      # seconds per catalog query
COUNT_TIMEOUT="${COUNT_TIMEOUT:-180}"  # seconds per count(*)
MAX_TABLES="${MAX_TABLES:-500}"        # most tables to count rows for
GLOBAL_BUDGET="${GLOBAL_BUDGET:-14400}"  # 4 hours, then stop and say so
STARTED=$(date +%s)
budget_left() { [ $(( $(date +%s) - STARTED )) -lt "$GLOBAL_BUDGET" ]; }
elapsed() { echo $(( $(date +%s) - STARTED )); }

MODE=""          # how to reach the database, decided once below
SERVER_STARTED=0

raw_tbi() {
  local secs="${2:-$TBI_TIMEOUT}"
  printf '%s\n' "$1" > /tmp/q.sql
  timeout -k 10 "$secs" "$TRANSBASE/tbi" -f /tmp/q.sql "$DB" "$DBU" "$PASS" 2>&1
  local rc=$?
  [ "$rc" -eq 124 ] && echo "*** TIMED OUT after ${secs}s ***"
  return 0
}

start_server() {
  [ "$SERVER_STARTED" = "1" ] && return
  "$TRANSBASE/tbserver" > /tmp/tbserver.out 2>&1 &
  SERVER_STARTED=1
  sleep 6
}

echo "############ CONNECTIVITY ############"
echo "hostname: $(hostname)"
echo "--- dblist.ini ---"; cat "$TRANSBASE/dblist.ini" 2>/dev/null | head
echo "--- tbadmin -i ---"; "$TRANSBASE/tbadmin" -i "$DB" 2>&1 | strip | head -25

PROBE="select * from systable;"
bad() { grep -qiE 'error|does not exist|cannot|refused|not reachable|no such' "$1"; }

try_mode() {  # try_mode <label> ; sets MODE on success
  [ -n "$MODE" ] && return 0
  raw_tbi "$PROBE" > /tmp/try.out 2>&1
  if ! bad /tmp/try.out; then
    MODE="$1"
    echo "   connected via: $1"
    return 0
  fi
  echo "   no: $(strip < /tmp/try.out | head -3 | tr '\n' ' ')"
  return 1
}

echo
echo "1. tbi directly"
try_mode direct

if [ -z "$MODE" ]; then
  echo "2. boot the database first"
  "$TRANSBASE/tbadmin" -b "$DB" 2>&1 | strip | head -8
  try_mode booted
fi

if [ -z "$MODE" ]; then
  echo "3. start tbserver, then tbi"
  start_server
  strip < /tmp/tbserver.out | head -10
  try_mode server
fi

if [ -z "$MODE" ]; then
  echo "4. tbserver with the service ports from rc.tbenv"
  TRANSBASE_SERVICENAMES=2024:2025 "$TRANSBASE/tbserver" > /tmp/tbserver2.out 2>&1 &
  sleep 8
  strip < /tmp/tbserver2.out | head -10
  try_mode server-ports
fi

if [ -z "$MODE" ] && [ -x "$TRANSBASE/utbi" ]; then
  echo "5. utbi (local client, no server?)"
  printf '%s\n' "$PROBE" > /tmp/q.sql
  timeout -k 10 "$TBI_TIMEOUT" "$TRANSBASE/utbi" -f /tmp/q.sql "$DB" "$DBU" "$PASS" > /tmp/try.out 2>&1
  if ! bad /tmp/try.out; then
    MODE=utbi
    raw_tbi() { printf '%s\n' "$1" > /tmp/q.sql
                timeout -k 10 "${2:-$TBI_TIMEOUT}" "$TRANSBASE/utbi" -f /tmp/q.sql "$DB" "$DBU" "$PASS" 2>&1; return 0; }
    echo "   connected via: utbi"
  else
    echo "   no: $(strip < /tmp/try.out | head -3 | tr '\n' ' ')"
  fi
fi

echo "CONNECTION MODE: ${MODE:-NONE}"

if [ -z "$MODE" ]; then
  echo "Could not reach the database. Last attempt output:"
  strip < /tmp/try.out 2>/dev/null | head -40
  echo
  echo "--- dblist.ini at this point ---"
  cat "$TRANSBASE/dblist.ini" 2>&1 | head -20
  exit 1
fi

q() {  # q <outfile> <sql>
  if ! budget_left; then echo "  -> SKIPPED $1 (out of time budget)"; return; fi
  echo "  -> $1"
  { echo "-- $2"; raw_tbi "$2" | strip; } > "$OUT/$1" 2>&1
}

echo
echo "############ SYSTEM CATALOG ############"
q systable.txt        "select * from systable;"
q systable_names.txt  "select tname from systable order by tname;"
q syscolumn.txt       "select * from syscolumn;"
q sysindex.txt        "select * from sysindex;"
q sysview.txt         "select * from sysview;"
q sysuser.txt         "select * from sysuser;"
q sysdomain.txt       "select * from sysdomain;"
q sysrefconst.txt     "select * from sysrefconst;"

echo
echo "############ TABLES OF INTEREST (German ETK vocabulary) ############"
for kw in teil bildtaf baureihe typ fahrzeug sa_ sonder motor getriebe \
          lieferant preis text sprache modell serie ausstattung; do
  q "match_${kw}.txt" "select tname from systable where tname like '%${kw}%' order by tname;"
done

echo
echo "############ ROW COUNTS ############"
# Derive the table list from the catalog dump, then count each. Slow under
# emulation, which is why this is an overnight job.
grep -oiE '\b[a-z_][a-z0-9_]{2,}\b' "$OUT/systable_names.txt" 2>/dev/null \
  | grep -viE '^(tname|select|from|order|by|rows|row|null)$' \
  | sort -u > /tmp/tables.txt
TOTAL=$(wc -l < /tmp/tables.txt)
echo "candidate tables: $TOTAL (counting at most $MAX_TABLES, budget ${GLOBAL_BUDGET}s)"
: > "$OUT/rowcounts.txt"
done_n=0
stopped=""
while read -r t; do
  [ -z "$t" ] && continue
  if [ "$done_n" -ge "$MAX_TABLES" ]; then stopped="hit MAX_TABLES=$MAX_TABLES"; break; fi
  if ! budget_left; then stopped="ran out of time budget (${GLOBAL_BUDGET}s)"; break; fi
  n=$(raw_tbi "select count(*) from ${t};" "$COUNT_TIMEOUT" | strip | grep -oE '^[0-9]+$' | tail -1)
  [ -n "$n" ] && echo "${n}	${t}" >> "$OUT/rowcounts.txt"
  done_n=$((done_n + 1))
done < /tmp/tables.txt
sort -rn "$OUT/rowcounts.txt" -o "$OUT/rowcounts.txt" 2>/dev/null || true
echo "row counts collected: $(wc -l < "$OUT/rowcounts.txt") of $TOTAL candidates"
# Never let a cap look like full coverage.
if [ -n "$stopped" ]; then
  echo "*** INCOMPLETE: $stopped -- $((TOTAL - done_n)) tables were not counted ***" \
    | tee "$OUT/INCOMPLETE.txt"
fi
echo "elapsed: $(elapsed)s"

echo
echo "############ DONE ############"
ls -la "$OUT"
