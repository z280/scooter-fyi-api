"""My Scooters (sql/081_favorite_devices.sql, src/api_favorites.py).

Fake-cursor tests. Two rules carry this feature and they are different
mechanisms, so they are tested apart:

  THE GATE. A valid QR payload is not enough — `validate_scan` only proves the
  caller has the plate, and nothing has ever compared the submitted position
  to the vehicle's. So the proximity half is tested on its own, including the
  case that would otherwise be the reliable way past it: a vehicle we have no
  position for.

  THE WITHHOLDING. A favourite's position is not returned while it is in a
  rental. This is the rule most likely to be broken by a later change (it
  looks like a missing field), so it is tested from several directions: both
  signals that mean "in use", the battery reading that is a coarse track of
  its own, and the explicit flag that exists so nobody "fixes" the absence.

The state machine gets its own block because its ORDER differs from the map's
on purpose — `in_use` beats `unavailable` here, since telling a rider "out of
service" about a scooter somebody is riding is simply wrong.

Postgres facts — the unique constraint, the cascade, the partial index — live
in tests/test_favorite_devices_pg.py.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_favorites
from src.accounts import SessionUser, require_session
from src.identity import hash_plate

#: The fixture clock, frozen — and the handler is made to read it too (see
#: the `store` fixture's monkeypatch of `api_favorites._now`). The first
#: version of this file froze only the fixture side, so "seen an hour ago"
#: meant an hour before a date in the past, and every feed-absence test
#: drifted into `gone` as the calendar moved past it. Both sides or neither.
_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)

_PLATE = "10-25 543"
_VID = hash_plate(_PLATE)
_QR = f"https://veoride.com/x?number={_PLATE}"
_CYCLE = uuid.uuid4()

# Standing at the scooter, near enough.
_AT_SCOOTER = {"lat": 39.7392, "lng": -104.9903}
_DEVICE_POS = (39.7392, -104.9903)


class _FakeStore:
    """An in-memory favorite_devices plus just enough of the fleet.

    Holds `feed` (this cycle's raw_telemetry_points row, or None for a vehicle
    absent from the feed) separately from `device` (device_state), because the
    interesting states are exactly the ones where those two disagree.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], dict] = {}
        self.account_exists = True
        self.device: dict[str, dict] = {
            _VID: {
                "current_lat": _DEVICE_POS[0],
                "current_lon": _DEVICE_POS[1],
                "last_observed_at": _NOW,
                "rental_started_at": None,
            }
        }
        self.feed: dict[str, dict | None] = {
            _VID: {
                "latitude": _DEVICE_POS[0],
                "longitude": _DEVICE_POS[1],
                "is_disabled": False,
                "is_reserved": False,
                "current_range_meters": 12000,
                "vehicle_model_name": "Cosmo",
                "vehicle_use_type": "sitting",
            }
        }
        self.points_awarded: list[str] = []

    def seed(self, vid: str, *, account_id: int = 1, **over) -> None:
        self.rows[(account_id, vid)] = {
            "nickname": None, "verified_at": _NOW, "notify_on_available": False,
            "created_at": _NOW, "last_seen_at": _NOW, **over,
        }


class _FakeCursor:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self._result: list[tuple] = []
        self.rowcount = 0

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        params = tuple(params)
        self._result = []
        self.rowcount = 0

        if s.startswith("SELECT cycle_id FROM observation_cycles"):
            self._result = [(_CYCLE,)]
            return
        if s.startswith("SELECT 1 FROM accounts"):
            self._result = [(1,)] if self._store.account_exists else []
            return
        if s.startswith("SELECT current_lat, current_lon, last_observed_at"):
            (vid,) = params
            d = self._store.device.get(vid)
            self._result = (
                [(d["current_lat"], d["current_lon"], d["last_observed_at"])]
                if d else []
            )
            return
        if s.startswith("SELECT COUNT(*) FROM favorite_devices"):
            account_id, vid = params
            self._result = [(sum(
                1 for (a, v) in self._store.rows if a == account_id and v != vid
            ),)]
            return
        if s.startswith("INSERT INTO favorite_devices"):
            account_id, vid, nickname, last_seen = params
            existing = self._store.rows.get((account_id, vid))
            inserted = existing is None
            row = existing or {
                "nickname": None, "verified_at": _NOW,
                "notify_on_available": False, "created_at": _NOW,
                "last_seen_at": last_seen,
            }
            row["verified_at"] = _NOW
            row["last_seen_at"] = last_seen
            if nickname is not None:
                row["nickname"] = nickname
            self._store.rows[(account_id, vid)] = row
            self._result = [(inserted,)]
            return
        if s.startswith("UPDATE favorite_devices SET"):
            *values, account_id, vid = params
            row = self._store.rows.get((account_id, vid))
            if row is None:
                return
            if "nickname = %s" in s:
                row["nickname"] = values.pop(0)
            if "notify_on_available = %s" in s:
                row["notify_on_available"] = values.pop(0)
            self._result = [(1,)]
            return
        if s.startswith("DELETE FROM favorite_devices"):
            account_id, vid = params
            self.rowcount = 1 if self._store.rows.pop((account_id, vid), None) else 0
            return
        if s.startswith("SELECT f.vehicle_identifier, f.nickname"):
            _cycle, account_id = params
            out = []
            for (a, vid), row in self._store.rows.items():
                if a != account_id:
                    continue
                feed = self._store.feed.get(vid)
                dev = self._store.device.get(vid, {})
                out.append((
                    vid, row["nickname"], row["verified_at"],
                    row["notify_on_available"], row["created_at"],
                    row["last_seen_at"],
                    feed["latitude"] if feed else None,
                    feed["longitude"] if feed else None,
                    feed["is_disabled"] if feed else None,
                    feed["is_reserved"] if feed else None,
                    feed["current_range_meters"] if feed else None,
                    feed["vehicle_model_name"] if feed else None,
                    feed["vehicle_use_type"] if feed else None,
                    dev.get("last_observed_at"),
                    dev.get("rental_started_at"),
                ))
            self._result = out
            return
        raise AssertionError(f"unexpected SQL reached the fake cursor: {s}")


class _FakeConn:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        self.commits += 1


@pytest.fixture()
def store(monkeypatch) -> _FakeStore:
    st = _FakeStore()

    @contextmanager
    def _fake_connection():
        yield _FakeConn(st)

    monkeypatch.setattr(api_favorites, "connection", _fake_connection)
    monkeypatch.setattr(api_favorites, "enforce", lambda cur, **kw: None)
    # The handler's clock, frozen to the fixture's. Without this the
    # feed-absence tests below compare a fixed timestamp against the real
    # date and every one of them reads `gone` on any day but the one this
    # file was written.
    monkeypatch.setattr(api_favorites, "_now", lambda: _NOW)

    def _credit(cur, *, account_id, vehicle_identifier, lat, lng):
        if vehicle_identifier in st.points_awarded:
            return None
        st.points_awarded.append(vehicle_identifier)
        return {"points": 100}

    monkeypatch.setattr(api_favorites, "credit_qr_scan_points", _credit)
    return st


def _app(*, authed: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(api_favorites.router)
    if authed:
        app.dependency_overrides[require_session] = lambda: _USER
    return app


@pytest.fixture()
def client(store) -> TestClient:
    return TestClient(_app())


def _body(**over):
    return {"vehicle_identifier": _VID, "qr_raw_value": _QR, **_AT_SCOOTER, **over}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_a_valid_scan_at_the_scooter_keeps_it(client, store):
    r = client.post("/api/v1/profile/favorite-devices", json=_body(nickname="My Rover"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["already_favorited"] is False
    assert body["favorite"]["nickname"] == "My Rover"
    assert body["points_awarded"] == 100


def test_a_qr_for_a_different_vehicle_is_refused(client, store):
    r = client.post(
        "/api/v1/profile/favorite-devices",
        json=_body(qr_raw_value="https://veoride.com/x?number=99-99 999"),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "qr_mismatch"
    assert store.rows == {}


def test_a_valid_scan_from_the_sofa_is_refused(client, store):
    """The half of the gate that does not exist upstream.

    `validate_scan` proves the caller has the plate and nothing more; without
    the proximity check, anyone who learned a plate could keep a scooter they
    have never seen.
    """
    r = client.post(
        "/api/v1/profile/favorite-devices",
        json=_body(lat=39.75, lng=-105.02),  # a couple of km away
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "too_far_from_device"
    assert detail["meters_away"] > api_favorites.FAVORITE_PROXIMITY_METERS
    assert store.rows == {}


def test_a_vehicle_with_no_known_position_cannot_be_kept(client, store):
    """Otherwise "we don't know where it is" is the reliable way past the
    gate — a hole that widens exactly as the feed degrades."""
    store.device[_VID]["current_lat"] = None
    store.device[_VID]["current_lon"] = None
    r = client.post("/api/v1/profile/favorite-devices", json=_body())
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "too_far_from_device"
    assert detail["meters_away"] is None
    assert detail["meters_allowed"] == api_favorites.FAVORITE_PROXIMITY_METERS
    assert store.rows == {}


def test_standing_just_inside_the_radius_is_accepted(client):
    # ~60 m north: inside 75, and the kind of distance a stale GBFS sample
    # plus street-canyon GPS produces for somebody with a hand on the bars.
    r = client.post(
        "/api/v1/profile/favorite-devices",
        json=_body(lat=_DEVICE_POS[0] + 0.00054, lng=_DEVICE_POS[1]),
    )
    assert r.status_code == 201, r.text


def test_an_unknown_vehicle_is_400(client, store):
    store.device.clear()
    r = client.post("/api/v1/profile/favorite-devices", json=_body())
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unknown_device"


def test_re_keeping_one_you_have_is_success_not_a_conflict(client, store):
    client.post("/api/v1/profile/favorite-devices", json=_body(nickname="My Rover"))
    r = client.post("/api/v1/profile/favorite-devices", json=_body())
    assert r.status_code == 201, r.text
    assert r.json()["already_favorited"] is True
    # ...and a re-scan carrying no nickname does not wipe the one they typed.
    assert r.json()["favorite"]["nickname"] == "My Rover"


def test_points_are_paid_once_however_many_times_it_is_kept(client, store):
    first = client.post("/api/v1/profile/favorite-devices", json=_body())
    second = client.post("/api/v1/profile/favorite-devices", json=_body())
    assert first.json()["points_awarded"] == 100
    assert second.json()["points_awarded"] == 0


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/profile/favorite-devices"),
    ("post", "/api/v1/profile/favorite-devices"),
    ("patch", f"/api/v1/profile/favorite-devices/{_VID}"),
    ("delete", f"/api/v1/profile/favorite-devices/{_VID}"),
])
def test_every_endpoint_needs_a_session(store, method, path):
    c = TestClient(_app(authed=False))
    kwargs = {"json": _body()} if method in ("post", "patch") else {}
    assert getattr(c, method)(path, **kwargs).status_code == 401


# ---------------------------------------------------------------------------
# The withholding
# ---------------------------------------------------------------------------
def _first(client) -> dict:
    return client.get("/api/v1/profile/favorite-devices").json()["favorite_devices"][0]


def test_a_parked_favorite_reports_its_position(client, store):
    store.seed(_VID)
    row = _first(client)
    assert row["state"] == "available"
    assert row["position_withheld"] is False
    assert row["lat"] == pytest.approx(_DEVICE_POS[0])
    assert row["battery_percent"] is not None


def test_last_seen_prefers_live_device_state_when_present(client, store):
    store.seed(_VID, last_seen_at=_NOW - timedelta(days=2))
    store.device[_VID]["last_observed_at"] = _NOW - timedelta(minutes=5)
    row = _first(client)
    assert row["last_seen_at"] == (_NOW - timedelta(minutes=5)).isoformat()


def test_an_in_use_favorite_reports_no_position_at_all(client, store):
    """The rule the whole feature turns on. Veo keeps rented vehicles in the
    feed broadcasting a live moving position; a one-tap subscription to one
    specific vehicle somebody physically located is a tool for following a
    person, and this is what stops it being one."""
    store.seed(_VID)
    store.feed[_VID]["is_reserved"] = True
    row = _first(client)
    assert row["state"] == "in_use"
    assert row["position_withheld"] is True
    assert "lat" not in row and "lon" not in row


def test_an_in_use_favorite_reports_no_battery_either(client, store):
    """A battery falling five points every ten minutes is a track too — a
    coarse one, but the same information in a different unit."""
    store.seed(_VID)
    store.feed[_VID]["is_reserved"] = True
    row = _first(client)
    assert "battery_percent" not in row
    assert "current_range_meters" not in row


def test_device_state_alone_is_enough_to_withhold(client, store):
    """Both signals count. src/device_state.py freezes a vehicle's position
    for the duration of a rental, so a stale feed row reporting it
    un-reserved must not be the thing that decides."""
    store.seed(_VID)
    store.feed[_VID]["is_reserved"] = False
    store.device[_VID]["rental_started_at"] = _NOW - timedelta(minutes=4)
    row = _first(client)
    assert row["state"] == "in_use"
    assert row["position_withheld"] is True
    assert "lat" not in row


def test_a_gone_favorite_withholds_a_days_old_position(client, store):
    """Different reason, same answer: the last position we have is days old,
    and publishing it as "where it is" would be a claim we cannot make."""
    store.seed(_VID)
    store.feed[_VID] = None
    store.device[_VID]["last_observed_at"] = _NOW - timedelta(days=3)
    row = _first(client)
    assert row["state"] == "gone"
    assert row["position_withheld"] is True
    assert "lat" not in row


def test_the_flag_is_always_present_so_absence_is_never_guessed_at(client, store):
    store.seed(_VID)
    for reserved in (False, True):
        store.feed[_VID]["is_reserved"] = reserved
        assert "position_withheld" in _first(client)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------
def test_in_use_beats_out_of_service(client, store):
    """Order differs from the map's on purpose: both hide the position, but
    only one of them is a person riding, and "out of service" is the wrong
    thing to tell a rider about a scooter somebody is on."""
    store.seed(_VID)
    store.feed[_VID]["is_reserved"] = True
    store.feed[_VID]["is_disabled"] = True
    assert _first(client)["state"] == "in_use"


def test_a_disabled_vehicle_is_unavailable_not_gone(client, store):
    store.seed(_VID)
    store.feed[_VID]["is_disabled"] = True
    row = _first(client)
    assert row["state"] == "unavailable"
    # Still parked somewhere a rider might want to know about.
    assert row["position_withheld"] is False


def test_a_brief_absence_from_the_feed_is_not_gone(client, store):
    """`device-watch.ts` calls a vehicle gone after two missed polls because a
    rider is WALKING to it. This list is read days later, and the same
    threshold would have every rider's favourites flickering."""
    store.seed(_VID)
    store.feed[_VID] = None
    store.device[_VID]["last_observed_at"] = _NOW - timedelta(hours=1)
    assert _first(client)["state"] == "unavailable"


def test_a_long_absence_is_gone(client, store):
    store.seed(_VID)
    store.feed[_VID] = None
    store.device[_VID]["last_observed_at"] = (
        _NOW - timedelta(hours=api_favorites.GONE_AFTER_HOURS + 1)
    )
    assert _first(client)["state"] == "gone"


# ---------------------------------------------------------------------------
# The list, the cap, and scoping
# ---------------------------------------------------------------------------
def test_the_cap_is_ten():
    assert api_favorites.MAX_FAVORITE_DEVICES == 10


def test_the_eleventh_is_409_and_names_the_cap(client, store):
    for i in range(api_favorites.MAX_FAVORITE_DEVICES):
        store.seed(f"{i:016x}")
    r = client.post("/api/v1/profile/favorite-devices", json=_body())
    assert r.status_code == 409
    assert r.json()["detail"]["max_favorites"] == api_favorites.MAX_FAVORITE_DEVICES


def test_at_the_cap_one_you_already_keep_can_still_be_re_verified(client, store):
    """A limit on how many you may keep, not a lock on the ones you have —
    the same rule the preference caps follow."""
    store.seed(_VID)
    for i in range(api_favorites.MAX_FAVORITE_DEVICES - 1):
        store.seed(f"{i:016x}")
    r = client.post("/api/v1/profile/favorite-devices", json=_body())
    assert r.status_code == 201, r.text


def test_another_account_s_favorites_are_invisible_and_untouchable(client, store):
    store.seed(_VID, account_id=2)
    assert client.get("/api/v1/profile/favorite-devices").json()["favorite_devices"] == []
    assert client.delete(f"/api/v1/profile/favorite-devices/{_VID}").status_code == 404
    assert client.patch(
        f"/api/v1/profile/favorite-devices/{_VID}", json={"nickname": "mine"}
    ).status_code == 404
    assert store.rows[(2, _VID)]["nickname"] is None


def test_rename_and_notification_toggle(client, store):
    store.seed(_VID)
    r = client.patch(
        f"/api/v1/profile/favorite-devices/{_VID}",
        json={"nickname": "My Rover", "notify_on_available": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["favorite"]["nickname"] == "My Rover"
    assert r.json()["favorite"]["notify_on_available"] is True


def test_an_empty_nickname_clears_it(client, store):
    store.seed(_VID, nickname="My Rover")
    r = client.patch(f"/api/v1/profile/favorite-devices/{_VID}", json={"nickname": "  "})
    assert r.json()["favorite"]["nickname"] is None


def test_a_patch_with_nothing_in_it_is_400(client, store):
    store.seed(_VID)
    assert client.patch(
        f"/api/v1/profile/favorite-devices/{_VID}", json={}
    ).status_code == 400


def test_renaming_does_not_need_a_re_scan(client, store):
    """The gate establishes that this vehicle is yours to keep. Making a
    rider stand at the scooter to turn off a notification would be a rule
    with no purpose."""
    store.seed(_VID)
    assert client.patch(
        f"/api/v1/profile/favorite-devices/{_VID}",
        json={"notify_on_available": False},
    ).status_code == 200


def test_delete_and_then_404(client, store):
    store.seed(_VID)
    assert client.delete(f"/api/v1/profile/favorite-devices/{_VID}").status_code == 200
    assert client.delete(f"/api/v1/profile/favorite-devices/{_VID}").status_code == 404


def test_the_list_carries_the_cap_so_the_ui_need_not_hardcode_it(client, store):
    body = client.get("/api/v1/profile/favorite-devices").json()
    assert body["max_favorites"] == api_favorites.MAX_FAVORITE_DEVICES


# ---------------------------------------------------------------------------
# The scan as identity
# ---------------------------------------------------------------------------
def test_a_scan_alone_keeps_the_right_scooter(client, store):
    """No vehicle_identifier at all — the flow a rider gets from the panel,
    where the camera opens on a scooter they never tapped on the map.

    The alternative would have been a plate-extraction copy in the browser to
    compute an identifier the client cannot compute (the hash is salted
    server-side), which is exactly the duplication api_device_features.py
    avoided when it made the same field optional.
    """
    r = client.post(
        "/api/v1/profile/favorite-devices",
        json={"qr_raw_value": _QR, **_AT_SCOOTER},
    )
    assert r.status_code == 201, r.text
    assert r.json()["favorite"]["vehicle_identifier"] == _VID


def test_a_claim_that_disagrees_with_the_sticker_is_refused_not_retargeted(
    client, store
):
    """Unlike a features report, which re-targets because the answers describe
    the scooter the rider was standing at and there is data worth saving.
    "Keep this one" naming one scooter while the sticker names another is a
    client bug, and quietly keeping the other would be the app deciding which
    scooter somebody meant."""
    other = "f" * 16
    r = client.post(
        "/api/v1/profile/favorite-devices",
        json={"vehicle_identifier": other, "qr_raw_value": _QR, **_AT_SCOOTER},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "qr_mismatch"
    assert store.rows == {}
