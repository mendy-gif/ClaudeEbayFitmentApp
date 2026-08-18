#!/bin/bash
# Dump the ETK schema. Runs INSIDE the container. Safe unattended: every query is
# bounded and one failure never stops the rest.
#
# Two hard-won details:
#   * utbi options take NO space before their value: -c400, not -c 400. With a
#     space, utbi reads the number as the DATABASE NAME and reports
#     "database <400@etkdb> does not exist".
#   * Both clients are network clients, so tbmux (kernel + server) must already be
#     running in THIS container -- see start_transbase.sh.
set -u
DB="${DB:-etk_publ}"
DBU="${DBUSER:-tbadmin}"
PASS="${DBPASS:-altabe}"
OUT=/out
mkdir -p "$OUT/columns"
U="$TRANSBASE/utbi"

TBI_TIMEOUT="${TBI_TIMEOUT:-120}"
COUNT_TIMEOUT="${COUNT_TIMEOUT:-240}"
MAX_TABLES="${MAX_TABLES:-1000}"
GLOBAL_BUDGET="${GLOBAL_BUDGET:-14400}"
STARTED=$(date +%s)
budget_left() { [ $(( $(date +%s) - STARTED )) -lt "$GLOBAL_BUDGET" ]; }
elapsed() { echo $(( $(date +%s) - STARTED )); }
noise() { grep -vE '^Current Locale:|^ *$'; }

sql() {  # sql "<statement>" [timeout]
  timeout -k 10 "${2:-$TBI_TIMEOUT}" "$U" -c400 -w60 "$DB" "$DBU" "$PASS" "$1" 2>&1
  return 0
}
interactive() {  # interactive <<commands
  timeout -k 10 "${1:-$TBI_TIMEOUT}" "$U" -c400 -w60 "$DB" "$DBU" "$PASS" 2>&1
}
failed() { grep -qiE 'Transbase Error Code|error report|does not exist|not reachable|^Usage:|UTBI Usage' "$1"; }

echo "############ 1. START THE ENGINE ############"
bash /start_transbase.sh 2>&1 | sed 's/^/  /'

echo
echo "############ 2. PROVE WE CAN QUERY ############"
sql "select 1;" > /tmp/v.out 2>&1
noise < /tmp/v.out | head -10
if failed /tmp/v.out; then
  echo "*** FATAL: cannot query. Stopping rather than writing files that look like results. ***"
  exit 1
fi
echo ">>> queries work"

echo
echo "############ 3. FULL TABLE LIST ############"
printf 'set columns 400\nset width 80\ndesc\nquit\n' | interactive 300 > "$OUT/tables_raw.txt" 2>&1
noise < "$OUT/tables_raw.txt" \
  | awk -F'|' 'NF>=3 {gsub(/^[ \t]+|[ \t]+$/,"",$3); if ($3 != "" && $3 != "tname" && $3 !~ /^=+$/) print $3}' \
  | sort -u > "$OUT/tables.txt"
TOTAL=$(wc -l < "$OUT/tables.txt")
echo "tables found: $TOTAL"
head -40 "$OUT/tables.txt"
[ "$TOTAL" -eq 0 ] && { echo "*** FATAL: no tables parsed ***"; head -30 "$OUT/tables_raw.txt"; exit 1; }

echo
echo "############ 4. ROW COUNTS ############"
: > "$OUT/rowcounts.txt"
n=0; stopped=""
while read -r t; do
  [ -z "$t" ] && continue
  if [ "$n" -ge "$MAX_TABLES" ]; then stopped="hit MAX_TABLES=$MAX_TABLES"; break; fi
  budget_left || { stopped="ran out of time budget (${GLOBAL_BUDGET}s)"; break; }
  c=$(sql "select count(*) from ${t};" "$COUNT_TIMEOUT" | noise \
        | grep -oE '^[[:space:]]*[0-9]+[[:space:]]*$' | tr -d ' ' | head -1)
  [ -n "$c" ] && printf '%s\t%s\n' "$c" "$t" >> "$OUT/rowcounts.txt"
  n=$((n+1))
  [ $((n % 25)) -eq 0 ] && echo "  ...$n/$TOTAL counted ($(elapsed)s)"
done < "$OUT/tables.txt"
sort -rn "$OUT/rowcounts.txt" -o "$OUT/rowcounts.txt" 2>/dev/null || true
echo "counted $(wc -l < "$OUT/rowcounts.txt") of $TOTAL tables in $(elapsed)s"
[ -n "$stopped" ] && echo "*** INCOMPLETE: $stopped -- $((TOTAL - n)) tables not counted ***" \
  | tee "$OUT/INCOMPLETE.txt"

echo
echo "--- 40 biggest tables ---"
head -40 "$OUT/rowcounts.txt"

echo
echo "############ 5. COLUMNS OF EVERY TABLE ############"
n=0
while read -r t; do
  [ -z "$t" ] && continue
  budget_left || { echo "*** stopped describing at $n tables (time budget) ***"; break; }
  printf 'set columns 400\nset width 60\ndesc %s\nquit\n' "$t" \
    | interactive 60 | noise > "$OUT/columns/${t}.txt" 2>&1
  if failed "$OUT/columns/${t}.txt"; then mv "$OUT/columns/${t}.txt" "$OUT/columns/${t}.error"; fi
  n=$((n+1))
  [ $((n % 50)) -eq 0 ] && echo "  ...$n/$TOTAL described ($(elapsed)s)"
done < "$OUT/tables.txt"
echo "described $n tables"

echo
echo "############ 6. TABLES MATCHING THE ETK VOCABULARY ############"
for kw in teil bildtaf baureihe typ fahrzeug sa lieferant preis motor getriebe \
          text sprache modell serie ausstattung zeile vin fgnr datum; do
  m=$(grep -i -- "$kw" "$OUT/tables.txt" | tr '\n' ' ')
  [ -n "$m" ] && printf '%-14s %s\n' "$kw:" "$m"
done | tee "$OUT/vocabulary_matches.txt"

echo
echo "############ DONE in $(elapsed)s ############"
ls -la "$OUT" | head -20
