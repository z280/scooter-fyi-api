"""The console's four Fleet endpoints, and the parity that keeps them honest.

The reports queue and the reporters view are each read by TWO surfaces now:
the Jinja page under /admin/fleet, and the JSON endpoint the in-app admin
console calls. Both go through src/fleet_admin_queries.py, and the tests that
matter here are the ones that would catch that going wrong — same fixture,
both surfaces, same rows in the same order. A console that says "3 open" while
the portal says "4" is worse than no console, because an operator would
believe whichever one they opened first.

Also covered: the account attribution on a reinstatement (sql/106 — the
portal records a GitHub login, the console an account, and both columns are
filled by the one write path), and the watch endpoints, whose rule is that an
admin signed in here can only sign up their OWN phone.

SKIPS unless VEO_TEST_PG_DSN points at a reachable, migratable database.
"""

from __future__ import annotations

import re


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from src import api_admin, api_fleet_admin, fleet_reports  # noqa: E402
from src.accounts import SessionUser, require_admin  # noqa: E402
from tests.test_admin_fleet_pg import _ORIGIN, _app as _page_client  # noqa: E402,F401
from tests.test_admin_fleet_pg import fleet  # noqa: E402,F401  (the fixture)

#: Every /api/v1/private route the console added, with a body for the writes.
_ROUTES = [
    ("get", "/api/v1/private/reports/queue", None),
    ("post", "/api/v1/private/reports/1/reinstate", {"reason": "x"}),
    ("get", "/api/v1/private/fleet/reporters", None),
    ("get", "/api/v1/private/fleet/watches", None),
    ("post", "/api/v1/private/fleet/watches",
     {"vehicle_identifier": "00000000000000aa", "consent": True}),
    ("delete", "/api/v1/private/fleet/watches/1", None),
]


def _json_client(admin: tuple[int, str] | None) -> TestClient:
    """The console's door: an account session on the admin allowlist.
    `admin=None` is a signed-out visitor."""
    app = FastAPI()
    app.include_router(api_fleet_admin.router)
    if admin is not None:
        aid, email = admin
        app.dependency_overrides[require_admin] = lambda: SessionUser(
            account_id=aid, email=email, scopes=("rider",), expires_at=None,
            sliding=True, method="google", token_sha256="x")
    return TestClient(app)


def _page_ids(html: str) -> list[int]:
    """The report ids the queue page rendered, in the order it rendered them.
    The first cell of each row is the id (src/templates/fleet_reports.html)."""
    return [int(m) for m in re.findall(r"<tr>\s*<td>(\d+)</td>", html)]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", _ROUTES)
def test_every_console_route_refuses_without_a_session(fleet, method, path, body):
    c = _json_client(None)
    r = getattr(c, method)(path, **({"json": body} if body else {}))
    assert r.status_code == 401, r.text


def test_every_console_route_is_gated_on_the_admin_allowlist(fleet):
    """Not on the session's scopes — on `accounts.require_admin`, which
    re-checks the allowlist against the table on every request, so removing
    an admin takes effect on their next call rather than at their next
    sign-in. Asserted on the routes themselves because `require_admin` calls
    `require_session` directly rather than through Depends, which makes it
    unmockable from the outside and therefore worth pinning here."""
    from fastapi.routing import APIRoute

    routes = [r for r in api_fleet_admin.router.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(_ROUTES)
    for r in routes:
        gates = [d.call for d in r.dependant.dependencies]
        assert require_admin in gates, r.path


# ---------------------------------------------------------------------------
# Queue parity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("params", [
    {},
    {"report_type": "improperly_parked"},
    {"reason": "flat_tire"},
    {"standing": "yes"},
    {"standing": "no"},
    {"status": "open"},
    {"status": "resolved"},
])
def test_the_queue_answers_the_same_as_the_page(fleet, params):
    a = fleet.account()
    admin = fleet.admin_account()
    v1 = fleet.vehicle("9500001")
    v2 = fleet.vehicle("9500002")
    fleet.report(v1, "not_rideable", account_id=a, reason="flat_tire")
    fleet.report(v1, "improperly_parked", account_id=a)
    fleet.report(v2, "damaged", account_id=None)
    resolved = fleet.report(v2, "not_rideable", account_id=a, reason="flat_tire")
    with fleet.conn.cursor() as cur:
        fleet_reports.resolve_report(
            cur, resolved, source=fleet_reports.RESOLUTION_SOURCE_ADMIN,
            resolution="checked, fine", login="octo-admin")
    fleet.conn.commit()

    page = _page_client().get("/admin/fleet/reports", params=params)
    assert page.status_code == 200, page.text
    api = _json_client(admin).get("/api/v1/private/reports/queue", params=params)
    assert api.status_code == 200, api.text

    assert [r["id"] for r in api.json()["reports"]] == _page_ids(page.text), params


def test_each_queue_filter_narrows_to_what_it_says(fleet):
    """Parity alone cannot catch this. Both surfaces call one function, so a
    filter that is wrong in that function is wrong identically on both and
    the parity assertions still hold. These are the absolute statements."""
    a = fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500007")
    flat = fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    bare = fleet.report(v, "not_rideable", account_id=a)
    parked = fleet.report(v, "improperly_parked", account_id=a)
    done = fleet.report(v, "improperly_parked", account_id=a)
    with fleet.conn.cursor() as cur:
        fleet_reports.resolve_report(
            cur, done, source=fleet_reports.RESOLUTION_SOURCE_ADMIN,
            resolution="fine", login="octo-admin")
    fleet.conn.commit()
    mine = {flat, bare, parked, done}
    c = _json_client(admin)

    def ids(**params) -> set[int]:
        body = c.get("/api/v1/private/reports/queue", params=params).json()
        return {r["id"] for r in body["reports"]} & mine

    assert ids(report_type="improperly_parked") == {parked, done}
    assert ids(reason="flat_tire") == {flat}
    # 'unspecified' is a not_rideable report that named no reason — not every
    # report without one, which would also match improperly_parked.
    assert ids(reason="unspecified") == {bare}
    assert ids(status="open") == {flat, bare, parked}
    assert ids(status="resolved") == {done}
    # A resolved report no longer stands, so the two filters agree about it.
    assert done not in ids(standing="yes")
    assert done in ids(standing="no")
    assert ids(report_type="not_rideable", status="open") == {flat, bare}


def test_the_queue_region_filter_answers_the_same_as_the_page(fleet):
    a = fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500003")
    fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    region = api_admin._region_of(39.7392123, -104.9903456)
    assert region, "the neighborhood layer must be loaded for this to mean anything"
    other = next(n for n in api_admin._region_names() if n != region)
    for params in ({"region": region}, {"region": other}):
        page = _page_client().get("/admin/fleet/reports", params=params)
        api = _json_client(admin).get("/api/v1/private/reports/queue", params=params)
        assert [r["id"] for r in api.json()["reports"]] == _page_ids(page.text), params
    # And the scan ceiling travels with the answer, so a caller can say
    # "older than this was not looked at" rather than implying completeness.
    body = _json_client(admin).get("/api/v1/private/reports/queue",
                                   params={"region": region}).json()
    assert body["scan_limited"] is False
    assert body["scan_limit"] == 5000


def test_the_queue_carries_the_vocabularies_its_filters_need(fleet):
    admin = fleet.admin_account()
    body = _json_client(admin).get("/api/v1/private/reports/queue").json()
    f = body["filters"]
    assert "not_rideable" in f["report_types"] and "improperly_parked" in f["report_types"]
    # 'unspecified' is a pseudo-reason the queue filters on (a not_rideable
    # report that named none), so it belongs in the vocabulary.
    assert "unspecified" in f["reasons"] and "flat_tire" in f["reasons"]
    assert f["regions"], "the neighborhood names the region filter offers"
    assert body["page"] == 0 and body["page_size"] == 50
    # as_of is the cycle snapshot, not now: it is what "standing" means.
    assert body["as_of"]


def test_the_queue_pages_without_counting_the_whole_table(fleet):
    a = fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500004")
    ids = [fleet.report(v, "improperly_parked", account_id=a) for _ in range(3)]
    c = _json_client(admin)
    import src.fleet_admin_queries as q

    orig = q.PAGE_SIZE
    q.PAGE_SIZE = 2
    try:
        first = c.get("/api/v1/private/reports/queue",
                      params={"report_type": "improperly_parked"}).json()
        assert len(first["reports"]) == 2 and first["has_next"] is True
        second = c.get("/api/v1/private/reports/queue",
                       params={"report_type": "improperly_parked", "page": 1}).json()
        assert second["has_next"] is False
        assert set(ids) == {r["id"] for r in first["reports"] + second["reports"]}
        # No total: counting every match on every page view costs more than
        # it tells an operator, so the contract offers has_next instead.
        assert "total" not in first
    finally:
        q.PAGE_SIZE = orig


def test_a_queue_row_carries_what_the_console_renders(fleet):
    a = fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500005")
    rid = fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    row = next(r for r in _json_client(admin).get(
        "/api/v1/private/reports/queue").json()["reports"] if r["id"] == rid)
    assert row["report_type"] == "not_rideable" and row["reason"] == "flat_tire"
    assert row["signed_in"] is True and row["account_id"] == a
    assert row["standing"] is True and row["negative_type"] is True
    assert row["display_name"] and row["vehicle_identifier"] == v
    # Times are ISO strings, not datetimes the JSON encoder guessed at.
    assert row["reported_at"].startswith("20") and row["resolved_at"] is None


def test_the_queue_shows_no_reporter_emails(fleet):
    a = fleet.account("queue-reporter@example.test")
    admin = fleet.admin_account()
    v = fleet.vehicle("9500006")
    fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    body = _json_client(admin).get("/api/v1/private/reports/queue").text
    assert "queue-reporter@example.test" not in body


# ---------------------------------------------------------------------------
# Reinstate
# ---------------------------------------------------------------------------

def _rider_resolve(fleet, rid: int) -> None:
    with fleet.conn.cursor() as cur:
        fleet_reports.resolve_report(
            cur, rid, source=fleet_reports.RESOLUTION_SOURCE_RIDER_CHECK,
            resolution="rider says it rides", account_id=fleet.account())
    fleet.conn.commit()


def test_reinstating_a_rider_resolution_is_attributed_to_the_account(fleet):
    a = fleet.account()
    aid, email = admin = fleet.admin_account()
    v = fleet.vehicle("9500010")
    rid = fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    _rider_resolve(fleet, rid)

    r = _json_client(admin).post(f"/api/v1/private/reports/{rid}/reinstate",
                                 json={"reason": "two more riders say otherwise"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": rid, "reinstated": True,
                        "reason": "two more riders say otherwise"}
    row = fleet.one(
        "SELECT resolved_at, resolution_source, reinstated_by, reinstated_by_login, "
        "reinstate_reason FROM device_reports WHERE id = %s", rid)
    assert row[0] is None and row[1] is None
    # sql/106: the account, because this session has one. The login column
    # stays empty — it is the portal's capacity, and an email never goes in it.
    assert row[2] == aid and row[3] is None
    assert row[4] == "two more riders say otherwise"
    assert email not in str(row)


def test_the_portal_still_reinstates_under_its_own_login(fleet):
    # The other half of sql/106: one write path, two capacities.
    a = fleet.account()
    v = fleet.vehicle("9500011")
    rid = fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    _rider_resolve(fleet, rid)
    r = _page_client().post(f"/admin/fleet/reports/{rid}/reinstate",
                            data={"reason": "put it back"}, headers=_ORIGIN)
    assert r.status_code == 303, r.text
    row = fleet.one("SELECT reinstated_by, reinstated_by_login FROM device_reports "
                    "WHERE id = %s", rid)
    assert row == (None, "octo-admin")


def test_an_admins_own_resolution_cannot_be_reinstated(fleet):
    a = fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500012")
    rid = fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    with fleet.conn.cursor() as cur:
        fleet_reports.resolve_report(
            cur, rid, source=fleet_reports.RESOLUTION_SOURCE_ADMIN,
            resolution="judged", login="octo-admin")
    fleet.conn.commit()
    r = _json_client(admin).post(f"/api/v1/private/reports/{rid}/reinstate",
                                 json={"reason": "I changed my mind"})
    assert r.status_code == 409, r.text
    assert fleet.one("SELECT resolved_at FROM device_reports WHERE id = %s", rid)[0]


def test_reinstating_what_does_not_exist_is_a_404(fleet):
    admin = fleet.admin_account()
    r = _json_client(admin).post("/api/v1/private/reports/99999999/reinstate",
                                 json={"reason": "x"})
    assert r.status_code == 404


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}])
def test_reinstating_needs_a_reason(fleet, body):
    admin = fleet.admin_account()
    r = _json_client(admin).post("/api/v1/private/reports/1/reinstate", json=body)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Reporters parity
# ---------------------------------------------------------------------------

def test_reporters_answers_the_same_accounts_as_the_page(fleet):
    a, b = fleet.account(), fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500020")
    for _ in range(3):
        fleet.report(v, "improperly_parked", account_id=a)
    fleet.report(v, "not_rideable", account_id=b, reason="flat_tire")

    page = _page_client().get("/admin/fleet/reporters", params={"days": 30})
    assert page.status_code == 200, page.text
    body = _json_client(admin).get("/api/v1/private/fleet/reporters",
                                   params={"days": 30}).json()
    # The page links each account by id; the JSON must name the same ones in
    # the same order (reports + resolutions, descending).
    page_ids = [int(m) for m in re.findall(r"reporters\?account_id=(\d+)&", page.text)]
    assert [r["account_id"] for r in body["reporters"]] == page_ids

    mine = next(r for r in body["reporters"] if r["account_id"] == a)
    assert mine["reports"] == 3 and mine["by_type"]["improperly_parked"] == 3
    assert mine["vehicles"] == 1
    assert body["days"] == 30 and body["since"]


#: A real resolution-10 cell over downtown Denver, and its resolution-8 parent.
_CELL_10 = 622174931027427327
_CELL_8 = "88268cda81fffff"


def test_reporter_detail_carries_the_hour_profile_and_a_coarse_spread(fleet):
    a = fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500021")
    rid = fleet.report(v, "not_rideable", account_id=a, reason="flat_tire")
    with fleet.conn.cursor() as cur:
        cur.execute("UPDATE device_reports SET h3_10_index = %s WHERE id = %s",
                    (_CELL_10, rid))
    fleet.conn.commit()
    body = _json_client(admin).get("/api/v1/private/fleet/reporters",
                                   params={"account_id": a}).json()
    d = body["detail"]
    assert d["account_id"] == a
    assert len(d["by_hour"]) == 24 and sum(h["reports"] for h in d["by_hour"]) == 1
    assert len(d["reports"]) == 1 and d["reports"][0]["reported_at"].startswith("20")
    # Resolution 8 is ~0.7 km²: a cluster is visible, an address is not.
    assert d["cells"] == [{"cell": _CELL_8, "reports": 1}]


def test_an_unusable_stored_cell_is_named_rather_than_dropped(fleet):
    # The shared fixture stores an h3_10_index that is not a valid cell, which
    # makes it the case worth pinning: a bad stored cell must not disappear
    # from the spread (which would understate it) or raise.
    a = fleet.account()
    admin = fleet.admin_account()
    v = fleet.vehicle("9500023")
    fleet.report(v, "improperly_parked", account_id=a)
    d = _json_client(admin).get("/api/v1/private/fleet/reporters",
                                params={"account_id": a}).json()["detail"]
    assert d["cells"] == [{"cell": "invalid-cell", "reports": 1}]


def test_reporters_without_an_account_id_has_no_detail(fleet):
    admin = fleet.admin_account()
    body = _json_client(admin).get("/api/v1/private/fleet/reporters").json()
    assert body["detail"] is None


def test_reporters_shows_no_emails(fleet):
    a = fleet.account("reporter-email@example.test")
    admin = fleet.admin_account()
    v = fleet.vehicle("9500022")
    fleet.report(v, "improperly_parked", account_id=a)
    text = _json_client(admin).get("/api/v1/private/fleet/reporters",
                                   params={"account_id": a}).text
    assert "reporter-email@example.test" not in text


# ---------------------------------------------------------------------------
# Watches
# ---------------------------------------------------------------------------

def test_a_watch_starts_on_the_signed_in_admins_own_phone(fleet):
    v = fleet.vehicle("9500030")
    aid, email = admin = fleet.admin_account()
    c = _json_client(admin)
    r = c.post("/api/v1/private/fleet/watches",
               json={"vehicle_identifier": v, "hours": 6, "consent": True})
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    # The account it texts is the admin's own: there is no recipient field to
    # point it at a colleague's phone.
    by, acct = fleet.one("SELECT created_by_login, account_id FROM admin_device_watches "
                         "WHERE id = %s", wid)
    assert acct == aid
    # The audit column is a GitHub login on the portal's side, so the account
    # goes in marked as such — never the email.
    assert by == f"account:{aid}" and email not in by
    assert len(fleet.sent) == 1 and fleet.sent[0]["to"] == fleet.phones[aid]

    listed = c.get("/api/v1/private/fleet/watches").json()
    mine = next(w for w in listed["watches"] if w["id"] == wid)
    assert mine["live"] is True and mine["mine"] is True
    assert mine["account_id"] == aid and mine["display_name"]
    assert listed["limits"]["max_texts"] == 20
    # A phone number is never in the response.
    assert fleet.phones[aid] not in c.get("/api/v1/private/fleet/watches").text

    assert c.delete(f"/api/v1/private/fleet/watches/{wid}").status_code == 200
    assert fleet.one("SELECT ended_reason, ended_by_login FROM admin_device_watches "
                     "WHERE id = %s", wid) == ("unsubscribed", f"account:{aid}")
    # Stopping it again is a 404, not a second pretend stop.
    assert c.delete(f"/api/v1/private/fleet/watches/{wid}").status_code == 404


def test_a_watch_will_not_start_without_consent(fleet):
    v = fleet.vehicle("9500031")
    admin = fleet.admin_account()
    r = _json_client(admin).post("/api/v1/private/fleet/watches",
                                 json={"vehicle_identifier": v, "hours": 6})
    assert r.status_code == 422 and "consent" in r.text
    assert fleet.sent == []
    assert fleet.one("SELECT COUNT(*) FROM admin_device_watches WHERE "
                     "vehicle_identifier = %s", v)[0] == 0


@pytest.mark.parametrize("kw,word", [
    ({"verified": False}, "verified"),
    ({"opted_out": True}, "STOP"),
])
def test_a_watch_refuses_a_phone_it_must_not_text(fleet, kw, word):
    v = fleet.vehicle("9500032")
    admin = fleet.admin_account(**kw)
    r = _json_client(admin).post("/api/v1/private/fleet/watches",
                                 json={"vehicle_identifier": v, "consent": True})
    assert r.status_code == 409 and word in r.text, r.text
    assert fleet.sent == []


def test_a_watch_on_an_unknown_vehicle_is_refused(fleet):
    admin = fleet.admin_account()
    r = _json_client(admin).post(
        "/api/v1/private/fleet/watches",
        json={"vehicle_identifier": "00000000deadbeef", "consent": True})
    assert r.status_code == 409 and "unknown vehicle" in r.text


@pytest.mark.parametrize("body", [
    {"vehicle_identifier": "nope", "consent": True},
    {"vehicle_identifier": "00000000000000aa", "consent": True, "hours": 0},
    {"vehicle_identifier": "00000000000000aa", "consent": True, "hours": 10_000},
    {"consent": True},
])
def test_a_watch_request_is_validated_before_anything_is_written(fleet, body):
    admin = fleet.admin_account()
    r = _json_client(admin).post("/api/v1/private/fleet/watches", json=body)
    assert r.status_code == 422, r.text
    assert fleet.sent == []


def test_the_watch_list_matches_the_page(fleet):
    v = fleet.vehicle("9500033")
    aid, _email = admin = fleet.admin_account()
    c = _json_client(admin)
    c.post("/api/v1/private/fleet/watches",
           json={"vehicle_identifier": v, "hours": 6, "consent": True})
    page = _page_client().get("/admin/fleet/watches")
    ids_json = [w["id"] for w in c.get("/api/v1/private/fleet/watches").json()["watches"]]
    ids_page = [int(m) for m in re.findall(r"watches/(\d+)/unsubscribe", page.text)]
    assert set(ids_page) <= set(ids_json)
    assert ids_json, "the watch just started must be listed"


def test_someone_elses_watch_is_listed_but_not_marked_mine(fleet):
    v = fleet.vehicle("9500034")
    theirs = fleet.admin_account()
    mine = fleet.admin_account()
    _json_client(theirs).post("/api/v1/private/fleet/watches",
                              json={"vehicle_identifier": v, "consent": True})
    body = _json_client(mine).get("/api/v1/private/fleet/watches").json()
    w = next(w for w in body["watches"] if w["vehicle_identifier"] == v)
    assert w["live"] is True and w["mine"] is False
