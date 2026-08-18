#!/bin/bash
# Keep Transbase's database registry on the /data volume.
#
# tbadmin records an attached database in $TRANSBASE/dblist.ini, but /opt/transbase
# lives in the image layer, so that registration vanishes when a --rm container
# exits -- which is why "tbadmin -i" reported the database missing right after a
# successful attach. Symlinking it onto the persistent volume fixes that.
#
# The container hostname is pinned by etk-db.sh (--hostname), because Transbase
# qualifies databases as <name>@<host> and a random hostname per run would never
# match a previous registration.
set -e
PERSIST=/data/_tbconf
mkdir -p "$PERSIST"
if [ ! -e "$PERSIST/dblist.ini" ]; then
  if [ -e "$TRANSBASE/dblist.ini.orig" ]; then
    cp "$TRANSBASE/dblist.ini.orig" "$PERSIST/dblist.ini"
  else
    : > "$PERSIST/dblist.ini"
  fi
fi
ln -sf "$PERSIST/dblist.ini" "$TRANSBASE/dblist.ini"
exec "$@"
