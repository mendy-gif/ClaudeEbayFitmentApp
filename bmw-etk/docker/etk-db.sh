#!/usr/bin/env bash
# Build and drive the Transbase container holding the BMW ETK catalog.
#
#   bash bmw-etk/docker/etk-db.sh build     # build the image from the ISO's tarball
#   bash bmw-etk/docker/etk-db.sh probe     # what the binaries are, do they run
#   bash bmw-etk/docker/etk-db.sh create    # attach the ROM files as database etk_publ
#   bash bmw-etk/docker/etk-db.sh sql "select ..."   # run one SQL statement
#   bash bmw-etk/docker/etk-db.sh shell     # interactive shell inside the container
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ISO="${ETK_ISO:-/Volumes/BMW ETK 2020-01}"
ROM="$ROOT/dump/rfiles"
IMAGE="etk-transbase"
VOLUME="etk-data"
PLATFORM="linux/amd64"
DB="etk_publ"
DBUSER="tbadmin"
DBPASS="altabe"

die() { echo "ERROR: $*" >&2; exit 1; }
have_docker() {
  if ! command -v docker >/dev/null 2>&1; then
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
  if ! docker info >/dev/null 2>&1; then
    cat >&2 <<'MSG'
ERROR: docker is installed but the engine is not running.

  Open Docker Desktop from Applications and wait for the whale icon in the
  menu bar to settle, then run this again.
MSG
    exit 1
  fi
}

run_in() {  # run a command in a throwaway container with rom + data mounted
  docker run --rm --platform "$PLATFORM" \
    -v "$ROM":/rom:ro -v "$VOLUME":/data \
    "$IMAGE" "$@"
}

case "${1:-}" in
build)
  have_docker
  [ -d "$ISO" ] || die "ISO not mounted at: $ISO  (set ETK_ISO to override)"
  cp "$ISO/transbase_linux/transbase_linux.tar.gz" "$HERE/" || die "could not copy the Transbase tarball"
  echo "Building $IMAGE for $PLATFORM (emulated on Apple Silicon)..."
  docker build --platform "$PLATFORM" -t "$IMAGE" "$HERE" || die "build failed"
  rm -f "$HERE/transbase_linux.tar.gz"
  docker volume create "$VOLUME" >/dev/null
  echo "Built. Next: bash $0 probe"
  ;;

probe)
  have_docker
  echo "=== binary architecture ==="
  run_in bash -lc 'file $TRANSBASE/tbadmin $TRANSBASE/tbi $TRANSBASE/tbserver 2>&1'
  echo
  echo "=== do they actually run? (a usage message here is SUCCESS) ==="
  run_in bash -lc '$TRANSBASE/tbadmin 2>&1 | head -20; echo "--- exit $? ---"'
  echo
  echo "=== ROM files visible in the container ==="
  run_in bash -lc 'ls -la /rom/files 2>/dev/null || ls -la /rom'
  ;;

create)
  have_docker
  echo "Attaching the ROM files as database '$DB' (this is the real test)..."
  run_in bash -lc '
    set -x
    ROMDIR=/rom
    [ -d /rom/files ] && ROMDIR=/rom/files
    mkdir -p /data/'"$DB"'
    $TRANSBASE/tbadmin -Cf '"$DB"' h=/data/'"$DB"' cp=utf8 p='"$DBPASS"' \
      rf=$ROMDIR/rfile000.000 rf=$ROMDIR/rfile000.001 rf=$ROMDIR/rfile001.000
    echo "--- tbadmin exit $? ---"
    ls -la /data/'"$DB"'
  '
  ;;

sql)
  have_docker
  [ -n "${2:-}" ] || die "usage: $0 sql \"select * from ...\""
  printf '%s\n' "$2" > /tmp/etk_query.sql
  docker run --rm --platform "$PLATFORM" \
    -v "$ROM":/rom:ro -v "$VOLUME":/data -v /tmp/etk_query.sql:/tmp/q.sql:ro \
    "$IMAGE" bash -lc "\$TRANSBASE/tbi -f /tmp/q.sql $DB $DBUSER $DBPASS 2>&1"
  ;;

shell)
  have_docker
  docker run --rm -it --platform "$PLATFORM" \
    -v "$ROM":/rom:ro -v "$VOLUME":/data "$IMAGE" bash
  ;;

*)
  sed -n '2,10p' "$0"
  ;;
esac
