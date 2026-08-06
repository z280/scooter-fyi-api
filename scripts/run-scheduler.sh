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

# Sets $pid rather than echoing it. THIS IS LOAD-BEARING, not style.
#
# The caller used to capture it with command substitution, and that hangs
# forever: substitution reads the child's stdout until EOF, supercronic is
# backgrounded but inherits that same pipe and never closes it, so the
# substitution never returns. The script sat blocked one line ABOVE the watch
# loop for the life of the container.
#
# It looked like it worked because supercronic logs to STDERR, which bypasses
# the substitution and reaches `docker logs` normally: cron jobs ran and their
# output appeared. The only symptoms were negative -- the reload never fired
# and not one `[run-scheduler]` line was ever printed. Confirmed on the live
# container: zero occurrences of "run-scheduler" across 6h of logs, and no
# `sleep` child of the script's PID in repeated sampling.
#
# Consequence: /app/state/crontab was read exactly once per container start,
# so /admin/scheduler/edit wrote a file nothing ever re-read and every crontab
# change silently required a restart to take effect.
run_supercronic() {
    supercronic -passthrough-logs "$CRONTAB" &
    pid=$!
}

mtime() {
    stat -c %Y "$CRONTAB" 2>/dev/null || echo 0
}

last_mtime=$(mtime)
run_supercronic
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
        run_supercronic
        echo "[run-scheduler] supercronic restarted (pid=$pid)"
    fi
done
