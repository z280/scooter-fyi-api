#!/bin/bash
# Make a Claude Code on the web session able to run this repo's tests FOR REAL.
#
# WHY THIS EXISTS. 25 test files are Postgres-only and self-skip when
# VEO_TEST_PG_DSN is unset — and a skip is indistinguishable from a pass in a
# summary line. On 2026-10-07 that gap let two broken commits reach a push from
# a web session that reported "2325 passed": a test that poisoned the shared
# database for every later pg test, and a migration whose constraint could not
# be replayed over existing rows. Both were found only after CI went red, and
# both were obvious the moment a database was present — 286 tests that had been
# skipping started running, and they failed.
#
# CI already does this (see .github/workflows/deploy.yml's `test` job, whose own
# comment says the same thing). This makes a web session match it.
set -euo pipefail

# BOTH OF THESE ARE SET BY THE HARNESS, and `set -u` turns a missing one into a
# hook that dies mid-way with "unbound variable" — after Postgres is up and
# before the DSN is exported, which is the worst possible place to stop: the pg
# suites go back to skipping and the only clue is one line about a shell
# variable. Defaulted so the failure is a sentence instead, and so this script
# can be run by hand to check it.
: "${CLAUDE_PROJECT_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
: "${CLAUDE_ENV_FILE:=}"

# Web sessions only, per the hook convention: a developer's machine has its own
# database and its own opinions about what listens on 5432.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)"
PGDATA=/var/lib/postgresql/claude-session
# The same credentials, host and port CI uses, so VEO_TEST_PG_DSN below is
# character-for-character the one in the workflow. A session that passes against
# a different DSN shape has proved less than it looks.
PGUSER_T=test
PGPASS_T=test
PGDB_T=test
PGHOST_T=127.0.0.1
PGPORT_T=5432

echo "session-start: installing Python dependencies"
# NOT fatal, and deliberately not what CI does. The workflow upgrades pip on a
# fresh setup-python install; this container's pip is Debian-managed, where the
# upgrade fails with "Cannot uninstall pip 24.0, RECORD file not found" and
# `set -e` would take the whole hook down over a version bump nothing needs.
python -m pip install --upgrade pip >/dev/null 2>&1 || true
# `install`, not a locked sync: the container caches its state after this hook,
# so the next session starts from the already-installed tree.
pip install -r "$CLAUDE_PROJECT_DIR/requirements.txt" pytest >/dev/null

start_postgres() {
  if [ -z "$PGBIN" ]; then
    echo "session-start: no postgres server installed — pg tests will skip" >&2
    return 1
  fi
  # Idempotent: a resume or a /clear re-runs this hook against a container that
  # may already have the server up.
  if "$PGBIN/pg_isready" -h "$PGHOST_T" -p "$PGPORT_T" -q 2>/dev/null; then
    echo "session-start: postgres already accepting connections"
    return 0
  fi
  if [ ! -s "$PGDATA/PG_VERSION" ]; then
    mkdir -p "$PGDATA"
    chown postgres:postgres "$PGDATA"
    # initdb refuses to run as root, which is what this session is.
    su postgres -c "$PGBIN/initdb -D $PGDATA -U postgres --auth=trust" >/dev/null
  fi
  # A RESUMED CONTAINER KEEPS THE DATA DIRECTORY AND LOSES THE PROCESS, so
  # `postmaster.pid` is left behind pointing at a pid that no longer exists.
  # pg_ctl then prints "another server might be running; trying to start server
  # anyway" and starts fine — but an alarming line in a session's first output
  # is a line somebody has to stop and read. `pg_isready` above has already
  # established nothing is listening, so a pid file here is stale by definition.
  if [ -f "$PGDATA/postmaster.pid" ]; then
    rm -f "$PGDATA/postmaster.pid"
  fi
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-p $PGPORT_T -h $PGHOST_T' -l $PGDATA/server.log -w start" >/dev/null
  local tries=0
  until "$PGBIN/pg_isready" -h "$PGHOST_T" -p "$PGPORT_T" -q 2>/dev/null; do
    tries=$((tries + 1))
    [ "$tries" -gt 30 ] && { echo "session-start: postgres did not come up" >&2; return 1; }
    sleep 1
  done
  psql -h "$PGHOST_T" -p "$PGPORT_T" -U postgres -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname='$PGUSER_T'" | grep -q 1 || \
    psql -h "$PGHOST_T" -p "$PGPORT_T" -U postgres -q \
      -c "CREATE ROLE $PGUSER_T LOGIN SUPERUSER PASSWORD '$PGPASS_T'"
  psql -h "$PGHOST_T" -p "$PGPORT_T" -U postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$PGDB_T'" | grep -q 1 || \
    psql -h "$PGHOST_T" -p "$PGPORT_T" -U postgres -q \
      -c "CREATE DATABASE $PGDB_T OWNER $PGUSER_T"
  echo "session-start: postgres ready on $PGHOST_T:$PGPORT_T"
}

if start_postgres; then
  DSN="postgresql://$PGUSER_T:$PGPASS_T@$PGHOST_T:$PGPORT_T/$PGDB_T"
  {
    echo "export VEO_TEST_PG_DSN=\"$DSN\""
    # `src/config.py` reads these for its own connections, and `src.cli migrate`
    # fails with a bare KeyError without them.
    echo "export POSTGRES_USER=\"$PGUSER_T\""
    echo "export POSTGRES_PASSWORD=\"$PGPASS_T\""
    echo "export POSTGRES_DB=\"$PGDB_T\""
    echo "export POSTGRES_HOST=\"$PGHOST_T\""
    echo "export POSTGRES_PORT=\"$PGPORT_T\""
    # Both mirror the workflow. The salt is fixed so vehicle-identifier HMACs
    # are reproducible across runs, as the fixtures assume.
    echo "export VEHICLE_IDENTIFIER_SALT=ci-fixed-salt"
    echo "export VEO_CONFIG=\"$CLAUDE_PROJECT_DIR/config.json\""
  } >> "${CLAUDE_ENV_FILE:-/dev/null}"
  if [ -z "${CLAUDE_ENV_FILE:-}" ]; then
    echo "session-start: no CLAUDE_ENV_FILE — the DSN is not exported, so the pg suites will skip" >&2
  fi

  # Applied up front so a session starts with the schema already there, as CI
  # does. NOT fatal if it fails: the pg fixtures replay `sql/` themselves, and
  # somebody whose session exists to FIX a broken migration must still get a
  # session. The warning is the signal.
  if ! ( cd "$CLAUDE_PROJECT_DIR" && \
         POSTGRES_USER="$PGUSER_T" POSTGRES_PASSWORD="$PGPASS_T" POSTGRES_DB="$PGDB_T" \
         POSTGRES_HOST="$PGHOST_T" POSTGRES_PORT="$PGPORT_T" \
         VEO_CONFIG="$CLAUDE_PROJECT_DIR/config.json" \
         python -m src.cli migrate >/dev/null 2>&1 ); then
    echo "session-start: migrations did not apply cleanly — the pg fixtures will still replay sql/" >&2
  fi
  # Said only when it is true. The warning above already explains the other
  # case, and a hook that printed both would be telling somebody the suites
  # will run two lines after telling them they will skip.
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "session-start: VEO_TEST_PG_DSN set — the Postgres-only suites will run, not skip"
  fi
fi
