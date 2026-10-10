"""How long a negative report counts, and what clears it — the owner's rules
of 2026-10-09 — against real Postgres.

  * signed in  -> high risk until CLEARED; no time limit;
  * anonymous  -> high risk for 24 h, then "unknown", never "ok" until cleared;
  * rideability (not_rideable, damaged, dead_battery) clears on a >= 100 m
    move AND a charge rise, or on reappearing >= 100 m away with a FULL
    battery after going off the map;
  * location (inaccessible, not_found) clears on a >= 100 m move, or on
    reappearing >= 100 m away after going off the map;
  * a move under 100 m never clears; time never clears;
  * an admin resolve clears; a reconfirmation re-baselines.

These run the REAL SQL — fleet_reports.negative_state_sql, the one builder
/devices/current and /h3 embed — against one synthetic telemetry row, which
is the only honest way to test it: the predicate spans device_reports,
negative_reports, device_state, device_history and the telemetry row.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src.fleet_reports import (  # noqa: E402
    charge_rise_meters, full_battery_meters, negative_state_sql,
)
from src.quality import full_charge_range_meters  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
FULL = full_charge_range_meters()
FULL95 = full_battery_meters()
HALF = FULL // 2
RISE = charge_rise_meters()
VID = "0123456789abcdef"
A = (39.7392, -104.9903)
DEG_PER_M = 1 / 111_195.0          # latitude degrees per metre


def north(m: float, of=A) -> tuple[float, float]:
    return (of[0] + m * DEG_PER_M, of[1])


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg():
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        for t in ("device_reports", "negative_reports", "device_state"):
            cur.execute(f"DELETE FROM {t} WHERE vehicle_identifier = %s", (VID,))
        cur.execute("DELETE FROM device_history WHERE vehicle_identifier = %s", (VID,))
    conn.commit()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        for t in ("device_reports", "negative_reports", "device_state", "device_history"):
            cur.execute(f"DELETE FROM {t} WHERE vehicle_identifier = %s", (VID,))
    conn.commit()
    conn.close()


def _account(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (email) VALUES (%s) RETURNING id",
                    (f"hold-{uuid.uuid4().hex[:8]}@example.test",))
        return cur.fetchone()[0]


def _report(conn, *, account_id, at=NOW - timedelta(days=3), report_type="not_rideable",
            at_pos=A, range_at_report=HALF, resolved_at=None, **baseline) -> int:
    cols = {"vehicle_identifier": VID, "report_type": report_type, "reported_at": at,
            "account_id": account_id, "range_at_report_meters": range_at_report,
            "resolved_at": resolved_at,
            "resolution_source": "admin" if resolved_at else None,
            "vehicle_lat_at_report": at_pos[0] if at_pos else None,
            "vehicle_lon_at_report": at_pos[1] if at_pos else None, **baseline}
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO device_reports ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
            list(cols.values()))
        rid = cur.fetchone()[0]
    conn.commit()
    return rid


def _pin(conn, *, at, pos=A):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO negative_reports (vehicle_identifier, reported_at, report_lat, "
            "report_lon, h3_8_index, h3_9_index, h3_10_index) VALUES (%s, %s, %s, %s, 1, 1, 1)",
            (VID, at, pos[0], pos[1]))
    conn.commit()


def _at(conn, pos, *, last_seen=NOW):
    """The vehicle's current position (device_state), as the ingest left it."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO device_state (vehicle_identifier, current_lat, current_lon, "
            "first_observed_at_location, first_ever_observed_at, last_observed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (vehicle_identifier) DO UPDATE SET current_lat = EXCLUDED.current_lat, "
            "current_lon = EXCLUDED.current_lon, last_observed_at = EXCLUDED.last_observed_at",
            (VID, pos[0], pos[1], NOW - timedelta(days=5), NOW - timedelta(days=90), last_seen))
    conn.commit()


def _off_map(conn, *, last_seen_at, last_pos=A):
    """A stop closed because the vehicle left the feed (sql/083 'absent')."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO device_history (vehicle_identifier, snapshot_time, departed_at, lat, "
            "lon, spatial_status, device_id_observed, departure_reason) "
            "VALUES (%s, %s, %s, %s, %s, 'denver_core', 'x', 'absent')",
            (VID, last_seen_at - timedelta(days=1), last_seen_at, last_pos[0], last_pos[1]))
    conn.commit()


def _state(conn, *, range_meters=HALF):
    """'high' | 'unknown' | None, from the shipped builder."""
    sql = f"""
        SELECT {negative_state_sql(vid="r.vehicle_identifier",
                                   current_range="r.current_range_meters",
                                   now="%(now)s")}
          FROM (SELECT %(vid)s::text AS vehicle_identifier,
                       %(range)s::int AS current_range_meters) r
          LEFT JOIN device_state ds USING (vehicle_identifier)
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"now": NOW, "vid": VID, "range": range_meters})
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Time: signed in never clears; anonymous fades, never to "ok"
# ---------------------------------------------------------------------------

def test_a_signed_in_report_never_clears_on_time(pg):
    _at(pg, A)
    _report(pg, account_id=_account(pg), at=NOW - timedelta(days=400))
    assert _state(pg) == "high"


def test_an_anonymous_report_is_high_for_24h_then_unknown_never_cleared(pg):
    _at(pg, A)
    rid = _report(pg, account_id=None, at=NOW - timedelta(hours=2))
    assert _state(pg) == "high"
    with pg.cursor() as cur:
        cur.execute("UPDATE device_reports SET reported_at = %s WHERE id = %s",
                    (NOW - timedelta(hours=25), rid))
    pg.commit()
    assert _state(pg) == "unknown"
    with pg.cursor() as cur:
        cur.execute("UPDATE device_reports SET reported_at = %s WHERE id = %s",
                    (NOW - timedelta(days=60), rid))
    pg.commit()
    assert _state(pg) == "unknown"          # time alone never clears it
    _at(pg, north(150))                     # cleared by the rules, not the clock
    assert _state(pg, range_meters=HALF + RISE) is None


def test_a_map_pin_fades_to_unknown_and_clears_by_the_rules(pg):
    _at(pg, A)
    _pin(pg, at=NOW - timedelta(hours=1))
    assert _state(pg) == "high"
    with pg.cursor() as cur:
        cur.execute("UPDATE negative_reports SET reported_at = %s WHERE vehicle_identifier = %s",
                    (NOW - timedelta(days=3), VID))
    pg.commit()
    assert _state(pg) == "unknown"
    _off_map(pg, last_seen_at=NOW - timedelta(hours=10))
    _at(pg, north(300))
    assert _state(pg, range_meters=FULL95) is None


# ---------------------------------------------------------------------------
# Movement: under 100 m never clears
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("report_type", ["not_rideable", "damaged", "dead_battery",
                                         "inaccessible", "not_found"])
def test_a_move_under_100m_never_clears_even_with_a_full_recharge(pg, report_type):
    _report(pg, account_id=_account(pg), report_type=report_type, range_at_report=1000)
    _at(pg, north(90))
    assert _state(pg, range_meters=FULL) == "high"


@pytest.mark.parametrize("report_type", ["not_rideable", "damaged", "dead_battery"])
def test_a_100m_move_without_a_rise_does_not_clear_a_rideability_report(pg, report_type):
    _report(pg, account_id=_account(pg), report_type=report_type)
    _at(pg, north(150))
    assert _state(pg, range_meters=HALF) == "high"
    assert _state(pg, range_meters=HALF + RISE - 1) == "high"   # under the threshold
    assert _state(pg, range_meters=HALF + RISE) is None          # move + rise


@pytest.mark.parametrize("report_type", ["inaccessible", "not_found"])
def test_a_100m_move_clears_a_location_report_with_no_battery_condition(pg, report_type):
    _report(pg, account_id=_account(pg), report_type=report_type)
    _at(pg, north(150))
    assert _state(pg, range_meters=HALF - 5000) is None


def test_a_rise_without_a_move_does_not_clear(pg):
    _report(pg, account_id=_account(pg), range_at_report=1000)
    _at(pg, A)
    assert _state(pg, range_meters=FULL) == "high"


def test_a_device_at_100_percent_can_be_reported_and_the_report_stands(pg):
    # §2.4, the Apollo behind the fence: no rise is possible past full, and a
    # move alone is not servicing.
    _report(pg, account_id=_account(pg), range_at_report=FULL)
    _at(pg, A)
    assert _state(pg, range_meters=FULL) == "high"
    _at(pg, north(200))
    assert _state(pg, range_meters=FULL) == "high"


def test_no_recorded_charge_counts_a_rise_only_at_full(pg):
    _report(pg, account_id=_account(pg), range_at_report=None)
    _at(pg, north(150))
    assert _state(pg, range_meters=HALF) == "high"
    assert _state(pg, range_meters=FULL95) is None


def test_an_unknown_position_or_battery_clears_nothing(pg):
    _report(pg, account_id=_account(pg))
    assert _state(pg, range_meters=FULL) == "high"           # no device_state row
    _at(pg, north(150))
    assert _state(pg, range_meters=None) == "high"            # no charge reading
    _report(pg, account_id=_account(pg), report_type="inaccessible", at_pos=None)
    assert _state(pg, range_meters=HALF + RISE) == "high"     # no baseline to move from


# ---------------------------------------------------------------------------
# Off the map and back
# ---------------------------------------------------------------------------

def test_off_map_and_back_far_away_full_clears_a_rideability_report(pg):
    _report(pg, account_id=_account(pg), range_at_report=FULL)
    _off_map(pg, last_seen_at=NOW - timedelta(hours=20))
    _at(pg, north(400))
    assert _state(pg, range_meters=FULL95 - 1) == "high"     # not full: stands
    assert _state(pg, range_meters=FULL95) is None


def test_off_map_and_back_far_away_clears_a_location_report_at_any_charge(pg):
    _report(pg, account_id=_account(pg), report_type="inaccessible")
    _off_map(pg, last_seen_at=NOW - timedelta(hours=20), last_pos=north(60))
    _at(pg, north(170))                 # 110 m from where it was last seen
    assert _state(pg, range_meters=1000) is None


def test_off_map_and_back_at_the_same_spot_clears_nothing(pg):
    _report(pg, account_id=_account(pg), report_type="inaccessible")
    _off_map(pg, last_seen_at=NOW - timedelta(hours=20))
    _at(pg, north(40))
    assert _state(pg, range_meters=FULL) == "high"


def test_an_absence_before_the_report_does_not_count(pg):
    _report(pg, account_id=_account(pg), at=NOW - timedelta(days=3))
    _off_map(pg, last_seen_at=NOW - timedelta(days=4), last_pos=north(-500))
    _at(pg, A)
    assert _state(pg, range_meters=FULL) == "high"


# ---------------------------------------------------------------------------
# Verification, re-baselining, and what is not negative
# ---------------------------------------------------------------------------

def test_a_resolved_report_counts_for_nothing(pg):
    _at(pg, A)
    _report(pg, account_id=_account(pg), resolved_at=NOW)
    _report(pg, account_id=None, at=NOW - timedelta(hours=2), resolved_at=NOW)
    assert _state(pg) is None


def test_a_reconfirmation_rebaselines_position_and_charge(pg):
    b = north(500)
    _report(pg, account_id=_account(pg), range_at_report=1000,
            baseline_lat=b[0], baseline_lon=b[1], baseline_range_meters=HALF,
            baseline_at=NOW - timedelta(hours=1))
    _at(pg, north(50, of=b))            # 550 m from the original spot, 50 from the new
    assert _state(pg, range_meters=FULL) == "high"
    _at(pg, north(150, of=b))
    assert _state(pg, range_meters=HALF + RISE - 1) == "high"   # rise is from the new charge
    assert _state(pg, range_meters=HALF + RISE) is None


def test_a_pending_baseline_cannot_clear(pg):
    _report(pg, account_id=_account(pg), report_type="inaccessible", baseline_pending=True)
    _at(pg, north(300))
    assert _state(pg) == "high"


def test_improperly_parked_changes_no_label(pg):
    _at(pg, A)
    _report(pg, account_id=_account(pg), report_type="improperly_parked")
    _report(pg, account_id=None, at=NOW - timedelta(hours=1), report_type="improperly_parked")
    assert _state(pg) is None


# ---------------------------------------------------------------------------
# Servicing in the history (sql/104) and legacy reports (owner, 2026-10-10)
# ---------------------------------------------------------------------------

def _serviced(conn, at):
    with conn.cursor() as cur:
        cur.execute("UPDATE device_state SET last_serviced_at = %s WHERE vehicle_identifier = %s",
                    (at, VID))
    conn.commit()


def _moves(conn, n, *, since, metres=150):
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO trip_events (vehicle_identifier, detected_at, from_lat, from_lon, "
                "to_lat, to_lon, distance_meters) VALUES (%s, %s, 0, 0, 0, 0, %s)",
                (VID, since + timedelta(hours=i + 1), metres))
    conn.commit()


@pytest.fixture()
def trips(pg):
    yield pg
    with pg.cursor() as cur:
        cur.execute("DELETE FROM trip_events WHERE vehicle_identifier = %s", (VID,))
    pg.commit()


def test_servicing_since_the_report_plus_a_move_clears_even_after_riding_down(pg):
    """Swapped to full after the report, then ridden back below the charge at
    report time: the snapshot shows no rise, the history does."""
    _report(pg, account_id=_account(pg), range_at_report=HALF)
    _at(pg, north(150))
    assert _state(pg, range_meters=HALF - 3000) == "high"
    _serviced(pg, NOW - timedelta(days=1))
    assert _state(pg, range_meters=HALF - 3000) is None


def test_servicing_needs_the_move_and_must_follow_the_report(pg):
    _report(pg, account_id=_account(pg), at=NOW - timedelta(days=3))
    _at(pg, north(90))
    _serviced(pg, NOW - timedelta(days=1))
    assert _state(pg) == "high"                       # under 100 m: never
    _at(pg, north(150))
    _serviced(pg, NOW - timedelta(days=4))            # before the report
    assert _state(pg) == "high"


def test_servicing_counts_from_a_rebaseline_not_the_original_report(pg):
    b = north(500)
    _report(pg, account_id=_account(pg), baseline_lat=b[0], baseline_lon=b[1],
            baseline_range_meters=HALF, baseline_at=NOW - timedelta(hours=2))
    _at(pg, north(150, of=b))
    _serviced(pg, NOW - timedelta(hours=5))           # before "still a problem"
    assert _state(pg) == "high"
    _serviced(pg, NOW - timedelta(hours=1))
    assert _state(pg) is None


def test_a_legacy_report_clears_after_three_100m_moves(trips):
    pg = trips
    at = NOW - timedelta(days=30)                     # NOW is before charge capture
    _report(pg, account_id=_account(pg), at=at, range_at_report=None)
    _at(pg, A)
    _moves(pg, 2, since=at)
    _moves(pg, 5, since=at, metres=90)                # short moves never count
    _moves(pg, 3, since=at - timedelta(days=10))      # moves before the report don't
    assert _state(pg) == "high"
    _moves(pg, 1, since=at + timedelta(days=1))
    assert _state(pg) is None


def test_the_three_move_rule_is_only_for_legacy_reports(trips):
    """Filed after charge capture, or with a charge recorded: the normal
    rules apply, however much the vehicle has moved."""
    pg = trips
    after = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
    now = after + timedelta(days=2)
    _report(pg, account_id=_account(pg), at=after, range_at_report=None)
    _at(pg, A)
    _moves(pg, 5, since=after)
    sql = f"""
        SELECT {negative_state_sql(vid="r.vehicle_identifier",
                                   current_range="r.current_range_meters", now="%(now)s")}
          FROM (SELECT %(vid)s::text AS vehicle_identifier, %(range)s::int AS current_range_meters) r
          LEFT JOIN device_state ds USING (vehicle_identifier)"""
    with pg.cursor() as cur:
        cur.execute(sql, {"now": now, "vid": VID, "range": HALF})
        assert cur.fetchone()[0] == "high"
    with pg.cursor() as cur:
        cur.execute("DELETE FROM device_reports WHERE vehicle_identifier = %s", (VID,))
    pg.commit()
    before = NOW - timedelta(days=30)
    _report(pg, account_id=_account(pg), at=before, range_at_report=HALF)
    _moves(pg, 5, since=before)
    assert _state(pg) == "high"                       # a charge was recorded
