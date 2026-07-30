"""Ride-session foundation + FEATURE_PLAN §10 reported fields
(sql/047_tracked_rides_reported_fields.sql, sql/049_ride_sessions.sql,
src/api_tracked_rides.py).

Same fake-cursor idiom as tests/test_api_tracked_rides_validation.py: a
monkeypatched connection/cursor, assertions on the SQL that gets built and
the payload shapes that come back, and a bare FastAPI() mounting the single
router with require_session overridden.

The highest-stakes assertion here is
test_track_signing_is_absent_from_the_list_response: track_key is a secret —
anyone holding it can mint batches the ride will accept — and the list
endpoint must never carry it.

The two `_bound_params` helpers pair each `col = %s` / VALUES placeholder
back to its column name by reading the SQL the handler actually built,
rather than hard-coding parameter indices. That is not fussiness: PATCH
.../end splices `path_polyline = %s` in conditionally, so every index after
it moves depending on whether the ride had waypoints, and an index-based
assertion silently checks the wrong slot the day someone adds a column.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import api_tracked_rides
from src.accounts import SessionUser, require_session

_RIDE_ID = uuid.uuid4()
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
_ENDED_AT = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)
_VID = "aaaa000000000000"
_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)

# A real value from data/range_soc_lut.json, so the battery derivation under
# test is the production one and not a stub: rank 51 of that 100-entry table.
_RANGE_METERS_AT_51_PERCENT = 20708

_KEY = "b3RoZXItcmlkZXJzLWNhbm5vdC1oYXZlLXRoaXMta2V5Xw"
_NONCE = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


# ---------------------------------------------------------------------------
# Row fixtures
# ---------------------------------------------------------------------------

def _row(
    *,
    reported_minutes: int | None = None,
    reported_plan: str | None = None,
    ride_options: dict | None = None,
    validation_status: str = "pending",
    validation_reasons: list | None = None,
) -> tuple:
    """An _RIDE_COLS row. Column order must track _RIDE_COLS."""
    return (
        _RIDE_ID, "watching", _NOW, 39.74, -104.98, _NOW,
        None, None, None, None, None,   # gbfs_* block
        None, None, None, None, None,   # user-reported end block
        {}, "", _VID, _NOW, _NOW,       # metadata, polyline, vehicle, created, updated
        None, None, None,               # distance_meters, distance_source, clamped_from
        reported_minutes, reported_plan,
        {} if ride_options is None else ride_options,
        validation_status,
        [] if validation_reasons is None else validation_reasons,
    )


def _owner_row(**kw) -> tuple:
    """An _RIDE_COLS_OWNER row: the above plus _SIGNING_COLS."""
    signing = kw.pop("signing", (_KEY, _NONCE, _NOW))
    return _row(**kw) + signing


def _end_select(*, ride_options: dict | None = None, gbfs_reappeared: bool = False) -> tuple:
    """PATCH .../end's narrower SELECT ... FOR UPDATE."""
    return (
        None,                                    # user_reported_ended_at
        _VID,                                    # vehicle_identifier
        _NOW if gbfs_reappeared else None,       # gbfs_reappeared_at
        39.75 if gbfs_reappeared else None,      # gbfs_end_lat
        -104.99 if gbfs_reappeared else None,    # gbfs_end_lon
        39.74, -104.98,                          # start_lat, start_lon
        {} if ride_options is None else ride_options,
    )


# ---------------------------------------------------------------------------
# Fake cursor / connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, fetchones, fetchalls=()):
        self._ones = list(fetchones)
        self._alls = list(fetchalls)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._ones.pop(0)

    def fetchall(self):
        return self._alls.pop(0) if self._alls else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetchones, fetchalls=()):
        self._fetchones = fetchones
        self._fetchalls = fetchalls
        self.cur: _FakeCursor | None = None

    def cursor(self):
        self.cur = _FakeCursor(self._fetchones, self._fetchalls)
        return self.cur

    def commit(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(api_tracked_rides.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


def _client(monkeypatch, fetchones=(), fetchalls=()):
    conn = _FakeConn(fetchones, fetchalls)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_tracked_rides, "connection", _fake_connection)
    monkeypatch.setattr(api_tracked_rides, "enforce", lambda cur, **kw: None)
    return TestClient(_app()), conn


def _start_fetches(*, telemetry=None, owner_row=None):
    """POST /tracked-rides' fetchone sequence, in order: device_state probe,
    active-ride probe, newest telemetry row, INSERT ... RETURNING, the
    owner-column re-read, plate lookup."""
    return [
        (1,),                                   # device_state: vehicle known
        None,                                   # no active ride
        telemetry,                              # newest raw_telemetry_points row
        (_RIDE_ID, _NOW),                       # INSERT ... RETURNING
        _owner_row() if owner_row is None else owner_row,
        None,                                   # plate lookup: no plate
    ]


# ---------------------------------------------------------------------------
# Bound-parameter readers (see the module docstring)
# ---------------------------------------------------------------------------

def _split_top_level(expr: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in expr:
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        current += ch
    parts.append(current)
    return parts


def _insert_bound_params(conn) -> dict[str, Any]:
    """{column: bound value} for the tracked_rides INSERT, read off the SQL.

    Columns whose VALUES expression binds nothing (NOW()) map to None; one
    that binds a placeholder inside a call (make_interval) maps to it.
    """
    sql, params = next(c for c in conn.cur.executed
                       if c[0].startswith("INSERT INTO tracked_rides"))
    columns = [c.strip() for c in
               re.search(r"INSERT INTO tracked_rides \(([^)]*)\) VALUES", sql).group(1).split(",")]
    exprs = [e.strip() for e in
             _split_top_level(re.search(r"VALUES \((.*)\) RETURNING", sql).group(1))]
    assert len(columns) == len(exprs), (columns, exprs)
    out: dict[str, Any] = {}
    i = 0
    for column, expr in zip(columns, exprs):
        count = expr.count("%s")
        out[column] = params[i] if count == 1 else (None if count == 0 else params[i:i + count])
        i += count
    assert i == len(params), "every bound parameter must belong to a column"
    return out


def _end_bound_params(conn) -> dict[str, Any]:
    """{column: bound value} for PATCH .../end's UPDATE, read off the SQL.
    The trailing `WHERE id = %s` lands under 'id'."""
    sql, params = next(c for c in conn.cur.executed
                       if c[0].lstrip().startswith("UPDATE tracked_rides SET")
                       and "status = 'completed'" in c[0])
    columns = re.findall(r"(\w+) = %s", sql)
    assert len(columns) == len(params), (columns, params)
    return dict(zip(columns, params))


def _post_start(client, **body):
    payload = {"vehicle_identifier": _VID, "start_lat": 39.74, "start_lon": -104.98}
    payload.update(body)
    return client.post("/api/v1/tracked-rides", json=payload)


def _patch_end(client, **body):
    payload = {"ended_at": _ENDED_AT.isoformat(), "end_lat": 39.75, "end_lon": -104.99}
    payload.update(body)
    return client.patch(f"/api/v1/tracked-rides/{_RIDE_ID}/end", json=payload)


def _end_fetches(*, row=None, **select_kw):
    """PATCH .../end's fetchone sequence with no waypoints: the FOR UPDATE
    read, the waypoint COUNT (which makes both award calls no-ops), the final
    response read."""
    return [_end_select(**select_kw), (0,), _row() if row is None else row]


# ---------- ride_options: 4 KB cap -----------------------------------------

def test_ride_options_at_the_cap_is_accepted():
    """Measured on the serialized bytes, so the boundary is exact."""
    pad = api_tracked_rides.MAX_RIDE_OPTIONS_BYTES - len('{"pad": ""}')
    blob = api_tracked_rides._serialize_ride_options({"pad": "x" * pad})
    assert len(blob.encode("utf-8")) == api_tracked_rides.MAX_RIDE_OPTIONS_BYTES


def test_ride_options_one_byte_over_the_cap_is_413():
    pad = api_tracked_rides.MAX_RIDE_OPTIONS_BYTES - len('{"pad": ""}') + 1
    with pytest.raises(HTTPException) as e:
        api_tracked_rides._serialize_ride_options({"pad": "x" * pad})
    assert e.value.status_code == 413
    assert "4 KB" in e.value.detail


def test_the_cap_is_enforced_before_a_connection_is_taken(monkeypatch):
    """A malformed blob is a client bug; it must not cost a pooled
    connection. Any DB access at all fails this test."""
    @contextmanager
    def _explode():
        raise AssertionError("the handler took a connection before validating")
        yield  # pragma: no cover

    monkeypatch.setattr(api_tracked_rides, "connection", _explode)
    r = _post_start(TestClient(_app()), ride_options={"pad": "x" * 5000})
    assert r.status_code == 413


# ---------- ride_options: shape -------------------------------------------

def test_ride_options_full_valid_blob_round_trips(monkeypatch):
    options = {
        "cost_hud": True, "speedometer": "digital", "theme": "auto",
        "navigation": True, "save_tracks": True, "battery_modeling": False,
        "nav_improvement": True, "end_survey": False, "own_device": False,
    }
    c, conn = _client(monkeypatch, _start_fetches())
    r = _post_start(c, ride_options=options)
    assert r.status_code == 200, r.text
    assert json.loads(_insert_bound_params(conn)["ride_options"]) == options


@pytest.mark.parametrize("key", [
    "cost_hud", "navigation", "save_tracks", "battery_modeling",
    "nav_improvement", "end_survey", "own_device",
])
def test_ride_options_gate_booleans_must_be_booleans(monkeypatch, key):
    """A truthy string in save_tracks would silently decide a rider's
    donation eligibility, so every gate the server acts on is type-checked."""
    c, _ = _client(monkeypatch, _start_fetches())
    r = _post_start(c, ride_options={key: "yes"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_ride_options"
    assert key in r.json()["detail"]["detail"]


@pytest.mark.parametrize("key,bad", [("speedometer", "dial"), ("theme", "neon")])
def test_ride_options_enumerated_values_are_checked(monkeypatch, key, bad):
    c, _ = _client(monkeypatch, _start_fetches())
    r = _post_start(c, ride_options={key: bad})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_ride_options"


@pytest.mark.parametrize("key,good", [
    ("speedometer", "classic"), ("speedometer", "digital"), ("speedometer", "none"),
    ("theme", "light"), ("theme", "dark"), ("theme", "auto"),
])
def test_ride_options_every_documented_choice_is_accepted(monkeypatch, key, good):
    c, conn = _client(monkeypatch, _start_fetches())
    assert _post_start(c, ride_options={key: good}).status_code == 200
    assert json.loads(_insert_bound_params(conn)["ride_options"]) == {key: good}


def test_ride_options_unknown_keys_pass_through(monkeypatch):
    """The blob is client-owned: the frontend adds options without needing an
    API deploy first, and the server stores what it does not read."""
    c, conn = _client(monkeypatch, _start_fetches())
    r = _post_start(c, ride_options={"an_option_this_version_never_heard_of": 7})
    assert r.status_code == 200, r.text
    assert json.loads(_insert_bound_params(conn)["ride_options"]) == {
        "an_option_this_version_never_heard_of": 7}


def test_ride_options_omitted_stores_an_empty_object(monkeypatch):
    c, conn = _client(monkeypatch, _start_fetches())
    assert _post_start(c).status_code == 200
    assert _insert_bound_params(conn)["ride_options"] == "{}"


def test_ride_options_must_be_an_object():
    r = _post_start(TestClient(_app()), ride_options=["cost_hud"])
    assert r.status_code == 422


# ---------- reported_start_battery_percent bounds -------------------------

@pytest.mark.parametrize("value", [-0.1, -1, 100.1, 101])
def test_start_battery_outside_0_100_is_rejected(value):
    r = _post_start(TestClient(_app()), reported_start_battery_percent=value)
    assert r.status_code == 422


@pytest.mark.parametrize("value", [0, 42.5, 100])
def test_start_battery_inside_0_100_is_stored(monkeypatch, value):
    c, conn = _client(monkeypatch, _start_fetches())
    assert _post_start(c, reported_start_battery_percent=value).status_code == 200
    assert _insert_bound_params(conn)["reported_start_battery_percent"] == value


def test_start_battery_omitted_stays_null(monkeypatch):
    c, conn = _client(monkeypatch, _start_fetches())
    assert _post_start(c).status_code == 200
    assert _insert_bound_params(conn)["reported_start_battery_percent"] is None


# ---------- feed_* stamping from the newest telemetry row ------------------

def test_feed_start_is_stamped_from_the_newest_fresh_observation(monkeypatch):
    """Battery via quality.compute_battery_percent(current_range_meters) —
    the same derivation ride_watch.py uses for the ride's other end — and a
    position the rider cannot influence."""
    telemetry = (_RANGE_METERS_AT_51_PERCENT, Decimal("39.741234"), Decimal("-104.987654"))
    c, conn = _client(monkeypatch, _start_fetches(telemetry=telemetry))
    assert _post_start(c).status_code == 200
    p = _insert_bound_params(conn)
    assert p["feed_start_battery_percent"] == 51
    assert p["feed_start_lat"] == pytest.approx(39.741234)
    assert p["feed_start_lon"] == pytest.approx(-104.987654)
    assert isinstance(p["feed_start_lat"], float), \
        "NUMERIC(9,6) arrives as Decimal; a DOUBLE PRECISION column takes a float"


def test_feed_start_is_null_when_the_feed_has_no_fresh_observation(monkeypatch):
    """The normal case for a rider who unlocked in the operator's app before
    hitting Start — the vehicle left GBFS the moment it was rented."""
    c, conn = _client(monkeypatch, _start_fetches(telemetry=None))
    assert _post_start(c).status_code == 200
    p = _insert_bound_params(conn)
    assert (p["feed_start_battery_percent"], p["feed_start_lat"], p["feed_start_lon"]) \
        == (None, None, None)


def test_feed_start_battery_is_null_when_range_is_unknown(monkeypatch):
    """A row with a position but no range still anchors the position."""
    c, conn = _client(monkeypatch, _start_fetches(
        telemetry=(None, Decimal("39.74"), Decimal("-104.98"))))
    assert _post_start(c).status_code == 200
    p = _insert_bound_params(conn)
    assert p["feed_start_battery_percent"] is None
    assert p["feed_start_lat"] == pytest.approx(39.74)


def test_the_telemetry_read_is_bounded_by_the_freshness_window(monkeypatch):
    """Unbounded, the 48-hour raw_telemetry_points buffer would hand back a
    position from before somebody else's ride."""
    c, conn = _client(monkeypatch, _start_fetches())
    assert _post_start(c).status_code == 200
    sql, params = next(e for e in conn.cur.executed if "raw_telemetry_points" in e[0])
    assert "ORDER BY snapshot_time DESC LIMIT 1" in sql
    assert "make_interval(mins => %s)" in sql
    assert params == (_VID, api_tracked_rides.FEED_START_MAX_AGE_MINUTES)


def test_the_telemetry_read_is_scoped_to_this_vehicle(monkeypatch):
    c, conn = _client(monkeypatch, _start_fetches())
    assert _post_start(c).status_code == 200
    sql, _ = next(e for e in conn.cur.executed if "raw_telemetry_points" in e[0])
    assert "WHERE vehicle_identifier = %s" in sql


# ---------- track_signing -------------------------------------------------

def _signing_of(payload) -> dict:
    assert payload["track_signing"] is not None
    return payload["track_signing"]


def test_start_response_carries_the_signing_and_validation_blocks(monkeypatch):
    c, _ = _client(monkeypatch, _start_fetches())
    r = _post_start(c)
    assert r.status_code == 200, r.text
    assert _signing_of(r.json()) == {
        "alg": "HS256", "key_id": str(_RIDE_ID), "key": _KEY,
        "nonce": _NONCE, "issued_at": _NOW.isoformat(),
    }
    assert r.json()["validation"] == {"status": "pending", "reasons": []}


def test_start_generates_a_fresh_key_and_nonce_of_the_specified_shapes(monkeypatch):
    """16 random bytes as hex and 32 random bytes as base64url, per the chain
    format — and never the same pair twice."""
    seen = set()
    for _ in range(3):
        c, conn = _client(monkeypatch, _start_fetches())
        assert _post_start(c).status_code == 200
        p = _insert_bound_params(conn)
        key, nonce = p["track_key"], p["track_nonce"]
        assert len(bytes.fromhex(nonce)) == api_tracked_rides.TRACK_NONCE_BYTES
        # base64url of 32 bytes, unpadded — no '=', '+' or '/'.
        assert len(key) == 43
        assert not (set(key) & set("=+/"))
        seen.add((key, nonce))
    assert len(seen) == 3, "the key/nonce pair must be fresh per ride"


def test_the_key_is_issued_server_side_at_start(monkeypatch):
    """track_key_issued_at is NOW() in the INSERT, not a client value: a
    donated chain must not be able to claim it predates the ride."""
    c, conn = _client(monkeypatch, _start_fetches())
    assert _post_start(c).status_code == 200
    sql, _ = next(e for e in conn.cur.executed if e[0].startswith("INSERT INTO tracked_rides"))
    assert "track_key_issued_at" in sql
    assert _insert_bound_params(conn)["track_key_issued_at"] is None  # bound to NOW()


def test_active_response_carries_the_signing_block(monkeypatch):
    """A client that reloaded mid-ride resumes signing from this."""
    c, _ = _client(monkeypatch, [_owner_row(), None])
    r = c.get("/api/v1/tracked-rides/active")
    assert r.status_code == 200, r.text
    assert _signing_of(r.json()["active"])["key"] == _KEY


def test_detail_response_carries_the_signing_block(monkeypatch):
    c, _ = _client(monkeypatch, [_owner_row(), None])
    r = c.get(f"/api/v1/tracked-rides/{_RIDE_ID}")
    assert r.status_code == 200, r.text
    assert _signing_of(r.json())["nonce"] == _NONCE


def test_track_signing_is_absent_from_the_list_response(monkeypatch):
    """THE SECRET-LEAK GUARD. Not a redaction: the list query never selects
    track_key, so there is nothing there to forget to strip."""
    c, conn = _client(monkeypatch, fetchones=[], fetchalls=[[_row(), _row()]])
    r = c.get("/api/v1/tracked-rides")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    for ride in r.json()["rides"]:
        assert "track_signing" not in ride
    assert _KEY not in r.text and _NONCE not in r.text
    list_sql = next(e[0] for e in conn.cur.executed if "FROM tracked_rides" in e[0])
    assert "track_key" not in list_sql
    assert "track_nonce" not in list_sql


def test_the_signing_columns_are_not_in_the_list_column_set():
    """Static form of the guard above, so a future edit that folds track_key
    into _RIDE_COLS fails here even without an endpoint test covering it."""
    for column in ("track_key", "track_nonce", "track_key_issued_at"):
        assert column not in api_tracked_rides._RIDE_COLS
        assert column in api_tracked_rides._RIDE_COLS_OWNER


def test_owner_slice_offset_tracks_the_column_lists():
    """_RIDE_COL_COUNT splits an owner row into ride + signing. It is derived
    from the column string so it cannot drift; this pins that."""
    assert api_tracked_rides._RIDE_COL_COUNT == len(_row())
    assert len(_owner_row()) == api_tracked_rides._RIDE_COL_COUNT + 3


def test_signing_block_is_none_for_a_ride_that_predates_sql_049(monkeypatch):
    """No key was ever minted, so there is nothing to sign with — say so
    plainly rather than hand back a half-built block."""
    c, _ = _client(monkeypatch, [_owner_row(signing=(None, None, None)), None])
    r = c.get(f"/api/v1/tracked-rides/{_RIDE_ID}")
    assert r.status_code == 200, r.text
    assert r.json()["track_signing"] is None


# ---------- §10 reported fields -------------------------------------------

def test_reported_fields_round_trip_through_end(monkeypatch):
    c, conn = _client(monkeypatch, _end_fetches(
        row=_row(reported_minutes=17, reported_plan="equity")))
    r = _patch_end(c, reported_minutes=17, reported_plan="equity")
    assert r.status_code == 200, r.text
    p = _end_bound_params(conn)
    assert p["reported_minutes"] == 17
    assert p["reported_plan"] == "equity"
    assert r.json()["reported_minutes"] == 17
    assert r.json()["reported_plan"] == "equity"


def test_reported_fields_absent_stay_null(monkeypatch):
    c, conn = _client(monkeypatch, _end_fetches())
    assert _patch_end(c).status_code == 200
    p = _end_bound_params(conn)
    assert p["reported_minutes"] is None
    assert p["reported_plan"] is None


@pytest.mark.parametrize("minutes", [-1, 1441, 2000])
def test_reported_minutes_outside_0_1440_is_rejected(minutes):
    """1440 = 24 h: the same "a number we won't stand behind doesn't enter
    the table" rule as the 80 km distance cap."""
    r = _patch_end(TestClient(_app()), reported_minutes=minutes)
    assert r.status_code == 422


@pytest.mark.parametrize("minutes", [0, 1440])
def test_reported_minutes_at_the_bounds_is_accepted(monkeypatch, minutes):
    c, conn = _client(monkeypatch, _end_fetches())
    assert _patch_end(c, reported_minutes=minutes).status_code == 200
    assert _end_bound_params(conn)["reported_minutes"] == minutes


def test_reported_minutes_is_not_reconciled_against_the_observed_duration(monkeypatch):
    """The whole point of a reported field: 5 minutes reported on a ride the
    server watched for 30 is stored as reported, not corrected or refused."""
    c, conn = _client(monkeypatch, _end_fetches())
    assert _patch_end(c, reported_minutes=5).status_code == 200
    assert _end_bound_params(conn)["reported_minutes"] == 5


@pytest.mark.parametrize("plan", ["resident", "visitor", "equity"])
def test_reported_plan_vocabulary_is_accepted(monkeypatch, plan):
    c, conn = _client(monkeypatch, _end_fetches())
    assert _patch_end(c, reported_plan=plan).status_code == 200
    assert _end_bound_params(conn)["reported_plan"] == plan


@pytest.mark.parametrize("plan", ["gold", "RESIDENT", "", "pass"])
def test_reported_plan_outside_the_rate_plan_vocabulary_is_rejected(plan):
    r = _patch_end(TestClient(_app()), reported_plan=plan)
    assert r.status_code == 422


def test_row_to_ride_returns_the_reported_fields_and_the_options_blob():
    ride = api_tracked_rides._row_to_ride(_row(
        reported_minutes=9, reported_plan="visitor",
        ride_options={"save_tracks": True}))
    assert ride["reported_minutes"] == 9
    assert ride["reported_plan"] == "visitor"
    assert ride["ride_options"] == {"save_tracks": True}
    assert ride["validation"] == {"status": "pending", "reasons": []}


def test_row_to_ride_defaults_a_missing_options_blob_to_an_object():
    ride = api_tracked_rides._row_to_ride(_row(ride_options=None))
    assert ride["ride_options"] == {}


def test_the_reported_fields_are_in_the_list_payload_too():
    """§10's fields ride in _RIDE_COLS, so the list view carries them —
    unlike track_signing, they are the rider's own report."""
    ride = api_tracked_rides._row_to_ride(
        _row(reported_minutes=3, reported_plan="resident"), path_geojson=False)
    assert ride["reported_minutes"] == 3
    assert ride["reported_plan"] == "resident"


# ---------- provisional validation ----------------------------------------

def test_provisional_validation_is_ineligible_without_save_tracks():
    """Terminal: there will never be a track to donate, and the reason token
    is the one A2's donation endpoint 422s with."""
    for options in ({"save_tracks": False}, {}, None, {"save_tracks": "yes"}):
        assert api_tracked_rides._provisional_validation(
            options, gbfs_reappeared_at=_NOW,
        ) == ("ineligible", ["tracking_not_opted"])


def test_provisional_validation_waits_on_the_feed():
    assert api_tracked_rides._provisional_validation(
        {"save_tracks": True}, gbfs_reappeared_at=None,
    ) == ("pending_feed", [])


def test_provisional_validation_is_pending_when_only_the_donation_is_outstanding():
    assert api_tracked_rides._provisional_validation(
        {"save_tracks": True}, gbfs_reappeared_at=_NOW,
    ) == ("pending", [])


def test_provisional_validation_never_reaches_eligible():
    """No track has been donated at /end, so nothing here may call one
    verified. A2 owns the authoritative status."""
    for options in ({"save_tracks": True}, {"save_tracks": False}, {}):
        for reappeared in (None, _NOW):
            status, _ = api_tracked_rides._provisional_validation(
                options, gbfs_reappeared_at=reappeared)
            assert status != "eligible"
            assert status in api_tracked_rides.VALIDATION_STATUSES


def test_end_writes_the_provisional_status_and_settles_only_when_terminal(monkeypatch):
    # gbfs_reappeared: the feed has ALREADY resolved, so the only thing
    # making this ride ineligible is that the rider never opted into saving
    # tracks — the check that has to win over 'pending'. The extra two
    # fetches are the GBFS-corroboration award that resolution unlocks
    # (cap-headroom probe, then the ledger INSERT), which A1 leaves alone.
    c, conn = _client(monkeypatch, [
        _end_select(ride_options={}, gbfs_reappeared=True),
        (0,), (0,), (78, _NOW),
        _row(validation_status="ineligible",
             validation_reasons=["tracking_not_opted"]),
    ])
    r = _patch_end(c)
    assert r.status_code == 200, r.text
    p = _end_bound_params(conn)
    assert p["validation_status"] == "ineligible"
    assert json.loads(p["validation_reasons"]) == ["tracking_not_opted"]
    assert p["validated_at"] is not None, "a terminal status stamps validated_at"
    assert r.json()["validation"] == {
        "status": "ineligible", "reasons": ["tracking_not_opted"]}


def test_end_leaves_validated_at_null_while_something_is_still_outstanding(monkeypatch):
    c, conn = _client(monkeypatch, _end_fetches(
        ride_options={"save_tracks": True},
        row=_row(validation_status="pending_feed")))
    assert _patch_end(c).status_code == 200
    p = _end_bound_params(conn)
    assert p["validation_status"] == "pending_feed"
    assert json.loads(p["validation_reasons"]) == []
    assert p["validated_at"] is None, "nothing is decided yet, so nothing is stamped"


def test_end_reads_ride_options_off_the_locked_row(monkeypatch):
    """The provisional status is computed from the row this transaction
    already holds FOR UPDATE, not from a second read that could race."""
    c, conn = _client(monkeypatch, _end_fetches(ride_options={"save_tracks": True}))
    assert _patch_end(c).status_code == 200
    sql, _ = next(e for e in conn.cur.executed if "FOR UPDATE" in e[0])
    assert "ride_options" in sql


# ---------- award behavior is A2's to change, not A1's ---------------------

def test_end_still_credits_waypoint_points(monkeypatch):
    """Award supersession is phase A2. A ride with waypoints credits them
    exactly as before, provisional validation notwithstanding."""
    c, conn = _client(monkeypatch, [
        _end_select(), (3,), (0,), (77, _NOW), _row()])
    assert _patch_end(c).status_code == 200
    insert = next(e for e in conn.cur.executed
                  if e[0].startswith("INSERT INTO user_points"))
    assert insert[1][1] == "waypoint"
    assert insert[1][2] == 6


# ---------- the parameter positions the pre-existing tests pin -------------

def test_appending_the_new_end_columns_did_not_move_the_distance_ones(monkeypatch):
    """tests/test_api_tracked_rides_validation.py asserts on this UPDATE's
    parameters BY INDEX. The §10/validation columns are appended after the
    distance block for exactly that reason; this pins the ordering from this
    side too, so the two files cannot drift apart silently."""
    c, conn = _client(monkeypatch, _end_fetches())
    assert _patch_end(c).status_code == 200
    _, params = next(e for e in conn.cur.executed
                     if e[0].lstrip().startswith("UPDATE tracked_rides SET"))
    # No waypoints, so no `path_polyline = %s` is spliced in and the distance
    # block sits at 6..8 — where it sat before this phase.
    assert params[0:3] == (_ENDED_AT, 39.75, -104.99)
    assert params[7] == "straight_line"
    assert params[-1] == str(_RIDE_ID)
