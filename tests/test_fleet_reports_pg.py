"""Fleet reports Phase 1 against real Postgres (docs/FLEET_REPORTS_PLAN.md §5).

What only a real database can show:

  * the owner's 2026-10-09 rules end to end through /api/v1/devices/current:
    a negative report sets the reliability label and never hides a scooter
    (the rule-by-rule SQL tests are tests/test_negative_report_hold_pg.py);
  * the identify extension (`/vehicles/resolve?explain=true`) reading
    device_state for vehicles the feed no longer carries, and the four
    reasons it answers with;
  * the census lists, the acknowledgement table and its survival across an
    ingest rewrite and a reappearance;
  * the export's counts;
  * sql/100's constraint and its replay safety.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
NEVER point that at production: the fixture executes every migration.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import (  # noqa: E402
    api_fleet_reports, api_public, api_vehicle_plates, dwell_stats, fleet_reports,
)
from src.accounts import SessionUser, require_admin  # noqa: E402
from src.identity import hash_plate  # noqa: E402
from src.quality import full_charge_range_meters  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

# Its own far-future instant, distinct from every other suite's, so this
# cycle is "the latest complete" for the test and nothing else's is.
SNAP = datetime(2099, 6, 1, 12, 0, tzinfo=timezone.utc)
FULL = full_charge_range_meters()
HALF = FULL // 2
_BUCKETS = ("vehicle_resolve_ip",)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


class _Fleet:
    """Builds one cycle plus device_state / reports / feature rows, and
    remembers everything it made so teardown removes exactly that."""

    def __init__(self, conn):
        self.conn = conn
        self.cycle = uuid.uuid4()
        self.vids: list[str] = []
        self.accounts: list[int] = []
        with conn.cursor() as cur:
            cur.execute("INSERT INTO observation_cycles (cycle_id, job_status) "
                        "VALUES (%s, 'complete')", (self.cycle,))
            cur.execute("INSERT INTO snapshot_metadata_core (cycle_id, snapshot_time) "
                        "VALUES (%s, %s)", (self.cycle, SNAP))
        conn.commit()

    def account(self, email: str | None = None) -> int:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO accounts (email) VALUES (%s) RETURNING id",
                        (email or f"fr-{uuid.uuid4().hex[:10]}@example.test",))
            aid = cur.fetchone()[0]
        self.conn.commit()
        self.accounts.append(aid)
        return aid

    def vehicle(self, plate: str, *, in_feed: bool = True, range_m: int | None = HALF,
                parked_since: datetime = SNAP - timedelta(days=10),
                first_ever: datetime = SNAP - timedelta(days=100),
                last_seen: datetime | None = None, device_id: str | None = None,
                features: dict | None = None) -> str:
        vid = hash_plate(plate)
        self.vids.append(vid)
        last_seen = last_seen or (SNAP if in_feed else SNAP - timedelta(days=5))
        f = features or {}
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_state (
                    vehicle_identifier, vehicle_plate, current_device_id,
                    current_lat, current_lon, current_form_factor,
                    first_observed_at_location, first_ever_observed_at,
                    last_observed_at, current_vehicle_model_name,
                    feature_status, has_bell, has_cup_holder, has_phone_holder,
                    has_basket, features_poor_condition
                ) VALUES (%s, %s, %s, 39.7392123, -104.9903456, 'scooter',
                          %s, %s, %s, 'Apollo', %s, %s, %s, %s, %s, %s)
                """,
                (vid, plate, device_id or f"bike-{plate}", parked_since, first_ever,
                 last_seen, f.get("status", "needs_features_confirmed"),
                 f.get("bell"), f.get("cup_holder"), f.get("phone_holder"),
                 f.get("basket"), f.get("poor")),
            )
            if in_feed:
                cur.execute(
                    """
                    INSERT INTO raw_telemetry_points (
                        cycle_id, snapshot_time, device_id, form_factor,
                        latitude, longitude, spatial_status,
                        vehicle_identifier, vehicle_plate, current_range_meters,
                        h3_10_index, is_disabled, is_reserved
                    ) VALUES (%s, %s, %s, 'scooter', 39.7392, -104.9903,
                              'denver_core', %s, %s, %s, 622236750537375743,
                              false, false)
                    """,
                    (self.cycle, SNAP, device_id or f"bike-{plate}", vid, plate, range_m),
                )
        self.conn.commit()
        return vid

    def report(self, vid: str, report_type: str, *, account_id: int | None,
               at: datetime = SNAP - timedelta(days=3), range_at_report: int | None = HALF,
               reason: str | None = None, submitted_reason: str | None = None,
               observed_at: datetime | None = None,
               at_pos: tuple[float, float] | None = (39.7392123, -104.9903456)) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_reports (
                    vehicle_identifier, report_type, reported_at, account_id,
                    range_at_report_meters, reason, submitted_reason, observed_at,
                    h3_10_index, vehicle_lat_at_report, vehicle_lon_at_report
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 622236750537375743, %s, %s)
                RETURNING id
                """,
                (vid, report_type, at, account_id, range_at_report, reason,
                 submitted_reason, observed_at or at,
                 at_pos[0] if at_pos else None, at_pos[1] if at_pos else None),
            )
            rid = cur.fetchone()[0]
        self.conn.commit()
        return rid

    def move(self, vid: str, metres_north: float, *, range_m: int | None = None):
        """Put the vehicle `metres_north` of the fixture's spot (device_state,
        as the ingest leaves it), optionally with a new charge."""
        with self.conn.cursor() as cur:
            cur.execute("UPDATE device_state SET current_lat = %s, current_lon = %s, "
                        "first_observed_at_location = %s WHERE vehicle_identifier = %s",
                        (39.7392123 + metres_north / 111_195.0, -104.9903456,
                         SNAP - timedelta(hours=1), vid))
            if range_m is not None:
                cur.execute("UPDATE raw_telemetry_points SET current_range_meters = %s "
                            "WHERE cycle_id = %s AND vehicle_identifier = %s",
                            (range_m, self.cycle, vid))
        self.conn.commit()

    def cleanup(self):
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM device_census_ack WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM device_reports WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM raw_telemetry_points WHERE cycle_id = %s", (self.cycle,))
            cur.execute("DELETE FROM device_state WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM snapshot_metadata_core WHERE cycle_id = %s", (self.cycle,))
            cur.execute("DELETE FROM observation_cycles WHERE cycle_id = %s", (self.cycle,))
            cur.execute("DELETE FROM rate_limit_events WHERE bucket = ANY(%s)", (list(_BUCKETS),))
            cur.execute("DELETE FROM accounts WHERE id = ANY(%s)", (self.accounts,))
        self.conn.commit()


@pytest.fixture()
def fleet(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — fleet reports Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
        cur.execute("DELETE FROM rate_limit_events WHERE bucket = ANY(%s)", (list(_BUCKETS),))
    conn.commit()

    @contextmanager
    def _per_request_connection():
        with psycopg.connect(dsn) as c:
            yield c

    for mod in (api_vehicle_plates, api_fleet_reports, api_public, dwell_stats):
        monkeypatch.setattr(mod, "connection", _per_request_connection)
    dwell_stats._cache.clear()
    f = _Fleet(conn)
    try:
        yield f
    finally:
        f.cleanup()
        conn.close()


def _admin_client(fleet, router) -> TestClient:
    admin_id = fleet.account("fr-admin@example.test")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: SessionUser(
        account_id=admin_id, email="fr-admin@example.test", scopes=("rider",),
        expires_at=None, sliding=True, method="google", token_sha256="x")
    c = TestClient(app)
    c.admin_id = admin_id  # type: ignore[attr-defined]
    return c


def _public_client(router) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _devices(fleet) -> dict[str, dict]:
    r = _public_client(api_public.router).get("/api/v1/devices/current")
    assert r.status_code == 200, r.text
    return {f["properties"]["vehicle_identifier"]: f["properties"]
            for f in r.json()["features"]}


# ---------------------------------------------------------------------------
# Suppression on /devices/current, and its independence from reliability
# ---------------------------------------------------------------------------

def test_an_inaccessible_report_labels_high_risk_and_hides_nothing(fleet):
    # The Apollo behind the fence: 100%, signed-in inaccessible report. Owner,
    # 2026-10-09: "labeled as 'high risk', not hidden from the map".
    acct = fleet.account()
    apollo = fleet.vehicle("9100001", range_m=FULL)
    control = fleet.vehicle("9100002", range_m=FULL)
    fleet.report(apollo, "inaccessible", account_id=acct, range_at_report=FULL)
    d = _devices(fleet)
    assert apollo in d                                     # still on the map
    assert d[apollo]["reliability_tier"] == "high_risk"
    assert d[apollo]["has_negative_report"] is True
    assert d[apollo]["negative_report_risk"] == "high_risk"
    assert d[apollo]["negative_report_reason"] == "inaccessible"
    assert d[apollo]["negative_report_since"].startswith("2099-05-29")
    for gone in ("suppressed", "suppressed_reason", "suppressed_since"):
        assert gone not in d[apollo]
    assert d[control]["negative_report_risk"] is None
    assert d[control]["has_negative_report"] is False


def test_improperly_parked_changes_no_label(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100003")
    control = fleet.vehicle("9100013")
    fleet.report(v, "improperly_parked", account_id=acct)
    d = _devices(fleet)
    assert d[v]["negative_report_risk"] is None
    assert d[v]["has_negative_report"] is False
    assert d[v]["reliability_tier"] == d[control]["reliability_tier"]


def test_not_found_labels_high_risk(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100012")
    fleet.report(v, "not_found", account_id=acct)
    fleet.report(v, "improperly_parked", account_id=acct)
    d = _devices(fleet)
    assert d[v]["reliability_tier"] == "high_risk"
    assert d[v]["negative_report_reason"] == "not_found"


def test_the_reason_detail_names_why_it_wont_ride(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100014")
    fleet.report(v, "not_rideable", account_id=acct, reason="acceleration")
    d = _devices(fleet)[v]
    assert (d["negative_report_reason"], d["negative_report_reason_detail"]) == (
        "not_rideable", "acceleration")


def test_an_anonymous_report_fades_to_unknown_not_ok(fleet):
    # Parked an hour, so nothing but the report can make it high risk.
    v = fleet.vehicle("9100005", parked_since=SNAP - timedelta(hours=1))
    twin = fleet.vehicle("9100015", parked_since=SNAP - timedelta(hours=1))
    rid = fleet.report(v, "damaged", account_id=None, at=datetime.now(timezone.utc)
                       - timedelta(hours=2))
    d = _devices(fleet)[v]
    assert d["reliability_tier"] == "high_risk" and d["negative_report_risk"] == "high_risk"
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE device_reports SET reported_at = %s WHERE id = %s",
                    (datetime.now(timezone.utc) - timedelta(hours=30), rid))
    fleet.conn.commit()
    devices = _devices(fleet)
    d = devices[v]
    assert devices[twin]["reliability_tier"] == "ok"
    assert d["reliability_tier"] == "unknown"
    assert d["negative_report_risk"] == "unknown"
    assert d["has_negative_report"] is False


def test_a_signed_in_report_has_no_time_limit(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100004")
    fleet.report(v, "not_rideable", account_id=acct, at=SNAP - timedelta(days=300))
    assert fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)[v]["risk"] \
        == "high_risk"


def test_a_short_move_never_clears_and_a_long_one_needs_a_rise(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100006")
    fleet.report(v, "dead_battery", account_id=acct, range_at_report=1000)
    fleet.move(v, 60, range_m=FULL)                   # recharged, but 60 m
    assert v in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)
    fleet.move(v, 160, range_m=1000)                  # far, but no rise
    assert v in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)
    fleet.move(v, 160, range_m=FULL)                  # far and recharged
    assert v not in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)
    assert _devices(fleet)[v]["negative_report_risk"] is None


def test_a_long_move_clears_a_location_report(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100007")
    fleet.report(v, "inaccessible", account_id=acct)
    fleet.move(v, 99)
    assert v in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)
    fleet.move(v, 101)
    assert v not in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)


def test_latest_report_is_the_newest_uncleared_one_not_the_strongest(fleet):
    # Owner, 2026-10-09: "The most recent report should be displayed on the
    # scooter details tile."
    a = fleet.account()
    v = fleet.vehicle("9100030")
    fleet.report(v, "inaccessible", account_id=a, at=SNAP - timedelta(days=4))
    newest = fleet.report(v, "not_rideable", account_id=None, reason="flat_tire",
                          at=SNAP - timedelta(hours=3),
                          observed_at=SNAP - timedelta(hours=5))
    fleet.report(v, "improperly_parked", account_id=a, at=SNAP - timedelta(hours=1))
    d = _devices(fleet)[v]
    assert d["negative_report_reason"] == "inaccessible"         # strongest
    lr = d["latest_report"]
    assert lr == {"report_type": "not_rideable", "reason": "flat_tire",
                  "observed_at": (SNAP - timedelta(hours=5)).isoformat(),
                  "reported_at": (SNAP - timedelta(hours=3)).isoformat(),
                  "anonymous": True,                              # parking excluded
                  "serviced_since": None}                         # sql/107, none since
    # A cleared report drops out; the next newest takes its place.
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE device_reports SET resolved_at = NOW(), resolution_source = "
                    "'admin' WHERE id = %s", (newest,))
    fleet.conn.commit()
    lr = _devices(fleet)[v]["latest_report"]
    assert lr["report_type"] == "inaccessible" and lr["anonymous"] is False
    clean = fleet.vehicle("9100031")
    fleet.report(clean, "improperly_parked", account_id=a)
    assert _devices(fleet)[clean]["latest_report"] is None


def test_the_etag_changes_when_a_report_lands_mid_cycle(fleet):
    app = FastAPI()
    app.include_router(api_public.router)
    c = TestClient(app)
    v = fleet.vehicle("9100032")
    tag = c.get("/api/v1/devices/current").headers["etag"]
    assert c.get("/api/v1/devices/current",
                 headers={"If-None-Match": tag}).status_code == 304
    rid = fleet.report(v, "damaged", account_id=fleet.account())
    r = c.get("/api/v1/devices/current", headers={"If-None-Match": tag})
    assert r.status_code == 200 and r.headers["etag"] != tag
    tag2 = r.headers["etag"]
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE device_reports SET resolved_at = NOW(), resolution_source = "
                    "'admin' WHERE id = %s", (rid,))
    fleet.conn.commit()
    assert c.get("/api/v1/devices/current",
                 headers={"If-None-Match": tag2}).status_code == 200


def test_the_strongest_reason_wins_and_since_is_its_oldest_report(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9100009")
    fleet.report(v, "not_rideable", account_id=a, at=SNAP - timedelta(days=4))
    fleet.report(v, "inaccessible", account_id=a, at=SNAP - timedelta(days=2))
    fleet.report(v, "inaccessible", account_id=b, at=SNAP - timedelta(days=1))
    st = fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)[v]
    assert st["reason"] == "inaccessible"
    assert st["since"] == SNAP - timedelta(days=2)


def test_broken_parts_from_feature_confirmation_change_no_label(fleet):
    v = fleet.vehicle("9100010", features={
        "status": "up_to_date", "bell": True, "cup_holder": True,
        "phone_holder": False, "basket": True, "poor": ["bell", "cup_holder", "basket"]})
    d = _devices(fleet)
    assert d[v]["negative_report_risk"] is None
    assert d[v]["device_features"]  # still rendered as equipment


def test_resolving_a_report_clears_the_label_immediately_and_is_attributed(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100011")
    rid = fleet.report(v, "inaccessible", account_id=acct)
    assert v in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)
    admin = _admin_client(fleet, api_fleet_reports.router)
    r = admin.post(f"/api/v1/private/reports/{rid}/resolve",
                   json={"resolution": "void: the yard gate was open, I checked"})
    assert r.status_code == 200, r.text
    assert r.json()["resolved_by"] == "fr-admin@example.test"
    assert v not in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)
    with fleet.conn.cursor() as cur:
        cur.execute("SELECT resolved_by, resolution FROM device_reports WHERE id = %s", (rid,))
        by, why = cur.fetchone()
    assert by == admin.admin_id and why.startswith("void:")
    # Final: a second resolve is a conflict, an unknown id a 404.
    assert admin.post(f"/api/v1/private/reports/{rid}/resolve",
                      json={"resolution": "again"}).status_code == 409
    assert admin.post("/api/v1/private/reports/999999999/resolve",
                      json={"resolution": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# Identify: /vehicles/resolve?explain=true
# ---------------------------------------------------------------------------

def _resolve(**params):
    return _public_client(api_vehicle_plates.router).get(
        "/api/v1/vehicles/resolve", params=params)


def test_a_reported_device_resolves_from_a_scan_and_names_the_report(fleet):
    # §2.7's acceptance test, under the owner's 2026-10-09 rules: the Apollo
    # behind the fence is ON the map, labelled high risk, and a scan says why.
    acct = fleet.account()
    v = fleet.vehicle("9100020", range_m=FULL, device_id="bike-apollo")
    fleet.report(v, "inaccessible", account_id=acct, range_at_report=FULL)
    r = _resolve(qr="https://veo.example/unlock?number=9100020&src=sticker", explain="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "on_map"
    assert body["device_id"] == "bike-apollo"
    assert body["vehicle_identifier"] == v
    assert body["negative_report_risk"] == "high_risk"
    assert body["negative_report_reason"] == "inaccessible"
    assert body["open_reports"][0]["report_type"] == "inaccessible"
    assert body["open_reports"][0]["risk"] == "high_risk"
    assert body["public_name"]
    assert "9100020" not in r.text  # never echoes the plate


def test_a_present_unreported_vehicle_is_on_map(fleet):
    v = fleet.vehicle("9100021")
    body = _resolve(plate="910-0021", explain="true").json()
    assert body["status"] == "on_map"
    assert body["vehicle_identifier"] == v
    assert body["open_reports"] == []


def test_a_vehicle_that_left_the_feed_is_missing_with_where_and_when(fleet):
    v = fleet.vehicle("9100022", in_feed=False, last_seen=SNAP - timedelta(hours=30))
    r = _resolve(plate="9100022", explain="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "missing"
    assert body["device_id"] is None  # a stale bike_id may be another vehicle's
    assert body["vehicle_identifier"] == v
    assert body["hours_missing"] == 30.0
    # Rounded to ~100 m, never the exact point.
    assert body["last_seen"] == {"lat": 39.739, "lon": -104.990}


def test_an_acknowledged_gone_vehicle_says_so(fleet):
    v = fleet.vehicle("9100023", in_feed=False, last_seen=SNAP - timedelta(days=40))
    admin = _admin_client(fleet, api_fleet_reports.router)
    assert admin.put(f"/api/v1/private/census/{v}/ack", json={}).status_code == 200
    body = _resolve(plate="9100023", explain="true").json()
    assert body["status"] == "gone"
    assert body["gone_acknowledged_at"]


def test_a_missing_vehicle_still_shows_its_standing_report(fleet):
    acct = fleet.account()
    v = fleet.vehicle("9100024", in_feed=False)
    fleet.report(v, "inaccessible", account_id=acct)
    body = _resolve(plate="9100024", explain="true").json()
    assert body["status"] == "missing"
    assert body["negative_report_reason"] == "inaccessible"
    assert [o["report_type"] for o in body["open_reports"]] == ["inaccessible"]


def test_without_explain_an_off_feed_vehicle_is_still_404(fleet):
    # Back-compat: the plain lookup is exactly the current-snapshot lookup.
    fleet.vehicle("9100025", in_feed=False)
    r = _resolve(plate="9100025")
    assert r.status_code == 404
    v = fleet.vehicle("9100026")
    assert _resolve(plate="9100026").json() == {"device_id": "bike-9100026",
                                                 "vehicle_identifier": v}


def test_an_unknown_plate_is_404_even_with_explain(fleet):
    assert _resolve(plate="9199999", explain="true").status_code == 404


def test_an_ambiguous_plate_is_404(fleet):
    # Two device_state rows whose stored plates normalise to the same string.
    fleet.vehicle("91000-27", in_feed=False)
    fleet.vehicle("9100027", in_feed=False)
    assert _resolve(plate="9100027", explain="true").status_code == 404


def test_identify_misses_are_charged_to_the_same_per_ip_bucket(fleet):
    hdr = {"X-Forwarded-For": "198.51.100.77"}
    c = _public_client(api_vehicle_plates.router)
    for i in range(30):
        c.get("/api/v1/vehicles/resolve",
              params={"plate": f"{9300000 + i}", "explain": "true"}, headers=hdr)
    r = c.get("/api/v1/vehicles/resolve", params={"qr": "9100028", "explain": "true"},
              headers=hdr)
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------

def _vids(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [d["vehicle_identifier"] for d in resp.json()["devices"]]


def test_missing_excludes_the_overnight_van_and_includes_five_days(fleet):
    van = fleet.vehicle("9100030", in_feed=False, last_seen=SNAP - timedelta(hours=2))
    lost = fleet.vehicle("9100031", in_feed=False, last_seen=SNAP - timedelta(days=5))
    admin = _admin_client(fleet, api_fleet_reports.router)
    got = _vids(admin.get("/api/v1/private/census/missing"))
    assert lost in got and van not in got
    # hours= is a parameter, not a constant.
    assert van in _vids(admin.get("/api/v1/private/census/missing", params={"hours": 1}))


def test_arrivals_order_by_first_ever_observed_not_by_last_move(fleet):
    # `moved` relocated an hour ago (first_observed_at_location resets on
    # movement) but has been in the fleet for a year. An implementation that
    # sorts on the wrong column lists it as the newest scooter.
    new = fleet.vehicle("9100032", first_ever=SNAP - timedelta(hours=3),
                        parked_since=SNAP - timedelta(hours=3))
    moved = fleet.vehicle("9100033", first_ever=SNAP - timedelta(days=365),
                          parked_since=SNAP - timedelta(hours=1))
    older = fleet.vehicle("9100034", first_ever=SNAP - timedelta(days=2),
                          parked_since=SNAP - timedelta(days=2))
    admin = _admin_client(fleet, api_fleet_reports.router)
    got = _vids(admin.get("/api/v1/private/census/arrivals", params={"limit": 500}))
    assert got.index(new) < got.index(older) < got.index(moved)
    assert got[0] == new


def test_ack_moves_a_vehicle_from_missing_to_gone(fleet):
    v = fleet.vehicle("9100035", in_feed=False, last_seen=SNAP - timedelta(days=9))
    admin = _admin_client(fleet, api_fleet_reports.router)
    r = admin.put(f"/api/v1/private/census/{v}/ack", json={"note": "Veo: scrapped"})
    assert r.status_code == 200, r.text
    assert r.json()["ack"]["status"] == "gone"
    assert r.json()["ack"]["acknowledged_by"] == "fr-admin@example.test"
    assert v not in _vids(admin.get("/api/v1/private/census/missing"))
    gone = admin.get("/api/v1/private/census/gone").json()
    row = next(d for d in gone["devices"] if d["vehicle_identifier"] == v)
    assert row["ack"]["note"] == "Veo: scrapped"
    assert row["ack"]["reappeared"] is False


def test_a_gone_vehicle_that_reappears_is_surfaced_and_its_ack_kept(fleet):
    v = fleet.vehicle("9100036", in_feed=False, last_seen=SNAP - timedelta(days=20))
    admin = _admin_client(fleet, api_fleet_reports.router)
    admin.put(f"/api/v1/private/census/{v}/ack", json={})
    # The ingest sees it again: device_state is rewritten, as every cycle does.
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE device_state SET last_observed_at = %s, "
                    "first_observed_at_location = %s WHERE vehicle_identifier = %s",
                    (SNAP, SNAP, v))
    fleet.conn.commit()
    gone = admin.get("/api/v1/private/census/gone").json()
    assert gone["gone_reappeared_count"] == 1
    assert gone["devices"][0]["vehicle_identifier"] == v  # reappeared sorts first
    assert gone["devices"][0]["ack"]["reappeared"] is True
    # Every census response carries the count, so it is never nowhere.
    assert admin.get("/api/v1/private/census/arrivals").json()["gone_reappeared_count"] == 1
    with fleet.conn.cursor() as cur:
        cur.execute("SELECT status FROM device_census_ack WHERE vehicle_identifier = %s", (v,))
        assert cur.fetchone()[0] == "gone"


def test_an_ingest_rewrite_of_device_state_leaves_acks_intact(fleet):
    v = fleet.vehicle("9100037", in_feed=False, last_seen=SNAP - timedelta(days=20))
    admin = _admin_client(fleet, api_fleet_reports.router)
    admin.put(f"/api/v1/private/census/{v}/ack", json={"note": "keep me"})
    with fleet.conn.cursor() as cur:
        # The harshest rewrite: the row deleted and re-inserted.
        cur.execute("DELETE FROM device_state WHERE vehicle_identifier = %s", (v,))
        cur.execute(
            "INSERT INTO device_state (vehicle_identifier, first_observed_at_location, "
            "first_ever_observed_at, last_observed_at) VALUES (%s, %s, %s, %s)",
            (v, SNAP - timedelta(days=30), SNAP - timedelta(days=100),
             SNAP - timedelta(days=20)))
    fleet.conn.commit()
    with fleet.conn.cursor() as cur:
        cur.execute("SELECT status, note FROM device_census_ack WHERE vehicle_identifier = %s",
                    (v,))
        assert cur.fetchone() == ("gone", "keep me")


def test_unacknowledging_keeps_the_row_and_returns_it_to_missing(fleet):
    v = fleet.vehicle("9100038", in_feed=False, last_seen=SNAP - timedelta(days=9))
    admin = _admin_client(fleet, api_fleet_reports.router)
    admin.put(f"/api/v1/private/census/{v}/ack", json={"note": "n"})
    r = admin.delete(f"/api/v1/private/census/{v}/ack")
    assert r.status_code == 200, r.text
    assert r.json()["ack"]["status"] == "not_gone"
    assert admin.delete(f"/api/v1/private/census/{v}/ack").status_code == 404
    missing = admin.get("/api/v1/private/census/missing").json()
    row = next(d for d in missing["devices"] if d["vehicle_identifier"] == v)
    assert row["ack"]["status"] == "not_gone" and row["ack"]["note"] == "n"
    with fleet.conn.cursor() as cur:
        cur.execute("SELECT withdrawn_by FROM device_census_ack WHERE vehicle_identifier = %s",
                    (v,))
        assert cur.fetchone()[0] == admin.admin_id


def test_a_note_on_an_unacknowledged_vehicle_and_unknown_vehicles(fleet):
    v = fleet.vehicle("9100039", in_feed=False)
    admin = _admin_client(fleet, api_fleet_reports.router)
    r = admin.put(f"/api/v1/private/census/{v}/note", json={"note": "in the shop"})
    assert r.status_code == 200
    assert r.json()["ack"] == {**r.json()["ack"], "status": "not_gone", "note": "in the shop"}
    assert admin.put(f"/api/v1/private/census/{v}/note", json={"note": None}).json()["ack"]["note"] is None
    unknown = "f" * 16
    assert admin.put(f"/api/v1/private/census/{unknown}/ack", json={}).status_code == 404
    assert admin.put(f"/api/v1/private/census/{unknown}/note", json={"note": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------

def test_export_counts(fleet):
    a, b = fleet.account(), fleet.account()
    # Inaccessible, window 30 days, unmoved_days 7:
    old_unmoved = fleet.vehicle("9100040", parked_since=SNAP - timedelta(days=60))
    fleet.report(old_unmoved, "inaccessible", account_id=a, at=SNAP - timedelta(days=10))
    fleet.report(old_unmoved, "inaccessible", account_id=b, at=SNAP - timedelta(days=9))
    recent_unmoved = fleet.vehicle("9100041", parked_since=SNAP - timedelta(days=60))
    fleet.report(recent_unmoved, "inaccessible", account_id=None, at=SNAP - timedelta(days=2))
    moved = fleet.vehicle("9100042", parked_since=SNAP - timedelta(days=1))
    fleet.report(moved, "inaccessible", account_id=a, at=SNAP - timedelta(days=12))
    off_feed = fleet.vehicle("9100043", in_feed=False, parked_since=SNAP - timedelta(days=60))
    fleet.report(off_feed, "inaccessible", account_id=a, at=SNAP - timedelta(days=15))
    voided = fleet.vehicle("9100044", parked_since=SNAP - timedelta(days=60))
    rid = fleet.report(voided, "inaccessible", account_id=a, at=SNAP - timedelta(days=11))
    outside = fleet.vehicle("9100045", parked_since=SNAP - timedelta(days=90))
    fleet.report(outside, "inaccessible", account_id=a, at=SNAP - timedelta(days=45))
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE device_reports SET resolved_at = %s WHERE id = %s", (SNAP, rid))
    fleet.conn.commit()

    # Broken parts, from feature consensus:
    fleet.vehicle("9100050", features={"status": "up_to_date", "bell": True,
                                       "cup_holder": True, "basket": True,
                                       "poor": ["bell", "basket"]})
    fleet.vehicle("9100051", features={"status": "needs_review", "bell": True,
                                       "poor": ["bell"]})
    fleet.vehicle("9100052", features={"status": "up_to_date", "bell": True,
                                       "cup_holder": True, "poor": ["cup_holder"]})
    fleet.vehicle("9100053", features={"status": "up_to_date", "bell": True, "poor": []})
    # Seen before the window: not in the sample.
    fleet.vehicle("9100054", in_feed=False, last_seen=SNAP - timedelta(days=40),
                  features={"status": "up_to_date", "bell": True, "poor": ["bell"]})

    # Why not rideable:
    nr = fleet.vehicle("9100060")
    fleet.report(nr, "not_rideable", account_id=a, reason="flat_tire",
                 at=SNAP - timedelta(days=1), observed_at=SNAP - timedelta(days=3))
    fleet.report(nr, "not_rideable", account_id=b, reason="flat_tire",
                 at=SNAP - timedelta(days=1), observed_at=SNAP - timedelta(days=1, hours=2))
    fleet.report(nr, "not_rideable", account_id=None, reason="seat",
                 at=SNAP - timedelta(days=2))
    fleet.report(nr, "not_rideable", account_id=None, at=SNAP - timedelta(days=2))
    fleet.report(nr, "not_found", account_id=None, submitted_reason="cannot_find",
                 at=SNAP - timedelta(days=1))
    fleet.report(nr, "not_found", account_id=None, submitted_reason="cannot_find",
                 at=SNAP - timedelta(days=40))  # outside the window: not counted
    fleet.report(nr, "improperly_parked", account_id=a, at=SNAP - timedelta(days=1))
    fleet.report(nr, "dead_battery", account_id=None, submitted_reason="dead_battery",
                 at=SNAP - timedelta(days=1))

    admin = _admin_client(fleet, api_fleet_reports.router)
    r = admin.get("/api/v1/private/reports/export",
                  params={"window_days": 30, "unmoved_days": 7})
    assert r.status_code == 200, r.text
    data = r.json()
    inacc = data["inaccessible"]
    assert inacc["vehicles_reported"] == 4      # old_unmoved, recent, moved, off_feed
    assert inacc["vehicles_reported_signed_in"] == 3
    assert inacc["reports"] == 5
    assert inacc["still_unmoved"] == 1          # only old_unmoved
    assert data["headline"] == "4 vehicles reported inaccessible, 1 still unmoved after 7 days"
    row = next(v for v in inacc["vehicles"] if v["vehicle_identifier"] == old_unmoved)
    assert row["distinct_reporting_accounts"] == 2 and row["still_unmoved"] is True
    assert "lat" not in str(inacc["vehicles"])  # never a location
    assert data["window"]["days"] == 30 and inacc["definition"]

    parts = {p["part"]: p for p in data["broken_parts"]["parts"]}
    assert [p["part"] for p in data["broken_parts"]["parts"]] == \
        ["bell", "cup_holder", "basket", "phone_holder"]
    assert parts["bell"]["broken"] == 1 and parts["bell"]["under_review"] == 1
    assert parts["cup_holder"]["broken"] == 1
    assert parts["basket"]["broken"] == 1
    assert parts["phone_holder"]["broken"] == 0
    assert parts["bell"]["vehicles_with_part"] == 4
    assert data["broken_parts"]["sample"]["vehicles_with_feature_answers"] == 4
    assert data["broken_parts"]["definition"]

    problems = data["problem_reports"]
    reasons = {r["reason"]: r for r in problems["not_rideable"]["reasons"]}
    assert reasons["flat_tire"]["reports"] == 2 and reasons["flat_tire"]["vehicles"] == 1
    assert reasons["seat"]["reports"] == 1
    assert reasons["unspecified"]["reports"] == 1
    assert reasons["acceleration"]["reports"] == 0
    assert reasons["flat_tire"]["oldest_observed_at"].startswith("2099-05-29")
    assert reasons["flat_tire"]["median_report_lag_hours"] == 25.0  # (48 + 2) / 2
    assert problems["remapped"] == {"cannot_find": 1, "dead_battery": 1}
    by_type = {t["report_type"]: t["reports"] for t in problems["by_type"]}
    assert by_type["not_rideable"] == 4 and by_type["dead_battery"] == 1
    assert by_type["not_found"] == 1
    # Improperly parked still counts in the export: it is Veo's to act on.
    assert by_type["improperly_parked"] == 1

    csv_r = admin.get("/api/v1/private/reports/export",
                      params={"window_days": 30, "unmoved_days": 7, "format": "csv"})
    assert csv_r.status_code == 200
    assert csv_r.headers["content-type"].startswith("text/csv")
    assert "inaccessible_still_unmoved,1," in csv_r.text
    assert "broken_bell,1,bell" in csv_r.text
    assert "not_rideable_reason_flat_tire,2," in csv_r.text
    per_vehicle = admin.get("/api/v1/private/reports/export",
                            params={"format": "csv", "table": "inaccessible"}).text
    assert old_unmoved in per_vehicle and "lat" not in per_vehicle.splitlines()[0]


def test_the_dossier_shows_reports_reasons_parts_and_census(fleet):
    a = fleet.account("fr-rider@example.test")
    v = fleet.vehicle("9100070", features={"status": "up_to_date", "bell": True,
                                           "basket": True, "poor": ["basket"]})
    standing = fleet.report(v, "not_rideable", account_id=a, reason="wheel",
                            observed_at=SNAP - timedelta(days=4))
    anon = fleet.report(v, "damaged", account_id=None, at=SNAP - timedelta(days=5))
    parked = fleet.report(v, "improperly_parked", account_id=a, at=SNAP - timedelta(days=2))
    admin = _admin_client(fleet, api_fleet_reports.router)
    r = admin.get(f"/api/v1/private/devices/{v}/reports")
    assert r.status_code == 200, r.text
    body = r.json()
    reports = {x["id"]: x for x in body["reports"]}
    assert reports[standing]["reason"] == "wheel"
    assert reports[standing]["standing"] is True
    assert reports[standing]["reporter_email"] == "fr-rider@example.test"
    assert reports[standing]["observed_at"].startswith("2099-05-28")
    # Anonymous and five days old: still uncleared (it faded to unknown).
    assert reports[anon]["signed_in"] is False and reports[anon]["standing"] is True
    # In the dossier as Veo's to act on, but it changes no label.
    assert reports[parked]["report_type"] == "improperly_parked"
    assert reports[parked]["standing"] is False
    assert body["negative_report"]["risk"] == "high_risk"
    assert body["negative_report"]["reason"] == "not_rideable"
    assert reports[standing]["distance_from_baseline_m"] == 0
    assert "suppression" not in body
    assert body["features"]["broken_parts"] == ["basket"]
    assert admin.get(f"/api/v1/private/devices/{'e' * 16}/reports").status_code == 404


# ---------------------------------------------------------------------------
# sql/100
# ---------------------------------------------------------------------------

def test_the_constraint_permits_inaccessible_and_the_reasons(fleet):
    v = fleet.vehicle("9100080")
    with fleet.conn.cursor() as cur:
        cur.execute("INSERT INTO device_reports (vehicle_identifier, report_type, reason) "
                    "VALUES (%s, 'not_rideable', 'handlebar')", (v,))
        cur.execute("INSERT INTO device_reports (vehicle_identifier, report_type, "
                    "submitted_reason) VALUES (%s, 'not_found', 'cannot_find')", (v,))
    fleet.conn.commit()
    for bad in [("damaged", "seat", None), ("not_rideable", "vibes", None),
                ("not_rideable", None, "cannot_find"),
                ("inaccessible", None, "cannot_find")]:
        with fleet.conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO device_reports (vehicle_identifier, report_type, "
                            "reason, submitted_reason) VALUES (%s, %s, %s, %s)", (v, *bad))
        fleet.conn.rollback()


def test_replaying_every_migration_with_an_inaccessible_row_is_safe(fleet):
    # sql/037 used to drop and re-add the five-value list unconditionally,
    # which a stored 'inaccessible' row would reject on the next replay.
    v = fleet.vehicle("9100081")
    fleet.report(v, "inaccessible", account_id=None)
    with fleet.conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    fleet.conn.commit()


@pytest.mark.parametrize("historical", ["device_reports_report_type_allowed",
                                        "device_reports_report_type_check"])
def test_sql_100_replays_over_either_historical_constraint_name(fleet, historical):
    sql100 = (SQL_DIR / "100_fleet_reports.sql").read_text()
    with fleet.conn.cursor() as cur:
        cur.execute("ALTER TABLE device_reports DROP CONSTRAINT IF EXISTS "
                    "device_reports_report_type_allowed")
        cur.execute(f"ALTER TABLE device_reports ADD CONSTRAINT {historical} CHECK "
                    "(report_type IN ('not_rideable', 'dead_battery', 'damaged', "
                    "'improperly_parked', 'not_found'))")
        cur.execute(sql100)
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'device_reports'::regclass AND contype = 'c' "
            "AND conname LIKE 'device_reports_report_type%%'")
        rows = dict(cur.fetchall())
    fleet.conn.commit()
    assert set(rows) == {"device_reports_report_type_allowed"}
    assert "inaccessible" in rows["device_reports_report_type_allowed"]


def test_sql_101_refiles_cannot_find_rows_stored_under_sql_100s_rule(fleet):
    # sql/100 (as merged in #144) paired 'cannot_find' with 'inaccessible'.
    # The owner corrected it to 'not_found'; sql/101 moves the constraint and
    # re-files any row already stored the old way.
    v = fleet.vehicle("9100082")
    with fleet.conn.cursor() as cur:
        cur.execute("ALTER TABLE device_reports DROP CONSTRAINT "
                    "device_reports_submitted_reason_allowed")
        cur.execute(
            "ALTER TABLE device_reports ADD CONSTRAINT "
            "device_reports_submitted_reason_allowed CHECK (submitted_reason IS NULL OR ("
            "(submitted_reason = 'cannot_find' AND report_type = 'inaccessible') OR "
            "(submitted_reason = 'dead_battery' AND report_type = 'dead_battery')))")
        cur.execute("INSERT INTO device_reports (vehicle_identifier, report_type, "
                    "submitted_reason) VALUES (%s, 'inaccessible', 'cannot_find') "
                    "RETURNING id", (v,))
        rid = cur.fetchone()[0]
        cur.execute((SQL_DIR / "101_cannot_find_is_not_found.sql").read_text())
        cur.execute("SELECT report_type FROM device_reports WHERE id = %s", (rid,))
        assert cur.fetchone()[0] == "not_found"
        # And a replay is a no-op.
        cur.execute((SQL_DIR / "101_cannot_find_is_not_found.sql").read_text())
    fleet.conn.commit()
