"""Fleet reports Phase 2 — the /admin/fleet pages — against real Postgres.

Page auth (no GitHub admin session → refused), rendering with fixtures, the
resolve form's audit, reinstating a rider resolution, the census actions,
the export CSV, and the SMS watch: subscribe (consent, verified phone,
allowlist), expiry, STOP, and the per-cycle alert — with comms mocked, so
nothing is ever sent.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
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
    accounts, admin_watch, api_admin, api_fleet_admin, api_fleet_reports,
    api_public, auth, condition_checks, dwell_stats,
)
from src.comms import OptedOut  # noqa: E402
from src.ingest import TaggedDevice  # noqa: E402
from tests.test_fleet_reports_pg import SNAP, SQL_DIR, _Fleet, _reachable  # noqa: E402

_ORIGIN = {"origin": "http://testserver"}


class _AdminFleet(_Fleet):
    def cleanup(self):
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM admin_device_watches WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM device_condition_checks WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM device_history WHERE vehicle_identifier = ANY(%s)",
                        (self.vids,))
            cur.execute("DELETE FROM admin_allowlist WHERE email LIKE 'af-%%@example.test'")
        self.conn.commit()
        super().cleanup()

    def admin_account(self, *, phone: str | None = None, verified: bool = True,
                      opted_out: bool = False, allowlisted: bool = True) -> tuple[int, str]:
        import uuid

        email = f"af-{uuid.uuid4().hex[:8]}@example.test"
        aid = self.account(email)
        phone = phone or f"+1303{aid % 10_000_000:07d}"
        self.phones = getattr(self, "phones", {})
        self.phones[aid] = phone
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET phone_number = %s, phone_verified_at = %s, "
                "sms_opted_out_at = %s WHERE id = %s",
                (phone, datetime.now(timezone.utc) if verified else None,
                 datetime.now(timezone.utc) if opted_out else None, aid),
            )
            if allowlisted:
                cur.execute("INSERT INTO admin_allowlist (email, added_by) VALUES (%s, 'test') "
                            "ON CONFLICT DO NOTHING", (email,))
        self.conn.commit()
        return aid, email

    def stop(self, vid: str, arrived: datetime, departed: datetime | None,
             lat: float = 39.7392, lon: float = -104.9903):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_history (vehicle_identifier, snapshot_time, departed_at,
                    lat, lon, spatial_status, device_id_observed)
                VALUES (%s, %s, %s, %s, %s, 'denver_core', 'bike-x')
                """,
                (vid, arrived, departed, lat, lon),
            )
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
        pytest.skip("VEO_TEST_PG_DSN not set — admin fleet Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    @contextmanager
    def _per_request_connection():
        with psycopg.connect(dsn) as c:
            yield c

    for mod in (api_admin, api_fleet_admin, api_fleet_reports, api_public, admin_watch,
                condition_checks, dwell_stats, accounts):
        monkeypatch.setattr(mod, "connection", _per_request_connection)
    dwell_stats._cache.clear()
    sent: list[dict] = []

    def _fake_send(to, body, *, idempotency_key, **kw):
        sent.append({"to": to, "body": body, "key": idempotency_key})
        return {"id": "fake"}

    monkeypatch.setattr(admin_watch, "send_sms", _fake_send)
    f = _AdminFleet(conn)
    f.sent = sent  # type: ignore[attr-defined]
    try:
        yield f
    finally:
        f.cleanup()
        conn.close()


def _app(admin: bool = True) -> TestClient:
    from starlette.middleware.sessions import SessionMiddleware

    app = FastAPI()
    app.include_router(api_admin.router)
    # The real app's session layer; without an override, require_admin reads
    # an empty session and refuses — exactly a signed-out visitor.
    app.add_middleware(SessionMiddleware, secret_key="test")
    if admin:
        app.dependency_overrides[auth.require_admin] = lambda: {"login": "octo-admin"}
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/admin/fleet", "/admin/fleet/reports", "/admin/fleet/reporters",
    "/admin/fleet/census", "/admin/fleet/watches", "/admin/fleet/export",
    "/admin/fleet/export.csv", "/admin/fleet/devices/00000000000000aa",
])
def test_every_page_refuses_without_an_admin_session(fleet, path):
    assert _app(admin=False).get(path).status_code == 401


@pytest.mark.parametrize("path", [
    "/admin/fleet/reports/1/resolve", "/admin/fleet/reports/1/reinstate",
    "/admin/fleet/watches", "/admin/fleet/watches/1/unsubscribe",
    "/admin/fleet/census/00000000000000aa/ack",
])
def test_every_form_refuses_without_an_admin_session(fleet, path):
    form = {"resolution": "x", "reason": "x", "note": "x",
            "vehicle_identifier": "00000000000000aa", "account_email": "a@b.c",
            "hours": "1", "consent": "1"}
    r = _app(admin=False).post(path, data=form, headers=_ORIGIN)
    assert r.status_code == 401


def test_the_admin_index_links_every_fleet_page(fleet):
    app = FastAPI()
    app.include_router(api_admin.router)
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key="t")
    c = TestClient(app, follow_redirects=False)
    assert c.get("/admin").headers["location"] == "/admin/login"
    body = _app().get("/admin/fleet").text
    for href in ("/admin/fleet/reports", "/admin/fleet/reporters", "/admin/fleet/census",
                 "/admin/fleet/watches", "/admin/fleet/export"):
        assert href in body, href


# ---------------------------------------------------------------------------
# Queue, dossier, reporters render with fixtures
# ---------------------------------------------------------------------------

def test_the_queue_renders_and_filters(fleet):
    a = fleet.account()
    v = fleet.vehicle("9400001")
    fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    fleet.report(v, "improperly_parked", account_id=a)
    c = _app()
    r = c.get("/admin/fleet/reports")
    assert r.status_code == 200, r.text
    row = "<code>not_rideable</code> · flat_tire"
    assert row in r.text and "uncleared" in r.text and "no label" in r.text
    assert "example.test" not in r.text      # no reporter emails
    r = c.get("/admin/fleet/reports", params={"report_type": "improperly_parked"})
    assert row not in r.text and "<code>improperly_parked</code>" in r.text
    r = c.get("/admin/fleet/reports", params={"standing": "yes", "reason": "flat_tire"})
    assert row in r.text
    r = c.get("/admin/fleet/reports", params={"standing": "no", "reason": "flat_tire"})
    assert row not in r.text
    # Region: the report has no coordinates of its own, so it falls back to
    # its cell, then to the vehicle's position (downtown Denver here).
    region = api_admin._region_of(39.7392123, -104.9903456)
    assert region
    assert row in c.get("/admin/fleet/reports", params={"region": region}).text
    other = next(n for n in api_admin._region_names() if n != region)
    assert row not in c.get("/admin/fleet/reports", params={"region": other}).text


def test_the_dossier_renders_reports_checks_and_the_same_spot_view(fleet):
    a, b = fleet.account(), fleet.account()
    v = fleet.vehicle("9400002", parked_since=SNAP - timedelta(days=2))
    # Two separate stops at the same yard, each drawing an inaccessible report.
    fleet.stop(v, SNAP - timedelta(days=20), SNAP - timedelta(days=15))
    fleet.stop(v, SNAP - timedelta(days=15), SNAP - timedelta(days=2), lat=39.75, lon=-104.95)
    fleet.stop(v, SNAP - timedelta(days=2), None)
    fleet.report(v, "inaccessible", account_id=a, at=SNAP - timedelta(days=18))
    fleet.report(v, "inaccessible", account_id=b, at=SNAP - timedelta(days=1))
    r = _app().get(f"/admin/fleet/devices/{v}", params={"days": 90})
    assert r.status_code == 200, r.text
    assert "Repeatedly hidden at the same spot" in r.text
    assert 'tag tag-bad">repeated' in r.text
    assert "Condition checks" in r.text and "Battery" in r.text
    assert "example.test" not in r.text


def test_the_dossier_404s_politely(fleet):
    r = _app().get("/admin/fleet/devices/00000000000000ab")
    assert r.status_code == 200 and "not found" in r.text.lower()


def test_the_reporter_view_counts_reports_and_rider_resolutions(fleet):
    a, b = fleet.account(), fleet.account()
    with fleet.conn.cursor() as cur:
        cur.execute("SELECT (SELECT word FROM sfw_adjectives ORDER BY word LIMIT 1), "
                    "(SELECT emoji FROM emoji_nouns ORDER BY emoji LIMIT 1)")
        adj, emo = cur.fetchone()
        cur.execute("UPDATE accounts SET username_adjective = %s, username_emoji = %s "
                    "WHERE id = %s RETURNING public_username", (adj, emo, a))
        username = cur.fetchone()[0]
    fleet.conn.commit()
    v = fleet.vehicle("9400003")
    w = fleet.vehicle("9400004")
    now = datetime.now(timezone.utc)
    fleet.report(v, "inaccessible", account_id=a, at=now - timedelta(days=1))
    fleet.report(w, "inaccessible", account_id=a, at=now - timedelta(days=2))
    rid = fleet.report(w, "damaged", account_id=b, at=now - timedelta(days=1))
    with fleet.conn.cursor() as cur:
        cur.execute("INSERT INTO device_condition_checks (vehicle_identifier, account_id, "
                    "test_ride, proof, reports_resolved, feed_status) "
                    "VALUES (%s, %s, true, 'plate', 1, 'unconfirmed')", (w, b))
    fleet.conn.commit()
    c = _app()
    r = c.get("/admin/fleet/reporters", params={"days": 30})
    assert r.status_code == 200, r.text
    assert f"#{a}" in r.text and username in r.text and f"#{b}" in r.text
    assert "example.test" not in r.text
    r = c.get("/admin/fleet/reporters", params={"days": 30, "account_id": a})
    assert r.status_code == 200 and "H3 res-8" in r.text
    assert f"/admin/fleet/devices/{w}" in r.text
    assert rid


# ---------------------------------------------------------------------------
# Resolve / reinstate forms: audited
# ---------------------------------------------------------------------------

def test_the_resolve_form_is_audited_by_login(fleet):
    a = fleet.account()
    v = fleet.vehicle("9400010")
    rid = fleet.report(v, "inaccessible", account_id=a)
    c = _app()
    # CSRF: no Origin, refused.
    r = c.post(f"/admin/fleet/reports/{rid}/resolve", data={"resolution": "void"})
    assert "cross-site" in r.headers["location"].replace("+", " ").replace("%20", " ")
    assert fleet.one("SELECT resolved_at FROM device_reports WHERE id = %s", rid)[0] is None
    # A reason is required.
    r = c.post(f"/admin/fleet/reports/{rid}/resolve", data={"resolution": "  "},
               headers=_ORIGIN)
    assert "error" in r.headers["location"]
    r = c.post(f"/admin/fleet/reports/{rid}/resolve",
               data={"resolution": "void: gate open, checked in person",
                     "next": f"/admin/fleet/devices/{v}"}, headers=_ORIGIN)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/admin/fleet/devices/{v}")
    assert fleet.one(
        "SELECT resolution_source, resolved_by, resolved_by_login, resolution "
        "FROM device_reports WHERE id = %s", rid) == (
        "admin", None, "octo-admin", "void: gate open, checked in person")
    # Final: a second resolve is refused, and an admin resolution cannot be reinstated.
    r = c.post(f"/admin/fleet/reports/{rid}/resolve", data={"resolution": "again"},
               headers=_ORIGIN)
    assert "already" in r.headers["location"]
    r = c.post(f"/admin/fleet/reports/{rid}/reinstate", data={"reason": "oops"},
               headers=_ORIGIN)
    assert "not+resolved+by+a+rider" in r.headers["location"]
    # next= never leaves /admin/fleet.
    rid2 = fleet.report(v, "damaged", account_id=a)
    r = c.post(f"/admin/fleet/reports/{rid2}/resolve",
               data={"resolution": "x", "next": "https://evil.example"}, headers=_ORIGIN)
    assert r.headers["location"].startswith("/admin/fleet/reports")


def test_an_admin_can_reinstate_a_rider_resolution(fleet):
    from src import fleet_reports

    a = fleet.account()
    v = fleet.vehicle("9400011")
    rid = fleet.report(v, "inaccessible", account_id=a)
    with fleet.conn.cursor() as cur:
        fleet_reports.resolve_report(cur, rid, source="rider_check",
                                     resolution="rider check", account_id=a)
    fleet.conn.commit()
    assert v not in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)
    r = _app().post(f"/admin/fleet/reports/{rid}/reinstate",
                    data={"reason": "the feed never saw a ride"}, headers=_ORIGIN)
    assert "reinstated" in r.headers["location"]
    row = fleet.one("SELECT resolved_at, resolution_source, reinstated_by_login, "
                    "reinstate_reason FROM device_reports WHERE id = %s", rid)
    assert row == (None, None, "octo-admin", "the feed never saw a ride")
    assert v in fleet_reports.negative_states(fleet.conn.cursor(), fleet.cycle)


# ---------------------------------------------------------------------------
# Census actions, export CSV
# ---------------------------------------------------------------------------

def test_census_pages_and_actions(fleet):
    gone = fleet.vehicle("9400020", in_feed=False, last_seen=SNAP - timedelta(days=10))
    c = _app()
    r = c.get("/admin/fleet/census", params={"list": "missing", "hours": 72})
    assert r.status_code == 200 and gone in r.text
    r = c.post(f"/admin/fleet/census/{gone}/ack", data={"note": "in the van"},
               headers=_ORIGIN)
    assert "acknowledged" in r.headers["location"]
    assert fleet.one("SELECT status, acknowledged_by, acknowledged_by_login, note, "
                     "note_by_login FROM device_census_ack WHERE vehicle_identifier = %s",
                     gone) == ("gone", None, "octo-admin", "in the van", "octo-admin")
    r = c.get("/admin/fleet/census", params={"list": "gone"})
    assert gone in r.text and "octo-admin" in r.text
    r = c.post(f"/admin/fleet/census/{gone}/unack", data={}, headers=_ORIGIN)
    assert "withdrawn" in r.headers["location"]
    assert fleet.one("SELECT status, withdrawn_by_login FROM device_census_ack "
                     "WHERE vehicle_identifier = %s", gone) == ("not_gone", "octo-admin")
    r = c.post(f"/admin/fleet/census/{gone}/unack", data={}, headers=_ORIGIN)
    assert "error" in r.headers["location"]
    r = c.post(f"/admin/fleet/census/{gone}/note", data={"note": "Veo: in the shop"},
               headers=_ORIGIN)
    assert fleet.one("SELECT note FROM device_census_ack WHERE vehicle_identifier = %s",
                     gone)[0] == "Veo: in the shop"
    assert c.get("/admin/fleet/census", params={"list": "arrivals"}).status_code == 200


def test_the_export_page_and_csv(fleet):
    a = fleet.account()
    v = fleet.vehicle("9400030", parked_since=SNAP - timedelta(days=60))
    fleet.report(v, "inaccessible", account_id=a, at=SNAP - timedelta(days=10))
    c = _app()
    r = c.get("/admin/fleet/export", params={"window_days": 30, "unmoved_days": 7})
    assert r.status_code == 200, r.text
    assert "vehicles reported inaccessible" in r.text and "Broken parts" in r.text
    r = c.get("/admin/fleet/export.csv", params={"window_days": 30, "unmoved_days": 7})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.splitlines()[0] == "metric,value,part,window_from,window_to,unmoved_days"
    assert "inaccessible_vehicles_reported" in r.text
    r = c.get("/admin/fleet/export.csv", params={"table": "inaccessible"})
    assert v in r.text and "lat" not in r.text.splitlines()[0]


# ---------------------------------------------------------------------------
# SMS watch
# ---------------------------------------------------------------------------

def test_subscribing_needs_consent_an_allowlisted_verified_phone(fleet):
    v = fleet.vehicle("9400040")
    c = _app()
    _aid, email = fleet.admin_account()
    r = c.post("/admin/fleet/watches", data={"vehicle_identifier": v,
                                              "account_email": email, "hours": 24},
               headers=_ORIGIN)
    assert "consent" in r.headers["location"]
    _x, unverified = fleet.admin_account(verified=False)
    _y, not_admin = fleet.admin_account(allowlisted=False)
    _z, stopped = fleet.admin_account(opted_out=True)
    for who, word in ((unverified, "verified"), (not_admin, "allowlist"), (stopped, "STOP")):
        r = c.post("/admin/fleet/watches", data={"vehicle_identifier": v,
                                                  "account_email": who, "hours": 24,
                                                  "consent": "1"}, headers=_ORIGIN)
        assert word in r.headers["location"].replace("+", " "), who
    assert fleet.sent == []
    assert fleet.one("SELECT COUNT(*) FROM admin_device_watches WHERE vehicle_identifier = %s",
                     v)[0] == 0


def test_subscribe_confirms_by_text_then_alerts_on_change_and_expires(fleet):
    v = fleet.vehicle("9400041")
    aid, email = fleet.admin_account()
    c = _app()
    r = c.post("/admin/fleet/watches", data={"vehicle_identifier": v, "account_email": email,
                                              "hours": 6, "consent": "on"}, headers=_ORIGIN)
    assert "started" in r.headers["location"], r.headers["location"]
    wid, by, acct = fleet.one("SELECT id, created_by_login, account_id FROM "
                              "admin_device_watches WHERE vehicle_identifier = %s", v)
    assert (by, acct) == ("octo-admin", aid)
    assert len(fleet.sent) == 1 and "STOP" in fleet.sent[0]["body"]
    assert fleet.sent[0]["key"] == f"admin-watch:{wid}:start"
    assert fleet.sent[0]["to"] == fleet.phones[aid]

    dev = TaggedDevice(device_id="b", vehicle_type_id=None, form_factor="scooter",
                       lat=39.7392, lon=-104.9903, spatial_status="denver_core",
                       vehicle_identifier=v, is_reserved=False, is_disabled=False)
    admin_watch.watch_for_cycle(SNAP, [dev])          # baseline: silent
    assert len(fleet.sent) == 1
    import dataclasses
    admin_watch.watch_for_cycle(SNAP, [dataclasses.replace(dev, is_reserved=True)])
    assert len(fleet.sent) == 2 and "rental started" in fleet.sent[1]["body"]
    assert fleet.sent[1]["key"] == f"admin-watch:{wid}:1"
    admin_watch.watch_for_cycle(SNAP, [dataclasses.replace(dev, lat=39.7492)])
    assert "rental ended" in fleet.sent[2]["body"] and "moved" in fleet.sent[2]["body"]
    admin_watch.watch_for_cycle(SNAP, [])
    assert "left the feed" in fleet.sent[3]["body"]
    # The page lists it, live, and the dossier links to it.
    assert "octo-admin" in c.get("/admin/fleet/watches").text
    # Expiry ends it; no more texts.
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE admin_device_watches SET expires_at = NOW() - INTERVAL '1 minute' "
                    "WHERE id = %s", (wid,))
    fleet.conn.commit()
    admin_watch.watch_for_cycle(SNAP, [dev])
    assert len(fleet.sent) == 4
    assert fleet.one("SELECT ended_reason FROM admin_device_watches WHERE id = %s",
                     wid)[0] == "expired"


def test_stop_ends_the_watch(fleet, monkeypatch):
    v = fleet.vehicle("9400042")
    aid, email = fleet.admin_account()
    c = _app()
    c.post("/admin/fleet/watches", data={"vehicle_identifier": v, "account_email": email,
                                         "hours": 6, "consent": "1"}, headers=_ORIGIN)
    wid = fleet.one("SELECT id FROM admin_device_watches WHERE vehicle_identifier = %s", v)[0]
    dev = TaggedDevice(device_id="b", vehicle_type_id=None, form_factor="scooter",
                       lat=39.7392, lon=-104.9903, spatial_status="denver_core",
                       vehicle_identifier=v, is_reserved=False)
    admin_watch.watch_for_cycle(SNAP, [dev])

    def _stopped(*a, **k):
        raise OptedOut("Reply UNSTOP to resume")

    monkeypatch.setattr(admin_watch, "send_sms", _stopped)
    admin_watch.watch_for_cycle(SNAP, [])
    assert fleet.one("SELECT ended_reason FROM admin_device_watches WHERE id = %s",
                     wid)[0] == "opted_out"


def test_a_stop_mirrored_onto_the_account_ends_the_watch_without_sending(fleet):
    v = fleet.vehicle("9400043")
    aid, email = fleet.admin_account()
    _app().post("/admin/fleet/watches", data={"vehicle_identifier": v, "account_email": email,
                                              "hours": 6, "consent": "1"}, headers=_ORIGIN)
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE accounts SET sms_opted_out_at = NOW() WHERE id = %s", (aid,))
    fleet.conn.commit()
    admin_watch.watch_for_cycle(SNAP, [])
    assert len(fleet.sent) == 1   # only the confirmation
    assert fleet.one("SELECT ended_reason FROM admin_device_watches "
                     "WHERE vehicle_identifier = %s", v)[0] == "opted_out"


def test_an_admin_can_unsubscribe(fleet):
    v = fleet.vehicle("9400044")
    _aid, email = fleet.admin_account()
    c = _app()
    c.post("/admin/fleet/watches", data={"vehicle_identifier": v, "account_email": email,
                                         "hours": 6, "consent": "1"}, headers=_ORIGIN)
    wid = fleet.one("SELECT id FROM admin_device_watches WHERE vehicle_identifier = %s", v)[0]
    r = c.post(f"/admin/fleet/watches/{wid}/unsubscribe", headers=_ORIGIN)
    assert "stopped" in r.headers["location"]
    assert fleet.one("SELECT ended_reason, ended_by_login FROM admin_device_watches "
                     "WHERE id = %s", wid) == ("unsubscribed", "octo-admin")
    r = c.post(f"/admin/fleet/watches/{wid}/unsubscribe", headers=_ORIGIN)
    assert "error" in r.headers["location"]


def test_the_watch_text_cap(fleet, monkeypatch):
    monkeypatch.setattr(admin_watch, "MAX_TEXTS_PER_WATCH", 2)
    v = fleet.vehicle("9400045")
    _aid, email = fleet.admin_account()
    _app().post("/admin/fleet/watches", data={"vehicle_identifier": v, "account_email": email,
                                              "hours": 6, "consent": "1"}, headers=_ORIGIN)
    dev = TaggedDevice(device_id="b", vehicle_type_id=None, form_factor="scooter",
                       lat=39.7392, lon=-104.9903, spatial_status="denver_core",
                       vehicle_identifier=v, is_reserved=False)
    admin_watch.watch_for_cycle(SNAP, [dev])
    admin_watch.watch_for_cycle(SNAP, [])
    admin_watch.watch_for_cycle(SNAP, [dev])
    admin_watch.watch_for_cycle(SNAP, [])
    assert len(fleet.sent) == 3   # confirmation + the cap of 2
    assert fleet.one("SELECT ended_reason FROM admin_device_watches "
                     "WHERE vehicle_identifier = %s", v)[0] == "cap_reached"
