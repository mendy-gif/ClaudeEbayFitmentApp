#!/bin/bash
# Find the ETK system catalogue and prove we can read real rows.
# Runs INSIDE the container. Prints RAW output -- no filtering, no interpretation.
#
# utbi is a LOCAL client (no tbserver needed) and its SQL is a POSITIONAL argument:
#     utbi [options] [ dbname [ uname [ passwd [ SQL command ] ] ] ]
# There is no -f option; passing one makes it print usage, which an earlier version
# of this project mistook for success.
set -u
DB="${DB:-etk_publ}"
DBU="${DBUSER:-tbadmin}"
PASS="${DBPASS:-altabe}"
U="$TRANSBASE/utbi"

# -c: line width (default 80 is too narrow)  -w: column width (default 10)
run() {
  echo "════════════════════════════════════════════════════════════"
  echo "SQL: $1"
  echo "────────────────────────────────────────────────────────────"
  timeout -k 10 90 "$U" -c 400 -w 40 "$DB" "$DBU" "$PASS" "$1" 2>&1 | head -"${2:-30}"
  echo
}

echo "########## CAN WE READ ANYTHING AT ALL? ##########"
run "select 1;"

echo "########## WHICH CATALOGUE TABLE NAME IS RIGHT? ##########"
run "select * from systable;"          15
run "select * from systables;"         15
run "select * from systable;"          15
run "select * from tables;"            15
run "select * from syscatalog;"        15
run "select * from sys.systable;"      15

echo "########## TRANSBASE-SPECIFIC SPELLINGS ##########"
run "select tname from systable;"      15
run "select * from systable where 1=0;" 10

echo "########## WHAT DOES THE ERROR ACTUALLY SAY? ##########"
run "select * from definitely_not_a_table;" 15

echo "########## utbi HELP COMMANDS (in case a dot-command lists tables) ##########"
printf 'help\nquit\n' | timeout -k 5 30 "$U" "$DB" "$DBU" "$PASS" 2>&1 | head -40
