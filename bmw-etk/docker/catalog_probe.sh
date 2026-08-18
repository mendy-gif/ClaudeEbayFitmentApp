#!/bin/bash
# Start Transbase properly, then find the system catalogue.
# Runs INSIDE the container. Prints RAW output -- no filtering, no interpretation.
#
# Both tbi and utbi are NETWORK clients: they connect to service 2024 and fail with
# "server <2024> at <etkdb> not reachable" when nothing is listening. There is no
# local/direct client. TRANSBASE_SERVICENAMES=2024:2025 is a PAIR -- tbserver takes
# 2025, and tbkernel serves clients on 2024 -- so BOTH processes must run, exactly as
# BMW's rc.TransBase does. The server must live in the SAME container as the query,
# since each `docker run` is a fresh container.
set -u
DB="${DB:-etk_publ}"
DBU="${DBUSER:-tbadmin}"
PASS="${DBPASS:-altabe}"
U="$TRANSBASE/utbi"

echo "########## 1. START TRANSBASE (boot + kernel + server) ##########"
bash /start_transbase.sh 2>&1 | sed 's/^/  /'

echo
echo "########## 2. IS ANYTHING LISTENING ON 2024 / 2025? ##########"
(ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null || cat /proc/net/tcp) | head -20
echo "--- processes ---"
ps ax 2>/dev/null | grep -iE 'tbserver|tbkernel|tbmux|tbdiag' | grep -v grep

run() {
  echo "════════════════════════════════════════════════════════════"
  echo "SQL: $1"
  echo "────────────────────────────────────────────────────────────"
  timeout -k 10 90 "$U" -c 400 -w 40 "$DB" "$DBU" "$PASS" "$1" 2>&1 | head -"${2:-30}"
  echo
}

echo
echo "########## 3. CAN WE READ ANYTHING AT ALL? ##########"
run "select 1;"

echo "########## 4. WHICH CATALOGUE TABLE NAME IS RIGHT? ##########"
run "select * from systable;"       15
run "select * from systables;"      15
run "select * from tables;"         15
run "select * from syscatalog;"     15
run "select tname from systable;"   15

echo "########## 5. WHAT DOES A REAL 'no such table' ERROR LOOK LIKE? ##########"
run "select * from definitely_not_a_table;" 15

echo "########## 6. utbi's OWN describe COMMAND ##########"
printf 'desc\nquit\n' | timeout -k 5 60 "$U" "$DB" "$DBU" "$PASS" 2>&1 | head -40
