#!/bin/bash
# Run a SQL script against the attached ETK database. Runs INSIDE the container.
# Tries tbi directly first; if that needs a running server, starts tbserver and
# retries, since it is not yet established which mode this build requires.
set -u
DB="${DB:-etk_publ}"
DBU="${DBUSER:-tbadmin}"
PASS="${DBPASS:-altabe}"
SQL="${1:-/tmp/q.sql}"
strip() { grep -vE '^\s*$|^@\(#\)|^ *Version:|^ *License:|U\.S\.-Patent|Copyright \(c\)'; }

echo "=== hostname: $(hostname) ==="
echo "=== registered databases (dblist.ini) ==="
cat "$TRANSBASE/dblist.ini" 2>/dev/null | head -20
echo
echo "=== tbadmin -i $DB ==="
"$TRANSBASE/tbadmin" -i "$DB" 2>&1 | strip | head -25
echo
echo "=== the query ==="
cat "$SQL"
echo
echo "=== attempt 1: tbi directly ==="
"$TRANSBASE/tbi" -f "$SQL" "$DB" "$DBU" "$PASS" > /tmp/tbi1.out 2>&1
rc=$?
echo "--- exit $rc ---"
strip < /tmp/tbi1.out | head -80
[ "$rc" -eq 0 ] && exit 0

echo
echo "=== attempt 2: boot the database, then tbi ==="
"$TRANSBASE/tbadmin" -b "$DB" 2>&1 | strip | head -10
"$TRANSBASE/tbi" -f "$SQL" "$DB" "$DBU" "$PASS" > /tmp/tbi2.out 2>&1
rc=$?
echo "--- exit $rc ---"
strip < /tmp/tbi2.out | head -80
[ "$rc" -eq 0 ] && exit 0

echo
echo "=== attempt 3: start tbserver, then tbi ==="
"$TRANSBASE/tbserver" > /tmp/tbserver.out 2>&1 &
sleep 6
echo "--- tbserver said ---"
strip < /tmp/tbserver.out | head -15
"$TRANSBASE/tbi" -f "$SQL" "$DB" "$DBU" "$PASS" > /tmp/tbi3.out 2>&1
rc=$?
echo "--- exit $rc ---"
strip < /tmp/tbi3.out | head -80
exit $rc
