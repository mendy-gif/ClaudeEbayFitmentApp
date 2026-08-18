#!/bin/bash
# Put the whole Transbase installation on the /data volume, not just a symlink.
#
# Why: tbadmin registers an attached database in $TRANSBASE/dblist.ini, but
# /opt/transbase is an image layer, so that registration is lost when a --rm
# container exits. Symlinking dblist.ini onto the volume does NOT work either --
# tbadmin rewrites the file (write temp + rename), which replaces the symlink with
# a regular file inside the image layer and leaves the persistent copy empty. That
# is exactly what happened: the attach reported success while dblist.ini still read
# "[databases]" with no entries.
#
# Copying the ~10 MB installation onto the volume once makes every piece of
# Transbase state persistent, however it chooses to write it.
set -e
IMAGE_TB=/opt/transbase
PERSIST_TB=/data/transbase

if [ ! -x "$PERSIST_TB/tbadmin" ]; then
  mkdir -p "$PERSIST_TB"
  cp -a "$IMAGE_TB/." "$PERSIST_TB/"
fi

export TRANSBASE="$PERSIST_TB"
export PATH="$PERSIST_TB:$PATH"
exec "$@"
