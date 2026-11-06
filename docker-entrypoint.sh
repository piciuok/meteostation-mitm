#!/bin/sh
# Start as root only to fix ownership of the data volume, then drop to an unprivileged
# user. The only capability kept is CAP_NET_BIND_SERVICE (HTTP on port 80).
set -eu
DATA_DIR="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    # Volumes created by older (root) versions are handed over to the app user.
    chown -R app:app "$DATA_DIR" || echo "entrypoint: cannot chown $DATA_DIR, it must be writable by uid 10001" >&2
    # Narrow the bounding set too when allowed (needs CAP_SETPCAP, absent with cap_drop: ALL).
    if setpriv --bounding-set=-all,+net_bind_service true 2>/dev/null; then
        exec setpriv --reuid=app --regid=app --init-groups \
            --inh-caps=-all,+net_bind_service --ambient-caps=-all,+net_bind_service \
            --bounding-set=-all,+net_bind_service -- "$@"
    fi
    exec setpriv --reuid=app --regid=app --init-groups \
        --inh-caps=-all,+net_bind_service --ambient-caps=-all,+net_bind_service -- "$@"
fi
exec "$@"
