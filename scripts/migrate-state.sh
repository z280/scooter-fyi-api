#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# migrate-state.sh — move veo-audit's LOCAL state from the old box to the new
# one. Run from a control machine (your laptop) that can SSH to BOTH boxes.
#
# What is and isn't "state" here:
#   * .env secrets            -> NOT handled here. They are rendered on the box
#                               from GitHub Actions secrets by deploy.yml. To
#                               migrate them you just repoint the VPS_HOST
#                               secret at the new box and run the workflow.
#   * container image         -> NOT handled here. Pulled from GHCR on deploy.
#   * Postgres data (pgdata)  -> YES. pg_dump on old -> restore on new.
#   * scheduler crontab       -> YES. The admin-editable live crontab lives on
#     (scheduler_state volume)   the scheduler_state volume; repo `crontab` only
#                               seeds a fresh volume, so any /admin edits (e.g.
#                               a hand-tuned cadence) are ONLY here.
#
# ORDER OF OPERATIONS (see MIGRATION.md for the full runbook):
#   1. Bring the new stack UP first (deploy.yml against the new box) so its
#      empty Postgres + volumes exist.
#   2. Freeze writes on the OLD box  (stop scheduler + worker; Postgres stays up
#      for the dump). This is the ~few-minute window where ingest pauses.
#   3. Run THIS script  -> dumps old Postgres, restores into new Postgres,
#      copies the scheduler crontab.
#   4. Verify on the new box, then cut the tunnel over (stop old cloudflared).
#
# Usage:
#   OLD=ubuntu@ai.neill.io NEW=ubuntu@<new-ip> ./scripts/migrate-state.sh
#
# Env:
#   OLD, NEW      required ssh targets (user@host) for the two boxes
#   OLD_DIR       compose dir on old box   (default /opt/veo-audit)
#   NEW_DIR       compose dir on new box   (default /opt/veo-audit)
#   PROJECT       compose project name     (default basename of the dir)
#   PGUSER,PGDB   override if your .env differs (default veo / veo_audit)
#   DRY_RUN=1     print the steps, touch nothing
# ---------------------------------------------------------------------------
set -euo pipefail

OLD="${OLD:?set OLD=user@old-host}"
NEW="${NEW:?set NEW=user@new-host}"
OLD_DIR="${OLD_DIR:-/opt/veo-audit}"
NEW_DIR="${NEW_DIR:-/opt/veo-audit}"
PROJECT="${PROJECT:-veo-audit}"
PGUSER="${PGUSER:-veo}"
PGDB="${PGDB:-veo_audit}"
DRY_RUN="${DRY_RUN:-0}"

DB_CONTAINER="denver_spatial_db"
STATE_VOL="${PROJECT}_scheduler_state"
WORKDIR="$(mktemp -d)"
DUMP="$WORKDIR/veo_audit.dump"
trap 'rm -rf "$WORKDIR"' EXIT

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
run()  { if [[ "$DRY_RUN" == "1" ]]; then printf '   [dry] %s\n' "$*"; else eval "$*"; fi; }

# --- Preflight: both DB containers reachable & healthy ----------------------
say "Preflight: checking Postgres on both boxes"
for tgt in "$OLD" "$NEW"; do
  if ! ssh "$tgt" "docker exec $DB_CONTAINER pg_isready -U $PGUSER -d $PGDB" >/dev/null 2>&1; then
    echo "ERROR: Postgres ($DB_CONTAINER) not ready on $tgt." >&2
    echo "  On NEW this means the stack isn't up yet — deploy it first (step 1)." >&2
    exit 1
  fi
done

# Refuse to clobber a NEW db that already has ingested data (guards a re-run
# that would double-load). Override by dropping/recreating the db yourself.
NEW_ROWS=$(ssh "$NEW" "docker exec $DB_CONTAINER psql -U $PGUSER -d $PGDB -tAc \
  \"SELECT count(*) FROM information_schema.tables WHERE table_schema='public'\"" 2>/dev/null || echo 0)
if [[ "${NEW_ROWS:-0}" -gt 0 && "$DRY_RUN" != "1" ]]; then
  echo "WARNING: new Postgres already has $NEW_ROWS public tables." >&2
  read -r -p "Restore ANYWAY (pg_restore --clean will drop+recreate)? [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted."; exit 1; }
fi

# --- 1. Dump old Postgres (custom format, no owner/acl — single-role db) -----
say "Dumping old Postgres -> $DUMP"
run "ssh '$OLD' 'docker exec $DB_CONTAINER pg_dump -U $PGUSER -d $PGDB -Fc --no-owner --no-acl' > '$DUMP'"
if [[ "$DRY_RUN" != "1" ]]; then
  sz=$(stat -c %s "$DUMP" 2>/dev/null || stat -f %z "$DUMP")
  say "Dump size: $(( sz / 1024 )) KiB"
  [[ "$sz" -gt 0 ]] || { echo "ERROR: empty dump — aborting before touching NEW." >&2; exit 1; }
fi

# --- 2. Restore into new Postgres -------------------------------------------
# --clean --if-exists so a re-run replaces cleanly; --no-owner keeps it under
# the single veo role regardless of the dump's recorded owner.
say "Restoring into new Postgres on $NEW"
run "ssh '$NEW' 'docker exec -i $DB_CONTAINER pg_restore -U $PGUSER -d $PGDB --clean --if-exists --no-owner --no-acl' < '$DUMP'"

# --- 3. Copy the admin-editable scheduler crontab ---------------------------
# Stream the scheduler_state volume contents old -> new via a throwaway
# alpine container on each side. Only the crontab lives here; it's tiny.
say "Copying scheduler_state volume ($STATE_VOL): old -> new"
run "ssh '$OLD' 'docker run --rm -v ${STATE_VOL}:/s alpine tar -C /s -cf - .' \
     | ssh '$NEW' 'docker run --rm -i -v ${STATE_VOL}:/s alpine tar -C /s -xf -'"

# --- 4. Post-restore sanity -------------------------------------------------
say "Sanity: row counts on the new box (spot-check a core table)"
run "ssh '$NEW' 'docker exec $DB_CONTAINER psql -U $PGUSER -d $PGDB -tAc \
   \"SELECT (SELECT count(*) FROM raw_telemetry_points) AS raw_points\"' || true"

say "State migration complete."
cat <<EOF

Next (see MIGRATION.md):
  * Restart the NEW scheduler so supercronic reloads the copied crontab:
        ssh $NEW 'cd $NEW_DIR && docker compose restart scheduler'
  * Verify the API on the new box (before cutover):
        ssh $NEW 'curl -sf localhost:8080/healthz || docker compose -f $NEW_DIR/docker-compose.yml logs --tail=50 pipeline_worker'
  * Cut the tunnel over: stop cloudflared on the OLD box so only the new
    connector serves data.scooter.fyi:
        ssh $OLD 'cd $OLD_DIR && docker compose stop cloudflared'
EOF
