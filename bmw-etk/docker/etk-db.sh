#!/usr/bin/env bash
# Build and drive the Transbase container holding the BMW ETK catalog.
#
#   bash bmw-etk/docker/etk-db.sh build     # build the image from the ISO's tarball
#   bash bmw-etk/docker/etk-db.sh probe     # what the binaries are, do they run
#   bash bmw-etk/docker/etk-db.sh params C  # tbadmin's own docs for an option
#   bash bmw-etk/docker/etk-db.sh create    # attach the ROM files as database etk_publ
#   bash bmw-etk/docker/etk-db.sh sql "select ..."   # run one SQL statement
#   bash bmw-etk/docker/etk-db.sh shell     # interactive shell inside the container
#
# If the amd64 + i386-multiarch build will not run the binaries, retry natively
# 32-bit:  ETK_PLATFORM=linux/386 bash bmw-etk/docker/etk-db.sh build
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ISO="${ETK_ISO:-/Volumes/BMW ETK 2020-01}"
ROM="$ROOT/dump/rfiles"
IMAGE="etk-transbase"
VOLUME="etk-data"
PLATFORM="${ETK_PLATFORM:-linux/amd64}"
# The Transbase binaries are 32-bit i386. On linux/amd64 we add i386 multiarch
# libraries; on linux/386 the whole base image is already 32-bit.
case "$PLATFORM" in
  linux/386) BASE_IMAGE="i386/debian:bullseye-slim" ;;
  *)         BASE_IMAGE="debian:bullseye-slim" ;;
esac
DB="etk_publ"
DBUSER="tbadmin"
DBPASS="altabe"

die() { echo "ERROR: $*" >&2; exit 1; }

# Docker Desktop does not always leave `docker` on the PATH of an already-open
# terminal, so look in the places it actually installs the CLI.
DOCKER=""
find_docker() {
  local c
  for c in "$(command -v docker 2>/dev/null)" \
           "$HOME/.docker/bin/docker" \
           /usr/local/bin/docker \
           /opt/homebrew/bin/docker \
           /Applications/Docker.app/Contents/Resources/bin/docker; do
    [ -n "$c" ] && [ -x "$c" ] && { DOCKER="$c"; return 0; }
  done
  return 1
}
have_docker() {
  if ! find_docker; then
    cat >&2 <<'MSG'
ERROR: docker is not installed.

  1. Download Docker Desktop for "Mac with Apple chip":
       https://www.docker.com/products/docker-desktop/
  2. Open the .dmg and drag Docker into Applications.
  3. Launch Docker from Applications and accept the prompts.
  4. Docker Desktop -> Settings -> General -> tick
       "Use Rosetta for x86_64/amd64 emulation"
     (the Transbase binaries are Intel-only; this makes them fast)
  5. Wait for the whale icon in the menu bar to stop animating, then re-run.
MSG
    exit 1
  fi
  if ! "$DOCKER" info >/dev/null 2>&1; then
    cat >&2 <<'MSG'
ERROR: docker is installed but the engine is not running.

  Open Docker Desktop from Applications and wait for the whale icon in the
  menu bar to settle (it animates while starting), then run this again.
MSG
    exit 1
  fi
  [ "$DOCKER" = "$(command -v docker 2>/dev/null)" ] || echo "(using docker at: $DOCKER)"
}

run_in() {  # run a command in a throwaway container with rom + data mounted
  "$DOCKER" run --rm --platform "$PLATFORM" \
    -v "$ROM":/rom:ro -v "$VOLUME":/data \
    "$IMAGE" "$@"
}

case "${1:-}" in
build)
  have_docker
  [ -d "$ISO" ] || die "ISO not mounted at: $ISO  (set ETK_ISO to override)"
  cp "$ISO/transbase_linux/transbase_linux.tar.gz" "$HERE/" || die "could not copy the Transbase tarball"
  echo "Building $IMAGE for $PLATFORM from $BASE_IMAGE (emulated on Apple Silicon)..."
  "$DOCKER" build --platform "$PLATFORM" --build-arg BASE_IMAGE="$BASE_IMAGE" \
    -t "$IMAGE" "$HERE" || die "build failed"
  rm -f "$HERE/transbase_linux.tar.gz"
  "$DOCKER" volume create "$VOLUME" >/dev/null
  echo "Built. Next: bash $0 probe"
  ;;

probe)
  have_docker
  echo "=== binary architecture ==="
  run_in bash -lc 'file $TRANSBASE/tbadmin $TRANSBASE/tbi $TRANSBASE/tbserver 2>&1'
  echo
  echo "=== 32-bit loader present? (needed: /lib/ld-linux.so.2) ==="
  run_in bash -lc 'ls -la /lib/ld-linux.so.2 2>&1; echo "container arch: $(dpkg --print-architecture)"'
  echo
  echo "=== libraries the binaries need (any \"not found\" is the problem) ==="
  run_in bash -lc 'ldd $TRANSBASE/tbadmin 2>&1 | head -20'
  echo
  echo "=== do they actually run? (a usage message here is SUCCESS) ==="
  run_in bash -lc '$TRANSBASE/tbadmin > /tmp/o 2>&1; echo "--- exit $? ---"; head -25 /tmp/o'
  echo
  echo "=== ROM files visible in the container ==="
  run_in bash -lc 'ls -la /rom/files 2>/dev/null || ls -la /rom'
  ;;

params)
  have_docker
  opt="${2:-C}"
  run_in bash -lc "\$TRANSBASE/tbadmin params $opt 2>&1"
  ;;

create)
  have_docker
  echo "=== the exact syntax tbadmin expects for -C (attach to CD-ROM database) ==="
  run_in bash -lc '$TRANSBASE/tbadmin params C 2>&1'
  echo
  echo "=== attaching the ROM files as database '"'"'$DB'"'"' ==="
  run_in bash -lc '
    ROMDIR=/rom
    [ -d /rom/files ] && ROMDIR=/rom/files
    echo "ROM directory: $ROMDIR"
    ls -la "$ROMDIR"
    mkdir -p /data/'"$DB"'
    set -x
    $TRANSBASE/tbadmin -Cf '"$DB"' h=/data/'"$DB"' cp=utf8 p='"$DBPASS"' \
      rf=$ROMDIR/rfile000.000 rf=$ROMDIR/rfile000.001 rf=$ROMDIR/rfile001.000
    rc=$?
    set +x
    echo "--- tbadmin exit $rc ---"
    echo
    echo "=== what landed in /data ==="
    ls -laR /data | head -40
    du -sh /data
    echo
    echo "=== does Transbase now know about the database? ==="
    $TRANSBASE/tbadmin -i '"$DB"' 2>&1 | head -30
  '
  ;;

sql)
  have_docker
  [ -n "${2:-}" ] || die "usage: $0 sql \"select * from ...\""
  printf '%s\n' "$2" > /tmp/etk_query.sql
  "$DOCKER" run --rm --platform "$PLATFORM" \
    -v "$ROM":/rom:ro -v "$VOLUME":/data -v /tmp/etk_query.sql:/tmp/q.sql:ro \
    "$IMAGE" bash -lc "\$TRANSBASE/tbi -f /tmp/q.sql $DB $DBUSER $DBPASS 2>&1"
  ;;

shell)
  have_docker
  "$DOCKER" run --rm -it --platform "$PLATFORM" \
    -v "$ROM":/rom:ro -v "$VOLUME":/data "$IMAGE" bash
  ;;

*)
  sed -n '2,10p' "$0"
  ;;
esac
