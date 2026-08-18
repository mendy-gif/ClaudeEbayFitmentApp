#!/bin/bash
# Attach the BMW ETK CD-ROM database, trying each plausible invocation in turn.
# Runs INSIDE the container (see etk-db.sh create).
#
# Background: BMW's postinstallDataDB.cmd passes only rfile000.000, rfile000.001
# and rfile001.000 -- but this data package also ships rfile000.002, a third
# segment of volume 000. tbadmin's -C usage also offers r=<dir> (whole romfile
# directory) and -F (no interaction at all) alongside -f (interact for CD-insert).
set -u

DB="${DB:-etk_publ}"
PASS="${DBPASS:-altabe}"
HOMEDIR="/data/$DB"
ROMDIR=/rom
[ -d /rom/files ] && ROMDIR=/rom/files

echo "ROM directory: $ROMDIR"
ls -la "$ROMDIR"
echo

attempt() {
  desc="$1"; shift
  echo "============================================================"
  echo "ATTEMPT: $desc"
  echo "============================================================"
  rm -rf "$HOMEDIR"
  echo "+ tbadmin $*"
  "$TRANSBASE/tbadmin" "$@" > /tmp/attach.out 2>&1
  rc=$?
  grep -vE '^\s*$|^@\(#\)|Version:|License:|U\.S\.-Patent|Copyright' /tmp/attach.out | tail -20
  echo "--- exit $rc ---"
  if [ "$rc" -eq 0 ]; then
    echo
    echo "*************************************************************"
    echo "*** SUCCESS: $desc"
    echo "*************************************************************"
    return 0
  fi
  echo
  return 1
}

COMMON="h=$HOMEDIR cp=utf8 p=$PASS"

# 1. Exactly what BMW's installer does (already known to fail; kept as control).
attempt "BMW's three romfiles, -Cf" \
  -Cf "$DB" $COMMON \
  rf=$ROMDIR/rfile000.000 rf=$ROMDIR/rfile000.001 rf=$ROMDIR/rfile001.000 && exit 0

# 2. Same, but including the fourth segment BMW's script omits.
attempt "all four romfiles, -Cf" \
  -Cf "$DB" $COMMON \
  rf=$ROMDIR/rfile000.000 rf=$ROMDIR/rfile000.001 rf=$ROMDIR/rfile000.002 \
  rf=$ROMDIR/rfile001.000 && exit 0

# 3. All four with no interaction at all, in case it is waiting on a CD prompt.
attempt "all four romfiles, -CF (no interaction)" \
  -CF "$DB" $COMMON \
  rf=$ROMDIR/rfile000.000 rf=$ROMDIR/rfile000.001 rf=$ROMDIR/rfile000.002 \
  rf=$ROMDIR/rfile001.000 && exit 0

# 4. Let tbadmin discover the romfiles from the directory itself.
attempt "romfile directory r=, -CF" -CF "$DB" $COMMON r=$ROMDIR && exit 0

# 5. Directory form, but allowing the CD-insert interaction.
attempt "romfile directory r=, -Cf" -Cf "$DB" $COMMON r=$ROMDIR && exit 0

# 6. Three romfiles with no interaction (isolates -f vs -F from the file count).
attempt "BMW's three romfiles, -CF" \
  -CF "$DB" $COMMON \
  rf=$ROMDIR/rfile000.000 rf=$ROMDIR/rfile000.001 rf=$ROMDIR/rfile001.000 && exit 0

echo "============================================================"
echo "All attempts failed. Full output of the LAST attempt:"
echo "============================================================"
cat /tmp/attach.out
exit 1
