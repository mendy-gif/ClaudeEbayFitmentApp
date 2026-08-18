#!/bin/bash
# What does this Transbase build expect? Runs INSIDE the container.
# Read-only: prints usage text and state, changes nothing.
set -u
DB="${DB:-etk_publ}"
strip() { grep -vE '^\s*$|^@\(#\)|^ *Version:|^ *License:|U\.S\.-Patent|Copyright \(c\)'; }

echo "############ WHERE TRANSBASE LIVES ############"
echo "TRANSBASE=$TRANSBASE"
ls -la "$TRANSBASE" | head -30
echo
echo "############ dblist.ini (is the database registered?) ############"
cat "$TRANSBASE/dblist.ini" 2>&1
echo
echo "############ tbadmin -i $DB ############"
"$TRANSBASE/tbadmin" -i "$DB" 2>&1 | strip | head -30
echo
echo "############ tbi USAGE (how does it address a database?) ############"
"$TRANSBASE/tbi" --help 2>&1 | strip | head -40
echo "--- tbi with no arguments ---"
"$TRANSBASE/tbi" 2>&1 | strip | head -40
echo
echo "############ tbserver USAGE (how is the server started?) ############"
"$TRANSBASE/tbserver" --help 2>&1 | strip | head -40
echo "--- tbserver with no arguments ---"
timeout -k 5 20 "$TRANSBASE/tbserver" 2>&1 | strip | head -40
echo
echo "############ utbi (a local/direct client?) ############"
"$TRANSBASE/utbi" --help 2>&1 | strip | head -25
echo "--- utbi with no arguments ---"
"$TRANSBASE/utbi" 2>&1 | strip | head -25
echo
echo "############ OTHER TOOLS SHIPPED ############"
ls "$TRANSBASE" | tr '\n' ' '
echo
echo
echo "############ ENVIRONMENT ############"
env | grep -i transbase
echo
echo "############ LISTENING PORTS ############"
(ss -lntp 2>/dev/null || netstat -lnt 2>/dev/null || echo "(no ss/netstat)") | head -20
