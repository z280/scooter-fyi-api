#!/bin/sh
# supercronic launcher with crontab live-reload.
#
# Reads from /app/state/crontab (the admin-editable copy). On first boot,
# seeds that file from the baked-in default at /app/crontab. Then polls
# its mtime every 15 seconds and re-execs supercronic when it changes —
# admin-panel edits take effect within ~15s without a container restart.
#
# tini is PID 1 (via the Dockerfile ENTRYPOINT) and forwards SIGTERM /
# SIGINT to this script; the TRAP below propagates that to supercronic.

set -e

CRONTAB="${CRONTAB:-/app/state/crontab}"
DEFAULT="${CRONTAB_DEFAULT:-/app/crontab}"
POLL_INTERVAL="${CRONTAB_POLL_SECONDS:-15}"

mkdir -p "$(dirname "$CRONTAB")"
if [ ! -f "$CRONTAB" ]; then
    echo "[run-scheduler] seeding $CRONTAB from $DEFAULT"
    cp "$DEFAULT" "$CRONTAB"
fi

run_supercronic() {
    supercronic -passthrough-logs "$CRONTAB" &
    echo $!
}

mtime() {
    stat -c %Y "$CRONTAB" 2>/dev/null || echo 0
}

last_mtime=$(mtime)
pid=$(run_supercronic)
echo "[run-scheduler] supercronic started (pid=$pid), watching $CRONTAB"

# Forward signals from tini → supercronic so docker compose down is clean
trap 'echo "[run-scheduler] caught signal, stopping supercronic"; kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; exit 0' TERM INT

while sleep "$POLL_INTERVAL"; do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "[run-scheduler] supercronic exited unexpectedly; letting tini restart us"
        exit 1
    fi
    current_mtime=$(mtime)
    if [ "$current_mtime" != "$last_mtime" ]; then
        echo "[run-scheduler] crontab mtime changed ($last_mtime -> $current_mtime); reloading supercronic"
        kill -TERM "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        last_mtime="$current_mtime"
        pid=$(run_supercronic)
        echo "[run-scheduler] supercronic restarted (pid=$pid)"
    fi
done
