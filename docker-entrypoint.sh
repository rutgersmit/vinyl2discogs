#!/bin/sh
# Make /data writable for the app user, then drop root privileges.
# PUID/PGID let you match the owner of a bind-mounted host folder.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/photos /data/inbox
    # Only fix ownership when it is wrong, so large photo folders aren't re-chowned on every start.
    if [ "$(stat -c %u:%g /data)" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" /data
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
