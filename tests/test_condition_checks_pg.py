"""Fleet reports Phase 1b — rider condition checks — against real Postgres
(docs/FLEET_REPORTS_PLAN.md §4.4).

Covers the endpoints and their auth and rate limits, the test-ride-No
discard, resolve / reconfirm semantics and their audit, the points (10, +40,
cooldown, daily cap, own-report anti-farm), the feed-confirmation window,
and `needs_condition_check` on /devices/current.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
NEVER point that at production: the fixture executes every migration.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import (  # noqa: E402
    api_condition_checks, api_fleet_reports, api_public, condition_checks, dwell_stats,
    fleet_reports, points,
)
from src.accounts import SessionUser, require_session  # noqa: E402
from tests.test_fleet_reports_pg import SQL_DIR, _Fleet, _reachable  # noqa: E402

_BUCKETS = ("condition_checks_account", "condition_conditions_account")


class _CheckFleet(_Fleet):
    def cleanup(self):
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM device_condition_checks WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM device_feature_reports WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM rate_limit_events WHERE bucket = ANY(%s)", (list(_BUCKETS),))
        self.conn.commit()
        super().cleanup()

    def feature_report(self, vid: str, account_id: int, *, plate_valid: bool = True,
                       age: timedelta = timedelta(minutes=5)) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_feature_reports (
                    vehicle_identifier, account_id, submitted_plate, plate_valid,
                    has_bell, has_cup_holder, has_phone_holder, all_good_condition,
                    poor_condition, status_at_report, reported_at
                ) VALUES (%s, %s, 'x', %s, true, true, true, true, '{}',
                          'up_to_date', NOW() - %s::interval)
                RETURNING id
                """,
                (vid, account_id, plate_valid, age),
            )
            rid = cur.fetchone()[0]
        self.conn.commit()
        return rid

    def state(self, vid: str, **cols):
        sets = ", ".join(f"{k} = %s" for k in cols)
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE device_state SET {sets} WHERE vehicle_identifier = %s",
                        (*cols.values(), vid))
        self.conn.commit()

    def one(self, sql: str, *args):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
        self.conn.commit()
        return row


@pytest.fixture()
def fleet(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — condition check Postgres test skipped")
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

    for mod in (api_condition_checks, condition_checks, api_public, api_fleet_reports,
                dwell_stats):
        monkeypatch.setattr(mod, "connection", _per_request_connection)
    dwell_stats._cache.clear()
    f = _CheckFleet(conn)
    try:
        yield f
    finally:
        f.cleanup()
        conn.close()


def _rider(fleet, account_id: int | None = None) -> TestClient:
    aid = account_id or fleet.account()
    app = FastAPI()
    app.include_router(api_condition_checks.router)
    app.dependency_overrides[require_session] = lambda: SessionUser(
        account_id=aid, email=None, scopes=("rider",), expires_at=None, sliding=True,
        method="sms", token_sha256="x")
    c = TestClient(app)
    c.account_id = aid  # type: ignore[attr-defined]
    return c


def _check(client, vid, *, answers, test_ride=True, plate=None, **extra):
    body = {"answers": [{"report_id": r, "still_a_problem": s} for r, s in answers],
            "test_ride": test_ride, **extra}
    if plate is not None:
        body["submitted_plate"] = plate
    return client.post(f"/api/v1/devices/{vid}/condition-checks", json=body)


def _devices(fleet) -> dict[str, dict]:
    app = FastAPI()
    app.include_router(api_public.router)
    r = TestClient(app).get("/api/v1/devices/current")
    assert r.status_code == 200, r.text
    return {f["properties"]["vehicle_identifier"]: f["properties"]
            for f in r.json()["features"]}


def _ledger(fleet, account_id: int) -> list[tuple[str, int]]:
    with fleet.conn.cursor() as cur:
        cur.execute("SELECT action, points FROM user_points WHERE account_id = %s "
                    "AND action LIKE 'condition_check%%' ORDER BY id", (account_id,))
        rows = cur.fetchall()
    fleet.conn.commit()
    return rows


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_both_endpoints_need_a_session(fleet):
    v = fleet.vehicle("9300001")
    app = FastAPI()
    app.include_router(api_condition_checks.router)
    c = TestClient(app)
    assert c.get(f"/api/v1/devices/{v}/conditions").status_code == 401
    r = c.post(f"/api/v1/devices/{v}/condition-checks",
               json={"answers": [], "test_ride": True, "submitted_plate": "9300001"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# The list, and needs_condition_check
# ---------------------------------------------------------------------------

def test_the_list_is_the_standing_negative_rideability_reports(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300002")
    nr = fleet.report(v, "not_rideable", account_id=a, reason="flat_tire",
                      observed_at=fleet_reports_snap() - timedelta(days=4))
    inacc = fleet.report(v, "inaccessible", account_id=b)
    nf = fleet.report(v, "not_found", account_id=b)
    fleet.report(v, "improperly_parked", account_id=b)          # never listed
    fleet.report(v, "damaged", account_id=None)                 # anonymous: never stands
    resolved = fleet.report(v, "dead_battery", account_id=a)
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE device_reports SET resolved_at = NOW(), "
                    "resolution_source = 'admin' WHERE id = %s", (resolved,))
    fleet.conn.commit()

    rider = _rider(fleet, a)
    r = rider.get(f"/api/v1/devices/{v}/conditions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["report_id"] for c in body["conditions"]] == [inacc, nr]
    flat = next(c for c in body["conditions"] if c["report_id"] == nr)
    assert flat["reason"] == "flat_tire" and flat["own_report"] is True
    assert flat["observed_at"].startswith("2099-05-28")
    assert [c["report_id"] for c in body["auto_resolves"]] == [nf]
    assert body["needs_condition_check"] is True
    assert body["points"] == {"base": 10, "feed_confirmed": 40, "max": 50,
                              "eligible": True, "withheld_reason": None}
    assert body["feed_window_minutes"] == 20


def fleet_reports_snap():
    from tests.test_fleet_reports_pg import SNAP
    return SNAP


def test_needs_condition_check_on_devices_current(fleet):
    a = fleet.account()
    hidden = fleet.vehicle("9300003")
    fleet.report(hidden, "not_rideable", account_id=a)
    only_nf = fleet.vehicle("9300004")
    fleet.report(only_nf, "not_found", account_id=a)
    parked = fleet.vehicle("9300005")
    fleet.report(parked, "improperly_parked", account_id=a)
    clean = fleet.vehicle("9300006")
    d = _devices(fleet)
    assert d[hidden]["needs_condition_check"] is True
    # not_found suppresses but is not asked about: no invitation.
    assert d[only_nf]["needs_condition_check"] is False
    assert d[only_nf]["suppressed"] is True
    assert d[parked]["needs_condition_check"] is False
    assert d[clean]["needs_condition_check"] is False


# ---------------------------------------------------------------------------
# Proof, validation, nothing-to-check
# ---------------------------------------------------------------------------

def test_presence_must_be_proven(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300007")
    rid = fleet.report(v, "damaged", account_id=b)
    rider = _rider(fleet, a)
    r = _check(rider, v, answers=[(rid, False)], plate="0000000")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "presence_not_proven"
    # A feature report by someone else, or a stale one, is no proof either.
    other = fleet.feature_report(v, b)
    stale = fleet.feature_report(v, a, age=timedelta(hours=2))
    for fid in (other, stale):
        r = _check(rider, v, answers=[(rid, False)], feature_report_id=fid)
        assert r.status_code == 422, fid
    assert fleet.one("SELECT resolved_at FROM device_reports WHERE id = %s", rid)[0] is None
    # The rider's own fresh plate-valid feature report is.
    fid = fleet.feature_report(v, a)
    r = _check(rider, v, answers=[(rid, False)], feature_report_id=fid)
    assert r.status_code == 200, r.text
    assert fleet.one("SELECT proof, feature_report_id FROM device_condition_checks "
                     "WHERE id = %s", r.json()["check_id"]) == ("feature_report", fid)


def test_every_listed_condition_must_be_answered(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300008")
    r1 = fleet.report(v, "damaged", account_id=b)
    r2 = fleet.report(v, "not_rideable", account_id=b)
    rider = _rider(fleet, a)
    r = _check(rider, v, answers=[(r1, False)], plate="9300008")
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "unanswered", "report_ids": [r2],
                                  "message": r.json()["detail"]["message"]}


def test_answers_naming_another_vehicles_report_are_refused(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300009")
    w = fleet.vehicle("9300010")
    fleet.report(v, "damaged", account_id=b)
    foreign = fleet.report(w, "damaged", account_id=b)
    r = _check(_rider(fleet, a), v, answers=[(foreign, False)], plate="9300009")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_report"


def test_nothing_to_check_is_a_409(fleet):
    v = fleet.vehicle("9300011")
    r = _check(_rider(fleet), v, answers=[], plate="9300011")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "nothing_to_check"


# ---------------------------------------------------------------------------
# Test ride = No discards everything
# ---------------------------------------------------------------------------

def test_no_test_ride_discards_every_answer(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300012")
    r1 = fleet.report(v, "not_rideable", account_id=b)
    r2 = fleet.report(v, "dead_battery", account_id=b)
    nf = fleet.report(v, "not_found", account_id=b)
    rider = _rider(fleet, a)
    r = _check(rider, v, answers=[(r1, False), (r2, True)], test_ride=False,
               plate="9300012")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discarded"] is True and body["points_awarded"] == 0
    assert body["resolved"] == body["reconfirmed"] == body["found"] == []
    for rid in (r1, r2, nf):
        assert fleet.one("SELECT resolved_at, reconfirm_count FROM device_reports "
                         "WHERE id = %s", rid) == (None, 0)
    # The minimal audit row, and nothing else.
    row = fleet.one("SELECT test_ride, feed_status, points_base, points_withheld "
                    "FROM device_condition_checks WHERE id = %s", body["check_id"])
    assert row == (False, "not_applicable", 0, "no_test_ride")
    assert fleet.one("SELECT COUNT(*) FROM device_condition_check_answers "
                     "WHERE check_id = %s", body["check_id"])[0] == 0
    assert _ledger(fleet, a) == []
    assert v in fleet_reports.suppressions(fleet.conn.cursor(), fleet.cycle)


# ---------------------------------------------------------------------------
# Test ride = Yes: resolve, reconfirm, found — and the audit
# ---------------------------------------------------------------------------

def test_a_test_ride_resolves_and_reconfirms_with_audit(fleet):
    a, b, admin_acct = fleet.account(), fleet.account(), fleet.account()
    v = fleet.vehicle("9300013")
    fixed = fleet.report(v, "not_rideable", account_id=b, reason="acceleration")
    still = fleet.report(v, "damaged", account_id=b)
    nf = fleet.report(v, "not_found", account_id=b)
    rider = _rider(fleet, a)
    r = _check(rider, v, answers=[(fixed, False), (still, True)], plate="9300013")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] == [fixed] and body["reconfirmed"] == [still]
    assert body["found"] == [nf]
    assert body["points_awarded"] == 10 and body["points_pending"] == 40
    assert body["feed_status"] == "pending"
    cid = body["check_id"]

    src, by, check_id, why = fleet.one(
        "SELECT resolution_source, resolved_by, resolved_by_check_id, resolution "
        "FROM device_reports WHERE id = %s", fixed)
    assert (src, by, check_id) == ("rider_check", a, cid)
    assert why.startswith(f"rider condition check #{cid}")
    assert fleet.one("SELECT resolution_source FROM device_reports WHERE id = %s",
                     nf)[0] == "rider_check"
    resolved_at, count, last = fleet.one(
        "SELECT resolved_at, reconfirm_count, last_reconfirmed_at FROM device_reports "
        "WHERE id = %s", still)
    assert resolved_at is None and count == 1 and last is not None
    with fleet.conn.cursor() as cur:
        cur.execute("SELECT report_id, still_a_problem, outcome, own_report "
                    "FROM device_condition_check_answers WHERE check_id = %s "
                    "ORDER BY report_id", (cid,))
        answers = cur.fetchall()
    fleet.conn.commit()
    assert answers == sorted([(fixed, False, "resolved", False),
                              (still, True, "reconfirmed", False),
                              (nf, None, "found", False)])
    # The reconfirmed report still suppresses; the resolved ones count for nothing.
    s = fleet_reports.suppressions(fleet.conn.cursor(), fleet.cycle)
    assert s[v][0] == "damaged"
    # A rider resolution is distinct from an admin's in the dossier.
    adm = TestClient(_admin_app(admin_acct))
    d = adm.get(f"/api/v1/private/devices/{v}/reports").json()
    by_id = {x["id"]: x for x in d["reports"]}
    assert by_id[fixed]["resolution_source"] == "rider_check"
    assert by_id[fixed]["resolved_by_check_id"] == cid
    assert by_id[still]["reconfirm_count"] == 1
    assert d["condition_checks"][0]["id"] == cid
    # And an admin resolve stays 'admin'.
    rr = adm.post(f"/api/v1/private/reports/{still}/resolve", json={"resolution": "void"})
    assert rr.status_code == 200 and rr.json()["resolution_source"] == "admin"


def _admin_app(account_id: int) -> FastAPI:
    from src.accounts import require_admin

    app = FastAPI()
    app.include_router(api_fleet_reports.router)
    app.dependency_overrides[require_admin] = lambda: SessionUser(
        account_id=account_id, email="cc-admin@example.test", scopes=("rider",),
        expires_at=None, sliding=True, method="google", token_sha256="x")
    return app


def test_a_resolution_unsuppresses_on_the_next_request(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300014")
    rid = fleet.report(v, "inaccessible", account_id=b)
    assert _devices(fleet)[v]["suppressed"] is True
    assert _check(_rider(fleet, a), v, answers=[(rid, False)],
                  plate="9300014").status_code == 200
    d = _devices(fleet)[v]
    assert d["suppressed"] is False and d["needs_condition_check"] is False


# ---------------------------------------------------------------------------
# Points: own reports, cooldown, daily cap
# ---------------------------------------------------------------------------

def test_own_reports_only_earns_nothing_but_still_applies(fleet):
    a = fleet.account()
    v = fleet.vehicle("9300015")
    rid = fleet.report(v, "not_rideable", account_id=a)
    rider = _rider(fleet, a)
    assert rider.get(f"/api/v1/devices/{v}/conditions").json()["points"]["withheld_reason"] \
        == "own_reports_only"
    r = _check(rider, v, answers=[(rid, False)], plate="9300015")
    assert r.status_code == 200
    assert r.json()["points_awarded"] == 0
    assert r.json()["points_withheld_reason"] == "own_reports_only"
    assert fleet.one("SELECT resolution_source FROM device_reports WHERE id = %s",
                     rid)[0] == "rider_check"
    assert _ledger(fleet, a) == []
    # The feed confirming it pays nothing either: the +40 follows a paid 10.
    fleet.state(v, rental_started_at=datetime.now(timezone.utc))
    condition_checks.confirm_pending_checks(datetime.now(timezone.utc))
    assert _ledger(fleet, a) == []


def test_one_award_per_vehicle_per_day(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300016")
    r1 = fleet.report(v, "damaged", account_id=b)
    rider = _rider(fleet, a)
    assert _check(rider, v, answers=[(r1, True)], plate="9300016").json()["points_awarded"] == 10
    r = _check(rider, v, answers=[(r1, True)], plate="9300016")
    assert r.status_code == 200
    assert r.json()["points_awarded"] == 0
    assert r.json()["points_withheld_reason"] == "cooldown"
    assert fleet.one("SELECT reconfirm_count FROM device_reports WHERE id = %s", r1)[0] == 2


def test_the_daily_cap(fleet, monkeypatch):
    monkeypatch.setattr(points, "CONDITION_CHECK_DAILY_CAP", 2)
    a, b = fleet.account(), fleet.account()
    rider = _rider(fleet, a)
    paid = []
    for i in range(3):
        plate = f"93001{20 + i}"
        v = fleet.vehicle(plate)
        rid = fleet.report(v, "damaged", account_id=b)
        r = _check(rider, v, answers=[(rid, True)], plate=plate)
        assert r.status_code == 200, r.text
        paid.append((r.json()["points_awarded"], r.json()["points_withheld_reason"]))
    assert paid == [(10, None), (10, None), (0, "daily_cap")]


# ---------------------------------------------------------------------------
# The feed confirmation window (+40)
# ---------------------------------------------------------------------------

def _pending(fleet, plate: str):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle(plate, parked_since=datetime.now(timezone.utc) - timedelta(days=2))
    rid = fleet.report(v, "damaged", account_id=b)
    r = _check(_rider(fleet, a), v, answers=[(rid, False)], plate=plate)
    assert r.status_code == 200, r.text
    return a, v, r.json()["check_id"]


def test_a_rental_inside_the_window_pays_the_40(fleet):
    a, v, cid = _pending(fleet, "9300030")
    now = datetime.now(timezone.utc)
    fleet.state(v, rental_started_at=now + timedelta(minutes=3))
    stats = condition_checks.confirm_pending_checks(now + timedelta(minutes=4))
    assert stats["confirmed"] >= 1
    assert fleet.one("SELECT feed_status, feed_signal, points_confirmed FROM "
                     "device_condition_checks WHERE id = %s", cid) == ("confirmed", "reserved", 40)
    assert _ledger(fleet, a) == [("condition_check", 10), ("condition_check_confirmed", 40)]
    # Idempotent: a second pass pays nothing more.
    condition_checks.confirm_pending_checks(now + timedelta(minutes=6))
    assert sum(p for _a, p in _ledger(fleet, a)) == 50


def test_a_rental_that_started_just_before_the_check_counts(fleet):
    # "Did you do a test ride?" is past tense: the ride usually starts first.
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9300031", parked_since=datetime.now(timezone.utc) - timedelta(days=2))
    fleet.state(v, rental_started_at=datetime.now(timezone.utc) - timedelta(minutes=8))
    rid = fleet.report(v, "damaged", account_id=b,
                       at=fleet_reports_snap() - timedelta(days=3))
    cid = _check(_rider(fleet, a), v, answers=[(rid, True)], plate="9300031").json()["check_id"]
    fleet.state(v, rental_started_at=None)
    condition_checks.confirm_pending_checks(datetime.now(timezone.utc))
    assert fleet.one("SELECT feed_status, feed_signal FROM device_condition_checks "
                     "WHERE id = %s", cid) == ("confirmed", "rental_before_check")


def test_a_move_after_the_check_counts(fleet):
    a, v, cid = _pending(fleet, "9300032")
    now = datetime.now(timezone.utc)
    fleet.state(v, first_observed_at_location=now + timedelta(minutes=5))
    condition_checks.confirm_pending_checks(now + timedelta(minutes=6))
    assert fleet.one("SELECT feed_status, feed_signal FROM device_condition_checks "
                     "WHERE id = %s", cid) == ("confirmed", "moved")


def test_nothing_in_the_window_is_unconfirmed_and_pays_no_40(fleet):
    a, v, cid = _pending(fleet, "9300033")
    now = datetime.now(timezone.utc)
    # Still inside the window: stays pending.
    condition_checks.confirm_pending_checks(now + timedelta(minutes=10))
    assert fleet.one("SELECT feed_status FROM device_condition_checks WHERE id = %s",
                     cid)[0] == "pending"
    # A rental that starts AFTER the window is somebody else's ride.
    fleet.state(v, rental_started_at=now + timedelta(minutes=45))
    condition_checks.confirm_pending_checks(now + timedelta(minutes=46))
    assert fleet.one("SELECT feed_status, points_confirmed FROM device_condition_checks "
                     "WHERE id = %s", cid) == ("unconfirmed", 0)
    assert _ledger(fleet, a) == [("condition_check", 10)]


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------

def test_the_post_is_rate_limited_per_account(fleet, monkeypatch):
    monkeypatch.setattr(api_condition_checks, "LIMIT_CONDITION_CHECKS_PER_ACCOUNT", (2, 3600))
    v = fleet.vehicle("9300040")
    rider = _rider(fleet)
    codes = [_check(rider, v, answers=[], plate="9300040").status_code for _ in range(3)]
    # Refused checks (409, nothing to check) still spend quota.
    assert codes == [409, 409, 429]


def test_the_get_is_rate_limited_per_account(fleet, monkeypatch):
    monkeypatch.setattr(api_condition_checks, "LIMIT_CONDITIONS_GET_PER_ACCOUNT", (1, 3600))
    v = fleet.vehicle("9300041")
    rider = _rider(fleet)
    assert rider.get(f"/api/v1/devices/{v}/conditions").status_code == 200
    assert rider.get(f"/api/v1/devices/{v}/conditions").status_code == 429


def test_unknown_vehicle_is_a_404(fleet):
    rider = _rider(fleet)
    assert rider.get("/api/v1/devices/00000000000000ff/conditions").status_code == 404
