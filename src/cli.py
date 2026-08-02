"""Subcommand entry points for the cron scheduler.

Invoked from the crontab inside the `scheduler` container as
`python -m src.cli <command>`. Each command is idempotent and safe
to run repeatedly — the cron daemon doesn't track state.

Available commands:
    ingest_cycle      Fetch GBFS + DuckDB compute + write to Postgres.
    archive_if_due    Run the R2 archive only if >= archive_hours have
                      passed since the last successful archive.
    daily_sla         Compute today's 6-9 AM Denver SLA window.
    daily_trips       Roll up yesterday's full-day trip/popularity stats.
    cleanup_receipts  Delete receipt images past the 18-month retention
                      (API_REQUIREMENTS.md §3.2 / privacy policy).
    cleanup_ride_screenshots
                      Delete ride transaction screenshots (+ their row)
                      past the 18-month retention (/api/v1/meta/privacy).
    cleanup_model_report_photos
                      Delete model-report photos past the same 18-month
                      retention as receipts, stamping photo_deleted_at.
                      The report row (the catalog correction itself)
                      outlives the image.
    backfill_public_usernames
                      One-time: assign a public_username to every account
                      created before sql/025 (idempotent — already-
                      assigned accounts are skipped).
    expire_stale_watches
                      Close out user_device_watch_list / tracked_rides
                      rows whose 3h watch window elapsed with no GBFS-side
                      resolution (src/ride_watch.py handles the two live
                      transitions every 2-min cycle; this just terminates
                      the ones that timed out).
    expire_stale_off_feed_rides
                      Close out off-feed `rides` left 'active' for more
                      than 24 hours with no end report, freeing the
                      one-active-ride slot they were holding.
    fetch_map_pbf     Sync the routing .pbf + canopy sidecar from R2 into the
                      Valhalla volume. Runs as a one-shot sidecar before the
                      valhalla service starts.
    refresh_routing_graph
                      Re-sync the routing assets and report whether the .pbf
                      changed, i.e. whether Valhalla needs a tile rebuild.
    fetch_photon_index
                      Sync the Photon geocoding index from R2 into the
                      photon_files volume. Runs as a one-shot sidecar before
                      the photon service starts.
    refresh_photon_index
                      Re-check R2 for a newer geocoding index (ETag-gated, a
                      no-op on all but the ~4 days a year it is rebuilt) and
                      log loudly when the photon container needs restarting
                      to load one.
    extract_battery_trips
                      Mine the last ~26h of telemetry for observation-gap trips
                      matching the anchor filter, route them through Valhalla,
                      store the observations.
    train_battery_model
                      Refit the battery-burn regression on stored observations.
    backfill_battery_trips
                      Seed observations from the R2 parquet archive instead of
                      waiting for the daily job to accumulate them. Needs ~2 GiB
                      -- raise the container limit before running (see the
                      docstring).
    deidentify_donations
                      De-identify donated ride tracks (track_donations +
                      donated_track_points) 4h after their points settle,
                      force-floored at 28h after donation even if points
                      never settle. Also sweeps ride_routes on its own 28h
                      clock once sql/052 (phase A3) exists -- a
                      to_regclass-guarded no-op until then.
    recompute_area_leaders
                      Recompute the H3 r8 area leader report (FEATURE_PLAN_
                      2026-07.md §11 / PLAN_RIDE_MODE_API.md phase A4):
                      trailing-28-day per-cell leaderboard, full replace
                      (src/area_leaders.py:recompute).
    process_device_feature_reports
                      Grade the crowdsourced device-feature confirmations
                      that have landed since the last firing (sql/055):
                      first valid report is authoritative, a later
                      disagreement flags the vehicle 'needs review', and
                      three reports resolve one by 2/3 consensus
                      (src/device_features.py:process_pending).
    migrate           Apply pending SQL migrations.
    admin             Manage the admin allowlist:
                      `admin (list | add <email> | remove <email>)`.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from .archive import run_archive
from .area_leaders import recompute as recompute_area_leaders
from .battery_model import (
    backfill_trips_from_archive,
    extract_trips,
    train as train_battery,
)
from .comms_replies import poll_once as poll_comms_replies
from .config import load
from .cycle import run_once
from .r2_map import sync_map_assets, sync_photon_index
from .daily_sla import run_daily
from .daily_trips import run_daily as run_daily_trips
from .device_features import process_pending as process_device_feature_reports
from .pg import connection, run_migrations
from .ride_watch import finalize_validation
from .sentry import capture_exception, init as sentry_init, monitor

log = logging.getLogger("veo.cli")


# Sentry Cron Monitors — Sentry auto-creates these on first check-in using
# the schedule embedded below. Match the cron expressions in /app/crontab.
# checkin_margin = how late a check-in can arrive before counting as missed
# max_runtime = how long a run can take before Sentry alerts on a stuck job
_MONITOR_INGEST = {
    "schedule": {"type": "crontab", "value": "*/2 * * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 1,    # minutes; must stay under the 2-min interval
    "max_runtime": 2,       # cycles run ~5s; a 2-min run is already stuck
    # 5 consecutive failed cycles = one full 10-minute SLA interval with no
    # data. Polling (2 min) is now decoupled from the SLA interval (10 min);
    # a single missed 2-min cycle is a blip, not an SLA-scale event.
    "failure_issue_threshold": 5,
    "recovery_threshold": 1,
}
_MONITOR_DAILY_SLA = {
    "schedule": {"type": "crontab", "value": "0 9 * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 10,
    "max_runtime": 5,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}
_MONITOR_ARCHIVE = {
    "schedule": {"type": "crontab", "value": "0 2 * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 30,
    "max_runtime": 30,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}
_MONITOR_DAILY_TRIPS = {
    "schedule": {"type": "crontab", "value": "0 9 * * *"},
    "timezone": "America/Denver",
    "checkin_margin": 10,
    "max_runtime": 10,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}


@monitor(slug="ingest_cycle", monitor_config=_MONITOR_INGEST)
def _cli_ingest_cycle():
    return run_once()


@monitor(slug="daily_sla", monitor_config=_MONITOR_DAILY_SLA)
def _cli_daily_sla():
    return run_daily()


@monitor(slug="daily_trips", monitor_config=_MONITOR_DAILY_TRIPS)
def _cli_daily_trips():
    return run_daily_trips()


@monitor(slug="archive_if_due", monitor_config=_MONITOR_ARCHIVE)
def _cli_archive_if_due():
    return archive_if_due()


def _last_archive_ts() -> datetime | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM system_state WHERE key = 'last_archive_ts'")
            r = cur.fetchone()
    if not r or not r[0]:
        return None
    try:
        return datetime.fromisoformat(r[0])
    except ValueError:
        return None


def archive_if_due() -> dict | None:
    """Only run the archive when the last success was more than
    archive_hours ago. Lets us cron this daily without forcing a 48h
    cadence at the cron-config level."""
    cfg = load()
    last = _last_archive_ts()
    if last is None:
        log.info("archive_if_due: no prior archive recorded — running")
        return run_archive()
    age = datetime.now(timezone.utc) - last
    if age < timedelta(hours=cfg.schedule.archive_hours):
        log.info(
            "archive_if_due: last success was %.1fh ago (< %dh window), skipping",
            age.total_seconds() / 3600.0,
            cfg.schedule.archive_hours,
        )
        return None
    log.info(
        "archive_if_due: last success was %.1fh ago, running",
        age.total_seconds() / 3600.0,
    )
    return run_archive()


_RECEIPT_RETENTION_MONTHS = 18


def _months_ago(dt: datetime, months: int) -> datetime:
    """Subtract calendar months, not `months * 30` days — that shortcut
    under-counts by up to ~9 days over 18 months and would delete receipts
    before the documented retention actually elapses."""
    total_months = dt.year * 12 + (dt.month - 1) - months
    year, month = divmod(total_months, 12)
    month += 1
    # Clamp day-of-month for the rare case a shorter target month can't
    # hold it (e.g. Aug 31 - 18mo -> Feb 31 doesn't exist -> Feb 28/29).
    import calendar
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def cleanup_receipts() -> dict:
    """Purge receipt images older than the documented 18-month retention.

    Deletes the R2 object and stamps receipt_deleted_at; the report row
    itself is kept (the evidence record outlives the image). Idempotent —
    rows already stamped are skipped.
    """
    from .receipts import ReceiptError, delete_receipt

    cutoff = _months_ago(datetime.now(timezone.utc), _RECEIPT_RETENTION_MONTHS)
    deleted = failed = 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, receipt_r2_key FROM discount_reports
                WHERE receipt_r2_key IS NOT NULL
                  AND receipt_deleted_at IS NULL
                  AND created_at < %s
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
            for row_id, key in rows:
                try:
                    delete_receipt(key)
                except ReceiptError:
                    log.exception("cleanup_receipts: R2 not configured — aborting")
                    raise
                except Exception:  # noqa: BLE001 — keep going, retry next run
                    log.exception("cleanup_receipts: delete failed for %s", key)
                    failed += 1
                    continue
                cur.execute(
                    "UPDATE discount_reports SET receipt_deleted_at = NOW() WHERE id = %s",
                    (row_id,),
                )
                deleted += 1
        conn.commit()
    log.info("cleanup_receipts: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


_SCREENSHOT_RETENTION_MONTHS = 18


def cleanup_ride_screenshots() -> dict:
    """Purge ride transaction screenshots older than 18 months — mirrors
    cleanup_receipts, but the row itself is deleted once the image is
    gone rather than kept with a tombstone column: unlike discount_reports
    (which carries other evidence fields), a ride_transaction_screenshots
    row has no purpose once its one image is removed.
    """
    from .ride_screenshots import RideScreenshotError, delete_screenshot

    cutoff = _months_ago(datetime.now(timezone.utc), _SCREENSHOT_RETENTION_MONTHS)
    deleted = failed = 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, r2_key FROM ride_transaction_screenshots WHERE created_at < %s",
                (cutoff,),
            )
            rows = cur.fetchall()
            for row_id, key in rows:
                try:
                    delete_screenshot(key)
                except RideScreenshotError:
                    log.exception("cleanup_ride_screenshots: R2 not configured — aborting")
                    raise
                except Exception:  # noqa: BLE001 — keep going, retry next run
                    log.exception("cleanup_ride_screenshots: delete failed for %s", key)
                    failed += 1
                    continue
                cur.execute("DELETE FROM ride_transaction_screenshots WHERE id = %s", (row_id,))
                deleted += 1
        conn.commit()
    log.info("cleanup_ride_screenshots: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


_MODEL_PHOTO_RETENTION_MONTHS = 18


def cleanup_model_report_photos() -> dict:
    """Purge model-report photos older than the 18-month retention.

    sql/038 added model_reports.photo_deleted_at and nothing ever set it:
    cleanup_receipts scans discount_reports only, so every `model-reports/`
    object ever uploaded was retained forever, in the same private bucket
    and under the same published 18-month promise as receipts. A photo of a
    scooter is a photo of wherever the rider was standing, so "forever" was
    not a defensible default and definitely not the documented one.

    Mirrors cleanup_receipts rather than cleanup_ride_screenshots: the row
    is KEPT and stamped, because a model report carries the correction
    itself (description, resolved_model_name, the operator queue state)
    which outlives the image. A ride_transaction_screenshots row, by
    contrast, is nothing but its image, so that job deletes the row.

    Idempotent — rows already stamped are skipped by the WHERE clause.
    """
    from .receipts import ReceiptError, delete_receipt

    cutoff = _months_ago(datetime.now(timezone.utc), _MODEL_PHOTO_RETENTION_MONTHS)
    deleted = failed = 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, photo_r2_key FROM model_reports
                WHERE photo_r2_key IS NOT NULL
                  AND photo_deleted_at IS NULL
                  AND created_at < %s
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
            for row_id, key in rows:
                try:
                    delete_receipt(key)
                except ReceiptError:
                    log.exception("cleanup_model_report_photos: R2 not configured — aborting")
                    raise
                except Exception:  # noqa: BLE001 — keep going, retry next run
                    log.exception("cleanup_model_report_photos: delete failed for %s", key)
                    failed += 1
                    continue
                cur.execute(
                    "UPDATE model_reports SET photo_deleted_at = NOW() WHERE id = %s",
                    (row_id,),
                )
                deleted += 1
        conn.commit()
    log.info("cleanup_model_report_photos: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


def backfill_public_usernames() -> dict:
    """One-time backfill: assign a public_username to every account that
    doesn't have one yet (pre-sql/025 accounts). Idempotent — accounts
    that already have one are skipped by the WHERE clause, so safe to
    re-run. `python -m src.cli backfill_public_usernames`.

    Commits per-row rather than in one giant transaction: each call to
    assign_public_username takes a short-lived advisory lock per candidate
    word pair, and committing frequently keeps any one lock's hold time
    short so a large backfill can't add latency to concurrent live
    sign-ups. It also means a crash mid-run leaves resumable progress
    instead of losing everything.
    """
    from .accounts import assign_public_username

    assigned = 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE public_username IS NULL ORDER BY id")
            ids = [r[0] for r in cur.fetchall()]
        for account_id in ids:
            with conn.cursor() as cur:
                assign_public_username(cur, account_id)
            conn.commit()
            assigned += 1
    log.info("backfill_public_usernames: assigned=%d", assigned)
    return {"assigned": assigned}


def expire_stale_watches() -> dict:
    """Close out watches/rides whose 3h window elapsed with no GBFS-side
    resolution. NOT required for read-path correctness — every read query
    already filters watch_expires_at/timestamps directly (see
    src/api_tracked_rides.py) — this exists to (1) give riders a clean
    terminal status instead of one stuck showing 'watching' forever, and
    (2) keep idx_watch_list_open_expiry / idx_tracked_rides_open (both
    partial indexes keyed on status/NULL-checks) from accumulating rows
    that can never match a live query again.

    ALSO the finalize_validation hook for a `pending_feed` ride whose watch
    window elapsed without GBFS ever resolving (PLAN_RIDE_MODE_API.md phase
    A2, "Validation finisher" — src/ride_watch.py:finalize_validation). The
    ride-side UPDATE just above SKIPS a donated ride: it already has
    `user_reported_ended_at` set (PATCH .../end ran) and its `status` is
    whatever /end left it as (not 'watching'/'left_feed'), so it never
    matches that UPDATE's WHERE clause. The finalizer therefore selects on
    the elapsed watch window itself (`watch_expires_at < NOW()`, no
    `gbfs_reappeared_at`) rather than on ride status — the two selections
    are deliberately independent and neither should be folded into the
    other's WHERE clause.
    """
    now = datetime.now(timezone.utc)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_device_watch_list SET status = 'expired' "
                "WHERE status IN ('watching', 'left_feed') AND watch_expires_at < %s",
                (now,),
            )
            watches_expired = cur.rowcount
            cur.execute(
                "UPDATE tracked_rides SET status = 'expired', updated_at = %s "
                "WHERE status IN ('watching', 'left_feed') "
                "AND watch_expires_at < %s AND user_reported_ended_at IS NULL",
                (now, now),
            )
            rides_expired = cur.rowcount

            cur.execute(
                """
                SELECT id FROM tracked_rides
                WHERE validation_status = 'pending_feed'
                  AND watch_expires_at < %s
                  AND gbfs_reappeared_at IS NULL
                """,
                (now,),
            )
            stale_ride_ids = [r[0] for r in cur.fetchall()]
        conn.commit()

        # One ride per transaction — same reasoning as
        # src/ride_watch.py:update_watches_for_cycle's own finalizer loop:
        # finalize_validation takes its own ride_validation:<ride_id>
        # advisory lock before touching that ride's row, and one ride's
        # failure must not roll back the expiry work already committed
        # above or block the next stale ride in this same run.
        finalized = 0
        for ride_id in stale_ride_ids:
            try:
                with conn.cursor() as cur:
                    result = finalize_validation(cur, str(ride_id))
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception(
                    "expire_stale_watches: finalize_validation failed for ride %s", ride_id)
                continue
            if result is not None:
                finalized += 1

    log.info("expire_stale_watches: watches=%d rides=%d finalized=%d",
              watches_expired, rides_expired, finalized)
    return {"watches_expired": watches_expired, "rides_expired": rides_expired,
            "finalized_validations": finalized}


_OFF_FEED_RIDE_MAX_ACTIVE_HOURS = 24


def expire_stale_off_feed_rides() -> dict:
    """Close out off-feed rides (sql/035) left 'active' for 24 hours.

    UNLIKE expire_stale_watches, this one IS required for correctness, not
    just for tidy state. idx_rides_one_active_per_account is a partial
    UNIQUE index on WHERE status = 'active', so a single abandoned ride
    409s its owner out of POST /api/v1/rides/start permanently — the rider
    can never start another ride, and before this job the only escape was
    DELETE, which destroys the abandoned ride's whole waypoint track.
    Expiry frees the slot without touching rider data.

    THE CLOCK RUNS FROM created_at, NOT started_at. RideStartIn lets a
    client backdate started_at (so a rider who noticed late doesn't lose
    the first minutes of the ride), which makes it spoofable in both
    directions: backdating 25h would otherwise expire a ride the instant
    it began, and post-dating it would exempt the ride forever. created_at
    is DEFAULT NOW() and nothing outside Postgres writes it, so the
    guarantee this job actually makes is "no active ride survives 24h past
    the moment the server learned about it".

    Terminal-state semantics are sql/040's: ended_at/duration_s/end_lat/
    end_lon stay NULL (we never observed an end and will not invent one),
    the waypoint-measured distance is left exactly as it stood, the row and
    its waypoints are kept and still export, and src/badges.py counts none
    of it — its union takes `status = 'completed'` only, mirroring the way
    tracked_rides' expired rows fall out on user_reported_ended_at IS NULL.

    Idempotent: an already-expired ride no longer matches status = 'active'.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_OFF_FEED_RIDE_MAX_ACTIVE_HOURS)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rides SET status = 'expired' "
                "WHERE status = 'active' AND created_at < %s",
                (cutoff,),
            )
            rides_expired = cur.rowcount
        conn.commit()
    log.info("expire_stale_off_feed_rides: rides=%d", rides_expired)
    return {"rides_expired": rides_expired}


def _cli_fetch_map_pbf() -> dict:
    """One-shot sidecar: pull the routing assets into the Valhalla volume.

    Always exits 0. `valhalla` gates on this via
    service_completed_successfully, and the deploy script runs under `set -e`,
    so a non-zero exit here fails `docker compose up -d` and aborts the deploy
    of everything else in the push. A missing credential or an R2 outage must
    degrade routing, not block a deploy. sync_map_assets logs loudly and
    reports `pbf_present` so the failure is visible.
    """
    return sync_map_assets()


def _cli_refresh_routing_graph() -> dict:
    """Re-sync the routing assets on a schedule and report whether they moved.

    Valhalla's scripted entrypoint hashes the .pbf and rebuilds tiles when it
    changes, so the rebuild is triggered by recreating the container once this
    reports pbf_changed. Left as an operator/deploy step rather than giving this
    container a Docker socket.
    """
    result = sync_map_assets()
    if result.get("pbf_changed"):
        log.warning("Routing .pbf changed — recreate the valhalla service to "
                    "rebuild tiles: docker compose up -d --force-recreate valhalla")
    return result


def _cli_fetch_photon_index() -> dict:
    """One-shot sidecar: pull the Photon geocoding index into its volume.

    Always exits 0, for the same reason as _cli_fetch_map_pbf above: `photon`
    gates on this via service_completed_successfully and the deploy script runs
    under `set -e`, so a missing credential or an R2 outage must degrade
    address autocomplete (a clean 503 from /api/v1/geocode/search) rather than
    abort the deploy of everything else in the push. sync_photon_index catches
    its own errors, logs loudly, and reports `index_present` so the failure is
    visible.
    """
    return sync_photon_index()


def _cli_refresh_photon_index() -> dict:
    """Re-check R2 for a newer geocoding index on a schedule (cron, 05:00).

    ETag-gated, so this is a no-op on all but the handful of days a year the
    index is rebuilt by hand (scripts/build_photon_index.md). REVIEW FIX:
    sync_photon_index only STAGES a changed index now — it never swaps it
    into the live, served directory itself (see that function's own doc
    comment for why and for the exact operator promotion sequence: stop
    photon, swap the staged directory in, start photon, verify, then delete
    the old one). This container deliberately has no Docker socket, exactly
    like refresh_routing_graph and Valhalla's tile rebuild.
    """
    result = sync_photon_index()
    if result.get("changed"):
        log.warning(
            "Photon geocoding index staged at %s — promote it with the "
            "operator stop/swap/start/health-check sequence documented on "
            "sync_photon_index, then delete the old directory once verified.",
            result.get("staged_dir"),
        )
    return result


def _cli_extract_battery_trips() -> dict:
    return extract_trips()


def _cli_train_battery_model() -> dict:
    return train_battery()


def _cli_backfill_battery_trips() -> dict:
    """Seed the observations table from the R2 parquet archive.

    Run by hand, not from cron: measured at ~1.25 GiB peak RSS on a single
    archive file, which exceeds the scheduler's 1024m limit. Raise it first:
        docker update --memory 2g --memory-swap 2g scheduler
    """
    return backfill_trips_from_archive()


# ---------------------------------------------------------------------------
# H3 r8 area leader report (FEATURE_PLAN_2026-07.md §11 / PLAN_RIDE_MODE_API.md
# phase A4). Cron: `15 9 * * * python -m src.cli recompute_area_leaders`.
# ---------------------------------------------------------------------------
def _cli_recompute_area_leaders() -> dict:
    return recompute_area_leaders()


# ---------------------------------------------------------------------------
# De-id sweep (PLAN_RIDE_MODE_API.md phase A2 / RIDE_MODE_OVERHAUL_PLAN.md
# glossary "De-id"). Cron: `15 * * * * python -m src.cli deidentify_donations`.
# ---------------------------------------------------------------------------

# Two independent triggers name the same wall-clock unit (hours) but read
# from two different columns — kept as separate constants rather than one
# "retention hours" so a future change to either window can't silently move
# the other.
_DEID_SETTLED_GRACE_HOURS = 4
_DEID_DONATION_FORCE_FLOOR_HOURS = 28
_MS_PER_MINUTE = 60_000


def deidentify_donations(dry_run: bool = False) -> dict:
    """De-identify donated ride tracks once points have settled — the sweep
    named in PLAN_RIDE_MODE_API.md phase A2 and
    RIDE_MODE_OVERHAUL_PLAN.md's "De-id" glossary entry.
    `python -m src.cli deidentify_donations`, hourly at :15.

    A `track_donations` row is swept the moment EITHER is true:

      - `points_settled_at` (stamped by the donation handler, or by A2's
        `finalize_validation` for a `pending_feed` donation that settles
        late — stamped on settle REGARDLESS of outcome, so a denied
        donation starts this clock too) is more than
        `_DEID_SETTLED_GRACE_HOURS` (4h) in the past. The normal path: a
        rider gets a few hours to see their award before the underlying
        track is severed from their account.
      - `donated_at` is more than `_DEID_DONATION_FORCE_FLOOR_HOURS` (28h)
        in the past, REGARDLESS of whether points ever settled. This is
        the force floor: a donation whose GBFS correlation never resolves
        (`validation.status` stuck at `pending_feed` forever, so
        `points_settled_at` stays NULL forever) must not keep full
        account + geometry linkage indefinitely just because settlement
        never happened.

    Sweeping nulls `account_id` and `tracked_ride_id` (severing the FKs
    that make hard-delete cascade pre-sweep — post-sweep the artifact has
    no owner left to cascade from) and stamps `deidentified_at`; every one
    of that donation's `donated_track_points.recorded_ms` is coarsened to
    minute precision in the same pass. `chain_root_hash`, `vehicle_model`,
    `distance_meters`, and the rest of the row are left alone — the
    de-identified geometry + derived battery observation are exactly what
    the battery-modeling feedback loop still needs.

    `WHERE deidentified_at IS NULL` is both the eligibility filter and the
    idempotence guard: a row already swept never matches again, so this
    command is safe to run every hour forever (or twice in the same
    minute) with no double effect.

    `ride_routes` (PLAN_RIDE_MODE_API.md phase A3, sql/052) sweeps on its
    OWN 28h clock, independent of any donation — a nav-improvement ride
    whose track is never donated still stored route geometry, and hanging
    its de-id off a donation that may not exist would keep it
    account-linked forever. That table does not exist in this build order
    (A2 lands before A3), so this arm is guarded on
    `to_regclass('ride_routes') IS NOT NULL`: `to_regclass` returns NULL
    for an unknown relation instead of raising, so the guard is a safe
    existence probe against a database that has not yet applied sql/052,
    and the arm underneath it is a pure no-op today. The moment sql/052
    lands, `to_regclass` starts returning a real relation id and this same
    predicate activates with no further code change.

    `dry_run=True` counts what the sweep WOULD touch without writing
    anything (a manual sanity check before changing the cron); the cron
    itself always calls this with the default `dry_run=False`.
    """
    now = datetime.now(timezone.utc)
    settled_cutoff = now - timedelta(hours=_DEID_SETTLED_GRACE_HOURS)
    donated_cutoff = now - timedelta(hours=_DEID_DONATION_FORCE_FLOOR_HOURS)

    donations_deidentified = 0
    points_coarsened = 0
    ride_routes_deidentified = 0

    with connection() as conn:
        with conn.cursor() as cur:
            if dry_run:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM track_donations
                    WHERE deidentified_at IS NULL
                      AND (points_settled_at < %s OR donated_at < %s)
                    """,
                    (settled_cutoff, donated_cutoff),
                )
                (donations_deidentified,) = cur.fetchone()
            else:
                cur.execute(
                    """
                    UPDATE track_donations
                    SET account_id = NULL, tracked_ride_id = NULL, deidentified_at = NOW()
                    WHERE deidentified_at IS NULL
                      AND (points_settled_at < %s OR donated_at < %s)
                    RETURNING id
                    """,
                    (settled_cutoff, donated_cutoff),
                )
                donation_ids = [row[0] for row in cur.fetchall()]
                donations_deidentified = len(donation_ids)

                if donation_ids:
                    cur.execute(
                        """
                        UPDATE donated_track_points
                        SET recorded_ms = (recorded_ms / %s) * %s
                        WHERE donation_id = ANY(%s)
                        """,
                        (_MS_PER_MINUTE, _MS_PER_MINUTE, donation_ids),
                    )
                    points_coarsened = cur.rowcount

            # ride_routes (A3, sql/052) — guarded existence probe; see the
            # docstring. Must run whether or not any donation matched above.
            cur.execute("SELECT to_regclass('ride_routes')")
            (ride_routes_relid,) = cur.fetchone()
            if ride_routes_relid is not None:
                if dry_run:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM ride_routes
                        WHERE deidentified_at IS NULL AND created_at < %s
                        """,
                        (donated_cutoff,),
                    )
                    (ride_routes_deidentified,) = cur.fetchone()
                else:
                    cur.execute(
                        """
                        UPDATE ride_routes
                        SET account_id = NULL, tracked_ride_id = NULL, deidentified_at = NOW()
                        WHERE deidentified_at IS NULL AND created_at < %s
                        """,
                        (donated_cutoff,),
                    )
                    ride_routes_deidentified = cur.rowcount

        if not dry_run:
            conn.commit()

    log.info(
        "deidentify_donations: donations=%d points_coarsened=%d ride_routes=%d dry_run=%s",
        donations_deidentified, points_coarsened, ride_routes_deidentified, dry_run,
    )
    return {
        "donations_deidentified": donations_deidentified,
        "points_coarsened": points_coarsened,
        "ride_routes_deidentified": ride_routes_deidentified,
        "dry_run": dry_run,
    }


COMMANDS = {
    "ingest_cycle":          _cli_ingest_cycle,
    "archive_if_due":        _cli_archive_if_due,
    "daily_sla":             _cli_daily_sla,
    "daily_trips":           _cli_daily_trips,
    "cleanup_receipts":      cleanup_receipts,
    "cleanup_ride_screenshots": cleanup_ride_screenshots,
    "cleanup_model_report_photos": cleanup_model_report_photos,
    "backfill_public_usernames": backfill_public_usernames,
    "expire_stale_watches":  expire_stale_watches,
    "expire_stale_off_feed_rides": expire_stale_off_feed_rides,
    "fetch_map_pbf":         _cli_fetch_map_pbf,
    "refresh_routing_graph": _cli_refresh_routing_graph,
    "fetch_photon_index":    _cli_fetch_photon_index,
    "refresh_photon_index":  _cli_refresh_photon_index,
    "extract_battery_trips": _cli_extract_battery_trips,
    "train_battery_model":   _cli_train_battery_model,
    "backfill_battery_trips": _cli_backfill_battery_trips,
    "poll_comms_replies":    poll_comms_replies,
    "deidentify_donations":  deidentify_donations,
    "recompute_area_leaders": _cli_recompute_area_leaders,
    "process_device_feature_reports": process_device_feature_reports,
    "migrate":               lambda: run_migrations(),
}


def admin_cli(sub_args: list[str]) -> int:
    """`python -m src.cli admin <list|add|remove> [email]` — manage the
    admin allowlist (the ADMIN_EMAILS replacement, sql/021). Same table the
    /admin/admins portal page edits."""
    from .accounts import add_admin, list_admins, remove_admin

    usage = "usage: python -m src.cli admin (list | add <email> | remove <email>)"
    if not sub_args:
        print(usage, file=sys.stderr)
        return 2
    action, rest = sub_args[0], sub_args[1:]

    if action == "list":
        rows = list_admins()
        if not rows:
            print("(no admins in the allowlist)")
        for r in rows:
            print(f"{r['email']}\t(added_by={r['added_by'] or '-'}, at={r['added_at'] or '-'})")
        return 0

    if action in ("add", "remove"):
        if len(rest) != 1:
            print(usage, file=sys.stderr)
            return 2
        email = rest[0]
        if action == "add":
            try:
                added = add_admin(email, added_by="cli")
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            print(f"{'added' if added else 'already present'}: {email}")
        else:
            removed = remove_admin(email)
            print(f"{'removed' if removed else 'not found'}: {email}")
        return 0

    print(usage, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sentry_init()

    args = argv if argv is not None else sys.argv[1:]

    # `admin` takes sub-arguments (list/add/remove <email>); the cron
    # commands are strictly zero-arg.
    if args and args[0] == "admin":
        try:
            return admin_cli(args[1:])
        except Exception as e:  # noqa: BLE001
            log.exception("cli command admin failed")
            capture_exception(e)
            return 1

    if len(args) != 1 or args[0] not in COMMANDS:
        choices = " | ".join(sorted(COMMANDS))
        print(f"usage: python -m src.cli ({choices} | admin ...)", file=sys.stderr)
        return 2

    cmd = args[0]
    try:
        result = COMMANDS[cmd]()
        log.info("cli command %s done: %r", cmd, result)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("cli command %s failed", cmd)
        capture_exception(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
