"""One-off cleanup of ghost stops, and a dry run that shows what it buys.

`python -m src.cli close_ghost_stops [--dry-run] [YYYY-MM-DD ...]`

WHAT A GHOST STOP IS -------------------------------------------------------
An open `device_history` row (departed_at NULL) for a vehicle that is no
longer in the GBFS feed. Before sql/083 a stop closed only when the vehicle
was seen somewhere else, so a vehicle pulled for repair, retired or re-keyed
stayed "parked" forever. src/device_state.py now closes these as they happen,
every cycle (see ABSENCE there). This module closes the backlog that built up
before that, under the SAME rule (`device_state.absence_window` with no lower
bound, and `device_state.close_absent_stops`):

    an open stop whose vehicle has been out of the feed for longer than
    ABSENT_STOP_AFTER, and has missed at least ABSENT_MIN_MISSED_CYCLES
    observed cycles, gets departed_at = the vehicle's last observed time,
    departure_reason = 'absent'.

"Observed cycles" are the ones device_state itself processed and counted
(sql/086's device_state_processed_cycles), exactly as for the per-cycle
sweep, so this backstop respects the same guards: run while the updater has
been failing, it cannot close the stops of vehicles that were in those feeds,
and an empty or implausibly small payload is never one of the missed cycles.
It also means that right after sql/086 is applied it closes nothing until
ABSENT_MIN_MISSED_CYCLES cycles have been processed (about 10 minutes).

Idempotent: a closed stop no longer matches, so a second run closes only
what has newly gone absent since. The run changes nothing else. It does not
touch device_state, trip_events, or any stored compliance figure. In
particular it does NOT recompute the `*_equity` columns or
daily_sla_compliance rows of days already reprocessed: the scheduled
`reprocess_equity_compliance` fills only days whose figure is still NULL.

DRY RUN --------------------------------------------------------------------
`--dry-run` writes nothing and takes no locks, so it runs on a read-only
session. It reports how many stops would close, and for each requested day
(default: the five Denver-local days before today) what the Equity Area
reconstruction (src/equity_backfill.py) would see before and after the
closure, over the 6-9 AM SLA window:
  * recorded vs reconstructed fleet and their ratio (fidelity);
  * how many snapshots pass the ±DEFAULT_MAX_DRIFT gate (which is NOT
    widened here, or anywhere);
  * the gated Equity Area percentage a real reprocess would store, next to
    the live figure the day actually recorded.
The "after" side applies the closures in memory to the same loaded stops,
the same way the database would present them after the real run.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, Iterable

from . import daily_sla, device_state, equity_backfill
from .pg import connection

log = logging.getLogger(__name__)

DEFAULT_PREVIEW_DAYS = 5

#: Absence-age buckets for the report, as (label, lower bound).
_AGE_BUCKETS: tuple[tuple[str, timedelta], ...] = (
    (">30d", timedelta(days=30)),
    ("7-30d", timedelta(days=7)),
    ("1-7d", timedelta(days=1)),
    ("6-24h", timedelta(hours=6)),
    ("<6h", timedelta(0)),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_bucket(age: timedelta) -> str:
    for label, lower in _AGE_BUCKETS:
        if age >= lower:
            return label
    return _AGE_BUCKETS[-1][0]


def _summarise(candidates: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    by_age = {label: 0 for label, _ in reversed(_AGE_BUCKETS)}
    for c in candidates:
        by_age[_age_bucket(now - c["departed_at"])] += 1
    return {
        "stops": len(candidates),
        "stops_denver_core": sum(1 for c in candidates if c["spatial_status"] == "denver_core"),
        "by_time_since_last_seen": by_age,
    }


def _rule(window: device_state.AbsenceWindow | None) -> dict[str, Any]:
    return {
        "absent_after_hours": device_state.ABSENT_STOP_AFTER.total_seconds() / 3600,
        "min_missed_cycles": device_state.ABSENT_MIN_MISSED_CYCLES,
        "observed_cycles": "device_state_processed_cycles (counts_as_observation)",
        "cutoff": window.cutoff.isoformat() if window else None,
    }


def default_preview_days(today: date_cls | None = None) -> list[date_cls]:
    today = today or datetime.now(daily_sla.DENVER_TZ).date()
    return [today - timedelta(days=i) for i in range(DEFAULT_PREVIEW_DAYS, 0, -1)]


# ---------------------------------------------------------------------------
# Fidelity preview (dry run only)
# ---------------------------------------------------------------------------
def apply_closures(
    stops: Iterable[equity_backfill.Stop],
    closures: dict[tuple[str, datetime], datetime],
) -> list[equity_backfill.Stop]:
    """`stops` as they will read once `closures` are written.

    `closures` maps (vehicle_identifier, arrived) of an OPEN stop to its new
    departed_at. Only open stops are touched. A vehicle has at most one open
    stop, so the key is unambiguous.
    """
    out = []
    for s in stops:
        new = closures.get((s.vehicle_identifier, s.arrived)) if s.departed is None else None
        out.append(replace(s, departed=new) if new is not None else s)
    return out


def _side(stops: list[equity_backfill.Stop], snapshots: list[dict[str, Any]],
          max_drift: float) -> dict[str, Any]:
    fid: list[float] = []
    passing_pcts: list[float] = []
    passing = 0
    recon_sizes: list[int] = []
    for snap in snapshots:
        fleet = equity_backfill.fleet_at(stops, snap["snapshot_time"])
        recon_sizes.append(len(fleet))
        recorded = snap["total_devices_denver"]
        if not fleet or not recorded:
            continue
        f = len(fleet) / recorded
        fid.append(f)
        if abs(f - 1.0) <= max_drift:
            passing += 1
            pct = equity_backfill.rebuild_metrics(fleet)["percent_all_devices_equity"]
            if pct is not None:
                passing_pcts.append(pct)
    return {
        "reconstructed_fleet_mean": round(sum(recon_sizes) / len(recon_sizes), 1) if recon_sizes else None,
        "fidelity_mean": round(sum(fid) / len(fid), 4) if fid else None,
        "fidelity_min": round(min(fid), 4) if fid else None,
        "fidelity_max": round(max(fid), 4) if fid else None,
        "snapshots_passing_gate": passing,
        # What a real reprocess would store for the day: daily_sla's plain
        # mean over the gate-passing snapshots in the window.
        "gated_percent_all_devices_equity": (
            round(sum(passing_pcts) / len(passing_pcts), 2) if passing_pcts else None
        ),
    }


def _live_figure(d: date_cls) -> float | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT avg_percent_all_devices_equity FROM daily_sla_compliance "
                "WHERE sla_date = %s",
                (d,),
            )
            row = cur.fetchone()
        conn.rollback()
    return None if row is None or row[0] is None else float(row[0])


def preview_day(
    d: date_cls,
    closures: dict[tuple[str, datetime], datetime],
    *,
    max_drift: float = equity_backfill.DEFAULT_MAX_DRIFT,
) -> dict[str, Any]:
    """Fidelity and gated equity figure for one day's SLA window, before and
    after `closures`. Reads only."""
    start, end = daily_sla.window_for_date(d)
    snapshots = equity_backfill._load_snapshots(start, end)
    out: dict[str, Any] = {
        "sla_date": d.isoformat(),
        "snapshots": len(snapshots),
        "recorded_fleet_mean": (
            round(sum(s["total_devices_denver"] or 0 for s in snapshots) / len(snapshots), 1)
            if snapshots else None
        ),
        "stored_percent_all_devices_equity": _live_figure(d),
    }
    if not snapshots:
        return out
    before = equity_backfill.tag_equity_membership(equity_backfill._load_stops(start, end))
    after = apply_closures(before, closures)
    out["before"] = _side(before, snapshots, max_drift)
    out["after"] = _side(after, snapshots, max_drift)
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def dry_run(days: list[date_cls] | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Report what `run()` would close and what that does to reconstruction
    fidelity. Writes nothing and takes no locks."""
    now = now or _now()
    with connection() as conn:
        with conn.cursor() as cur:
            window = device_state.absence_window(cur, now, bounded=False)
            candidates = device_state.absent_stop_candidates(cur, window)
        conn.rollback()

    closures = {(c["vehicle_identifier"], c["arrived"]): c["departed_at"] for c in candidates}
    days = days if days is not None else default_preview_days()
    return {
        "dry_run": True,
        "as_of": now.isoformat(),
        "rule": _rule(window),
        "would_close": _summarise(candidates, now),
        "days": [preview_day(d, closures) for d in days],
    }


def run(*, now: datetime | None = None) -> dict[str, Any]:
    """Close every ghost stop, in one transaction. Idempotent."""
    now = now or _now()
    with connection() as conn:
        with conn.cursor() as cur:
            window = device_state.absence_window(cur, now, bounded=False)
            candidates = device_state.absent_stop_candidates(cur, window)
            closed = device_state.close_absent_stops(cur, window)
        conn.commit()
    result = {
        "dry_run": False,
        "as_of": now.isoformat(),
        "rule": _rule(window),
        "closed": closed,
        "matched": _summarise(candidates, now),
    }
    log.info("close_ghost_stops: %r", result)
    return result
