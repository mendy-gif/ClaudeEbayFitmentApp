#!/bin/bash
# Start Transbase the way BMW's own rc.TransBase does. Runs INSIDE the container.
#
# The disc's init script is the authority here:
#     FLAG=-bfnv ; $ADM $FLAG            # boot all databases (force, no interaction)
#     sleep 1
#     if [ -f $TRANSBASE/tbmux ]; then
#         nohup $TRANSBASE/tbmux -tbk $PROG -tbs $SERV &
#     else
#         nohup $SERV -v &               # tbserver -- the network listener
#         nohup $PROG -v &               # tbkernel -- does the actual work
#     fi
#
# Starting tbserver alone is why connections were refused: it listens, but with no
# kernel behind it there is nothing to serve.
set -u
strip() { grep -vE '^\s*$|^@\(#\)|^ *Version:|^ *License:|U\.S\.-Patent|Copyright \(c\)'; }

: "${TRANSBASE_SERVICENAMES:=2024:2025}"
export TRANSBASE_SERVICENAMES
cd "$TRANSBASE" || exit 1

echo "--- what this build ships (looking for tbmux / tbkernel) ---"
ls "$TRANSBASE" | tr '\n' ' '; echo; echo

echo "--- booting databases: tbadmin -bfnv ---"
timeout -k 10 300 "$TRANSBASE/tbadmin" -bfnv 2>&1 | strip | head -20
sleep 2

# Mirror rc.TransBase's choice of kernel binary.
if   [ -f "$TRANSBASE/mykernel" ]; then PROG="$TRANSBASE/mykernel"
elif [ -f "$TRANSBASE/tbkernel" ]; then PROG="$TRANSBASE/tbkernel"
elif [ -f "$TRANSBASE/tbdiag"   ]; then PROG="$TRANSBASE/tbdiag"
else PROG="$TRANSBASE/mydiag"; fi
SERV="$TRANSBASE/tbserver"
echo "--- kernel: $PROG"
echo "--- server: $SERV"

if [ -f "$TRANSBASE/tbmux" ]; then
  echo "--- starting tbmux (multiplexer: kernel + server together) ---"
  nohup "$TRANSBASE/tbmux" -tbk "$PROG" -tbs "$SERV" > /tmp/tbmux.out 2>&1 &
else
  echo "--- starting tbserver and the kernel separately ---"
  nohup "$SERV" -v > /tmp/tbserver.out 2>&1 &
  [ -x "$PROG" ] && nohup "$PROG" -v > /tmp/tbkernel.out 2>&1 &
fi

sleep 8
echo "--- processes now running ---"
ps ax 2>/dev/null | grep -iE 'tbserver|tbkernel|tbmux|tbdiag' | grep -v grep | head -10
echo "--- listening ports ---"
(ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null || echo "(no ss/netstat)") | head -10
echo "--- startup output ---"
for f in /tmp/tbmux.out /tmp/tbserver.out /tmp/tbkernel.out; do
  [ -s "$f" ] && { echo "  [$f]"; strip < "$f" | head -12; }
done
