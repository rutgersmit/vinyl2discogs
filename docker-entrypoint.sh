#!/bin/sh
# Make /data writable for the app user, then drop root privileges.
# PUID/PGID let you match the owner of a bind-mounted host folder.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

fail() {
    echo "vinyl2discogs: $1" >&2
    echo "  - SELinux host (Fedora/RHEL): mount the volume as ./data:/data:z" >&2
    echo "  - NFS share with root_squash: on the host, run chown -R $PUID:$PGID data" >&2
    exit 1
}

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/photos /data/inbox 2>/dev/null || fail "cannot create folders in /data, even as root."
    # Only fix ownership when it is wrong, so large photo folders aren't re-chowned on every start.
    if [ "$(stat -c %u:%g /data)" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" /data 2>/dev/null || fail "cannot change the owner of /data to $PUID:$PGID."
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
