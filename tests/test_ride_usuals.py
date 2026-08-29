"""Ride Mode "Usuals" — the third kind of user_preferences row
(sql/050_ride_mode_usuals.sql, src/api_preferences.py).

Fake-cursor tests, following the idiom in tests/test_api_profile.py and
tests/test_api_tracked_rides_validation.py: a monkeypatched connection, a
bare FastAPI() carrying only this router, and require_session overridden.

The fake is a small in-memory user_preferences rather than a scripted list
of fetch results, for one reason: the property most worth testing here is
WHICH ROWS EACH ENDPOINT CAN SEE. Usuals and saved map settings share a
table, a name column and a (account_id, name) uniqueness shape, and are
kept apart only by `kind` — in every WHERE clause, in the ON CONFLICT
arbiter predicate, and in sql/050's partial unique index. A fake that
replayed canned rows without honouring `kind` could not fail a handler that
had the wrong kind literal in it, which is precisely the mistake a
copy-pasted family of endpoints makes.

The schema-side rules — that `ride_mode_usual` is a permitted kind at all,
that it REQUIRES a name, and that the partial unique index exists for the
upsert to infer — are Postgres facts and live in
tests/test_ride_usuals_pg.py.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_preferences
from src.accounts import SessionUser, require_session
# The in-memory user_preferences these tests run against — shared with
# tests/test_ride_specs.py so both kinds are checked against ONE model of the
# table (see that module's own header).
from tests.fake_preferences_store import EPOCH as _EPOCH
from tests.fake_preferences_store import FakeConn as _FakeConn
from tests.fake_preferences_store import FakeStore as _FakeStore

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=_EPOCH, sliding=True, method="google", token_sha256="x",
)
_OTHER_ACCOUNT = 2

# What the frontend actually stores: the Screen 2 options object plus the
# label Screen 2.5 shows in the picker.
_USUAL_BLOB = {
    "label": "Morning commute",
    "ride_options": {
        "cost_hud": True, "speedometer": "digital", "theme": "auto",
        "navigation": True, "save_tracks": True, "battery_modeling": True,
        "nav_improvement": True, "end_survey": False, "own_device": False,
    },
}


def _app(*, authed: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(api_preferences.router)
    if authed:
        app.dependency_overrides[require_session] = lambda: _USER
    return app


@pytest.fixture()
def store(monkeypatch) -> _FakeStore:
    st = _FakeStore()

    @contextmanager
    def _fake_connection():
        yield _FakeConn(st)

    monkeypatch.setattr(api_preferences, "connection", _fake_connection)
    return st


@pytest.fixture()
def client(store) -> TestClient:
    return TestClient(_app())


# ---------------------------------------------------------------------------
# CRUD round trip
# ---------------------------------------------------------------------------
def test_a_usual_round_trips_verbatim(client):
    assert client.get("/api/v1/profile/ride-usuals").json()["ride_usuals"] == []

    created = client.put(
        "/api/v1/profile/ride-usuals/commute", json={"settings": _USUAL_BLOB}
    )
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "commute"
    assert created.json()["settings"] == _USUAL_BLOB

    fetched = client.get("/api/v1/profile/ride-usuals/commute")
    assert fetched.status_code == 200
    assert fetched.json()["settings"] == _USUAL_BLOB

    listed = client.get("/api/v1/profile/ride-usuals").json()["ride_usuals"]
    assert [u["name"] for u in listed] == ["commute"]
    assert listed[0]["settings"] == _USUAL_BLOB

    assert client.delete("/api/v1/profile/ride-usuals/commute").json() == {
        "deleted": True, "name": "commute",
    }
    assert client.get("/api/v1/profile/ride-usuals/commute").status_code == 404
    assert client.get("/api/v1/profile/ride-usuals").json()["ride_usuals"] == []


def test_put_replaces_rather_than_accumulating(client, store):
    client.put("/api/v1/profile/ride-usuals/commute", json={"settings": {"label": "v1"}})
    replaced = client.put(
        "/api/v1/profile/ride-usuals/commute", json={"settings": {"label": "v2"}}
    )
    assert replaced.json()["settings"] == {"label": "v2"}
    listed = client.get("/api/v1/profile/ride-usuals").json()["ride_usuals"]
    assert len(listed) == 1, "replace created a second Usual instead of updating"
    assert store.names("ride_mode_usual") == {"commute"}


def test_replace_keeps_created_at_and_moves_updated_at(client):
    first = client.put(
        "/api/v1/profile/ride-usuals/commute", json={"settings": {"label": "v1"}}
    ).json()
    second = client.put(
        "/api/v1/profile/ride-usuals/commute", json={"settings": {"label": "v2"}}
    ).json()
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]


def test_list_is_newest_updated_first(client):
    for name in ("one", "two", "three"):
        client.put(f"/api/v1/profile/ride-usuals/{name}", json={"settings": {}})
    client.put("/api/v1/profile/ride-usuals/one", json={"settings": {"touched": True}})

    listed = client.get("/api/v1/profile/ride-usuals").json()["ride_usuals"]
    assert [u["name"] for u in listed] == ["one", "three", "two"]


def test_missing_usual_is_404_on_get_and_delete(client):
    assert client.get("/api/v1/profile/ride-usuals/nope").status_code == 404
    r = client.delete("/api/v1/profile/ride-usuals/nope")
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_delete_removes_only_the_named_usual(client, store):
    client.put("/api/v1/profile/ride-usuals/keep", json={"settings": {}})
    client.put("/api/v1/profile/ride-usuals/drop", json={"settings": {}})
    assert client.delete("/api/v1/profile/ride-usuals/drop").status_code == 200
    assert store.names("ride_mode_usual") == {"keep"}


def test_usuals_are_scoped_to_the_caller(client, store):
    """Another account's Usual of the same name is invisible and untouched —
    every statement is keyed on account_id, not just on name."""
    store.seed("ride_mode_usual", "commute", {"label": "theirs"},
               account_id=_OTHER_ACCOUNT)

    assert client.get("/api/v1/profile/ride-usuals").json()["ride_usuals"] == []
    assert client.get("/api/v1/profile/ride-usuals/commute").status_code == 404
    assert client.delete("/api/v1/profile/ride-usuals/commute").status_code == 404

    client.put("/api/v1/profile/ride-usuals/commute", json={"settings": {"label": "mine"}})
    assert store.rows[(_OTHER_ACCOUNT, "ride_mode_usual", "commute")]["settings"] == {
        "label": "theirs"
    }


def test_a_vanished_account_is_401_not_a_foreign_key_error(client, store):
    store.account_exists = False
    r = client.put("/api/v1/profile/ride-usuals/commute", json={"settings": {}})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# The cap: 10 per account
# ---------------------------------------------------------------------------
def test_the_cap_is_ten():
    """Pinned as a constant, not just as behaviour: PLAN_RIDE_MODE_API's
    A1 spec names MAX_RIDE_USUALS = 10, and the number is the contract the
    frontend's picker is designed around."""
    assert api_preferences.MAX_RIDE_USUALS == 10


def test_the_eleventh_usual_is_409(client):
    for i in range(api_preferences.MAX_RIDE_USUALS):
        r = client.put(f"/api/v1/profile/ride-usuals/u{i}", json={"settings": {}})
        assert r.status_code == 200, r.text

    r = client.put("/api/v1/profile/ride-usuals/one-too-many", json={"settings": {}})
    assert r.status_code == 409, r.text
    assert "10" in r.json()["detail"]


def test_at_the_cap_an_existing_usual_can_still_be_edited(client):
    """A limit on how much you may store, not a lock on your own data —
    the same rule the saved-map-settings cap follows."""
    for i in range(api_preferences.MAX_RIDE_USUALS):
        client.put(f"/api/v1/profile/ride-usuals/u{i}", json={"settings": {"v": i}})

    r = client.put("/api/v1/profile/ride-usuals/u0", json={"settings": {"v": 99}})
    assert r.status_code == 200, "at the cap, editing an existing Usual was refused"
    assert r.json()["settings"] == {"v": 99}


def test_the_two_caps_are_counted_separately(client, store):
    """Ten Usuals do not consume any of the fifty map-setting slots, and
    vice versa: the COUNT(*) is filtered on kind."""
    for i in range(api_preferences.MAX_RIDE_USUALS):
        store.seed("saved_map_settings", f"m{i}", {})
    for i in range(api_preferences.MAX_RIDE_USUALS):
        assert client.put(
            f"/api/v1/profile/ride-usuals/u{i}", json={"settings": {}}
        ).status_code == 200

    # And the reverse direction: a rider at the Usuals cap can still save a
    # map setting.
    assert client.put(
        "/api/v1/profile/map-settings/another", json={"settings": {}}
    ).status_code == 200


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def test_the_name_length_limit_matches_the_column(client):
    at_limit = "n" * api_preferences.MAX_NAME_LENGTH
    assert client.put(
        f"/api/v1/profile/ride-usuals/{at_limit}", json={"settings": {}}
    ).status_code == 200

    over = "n" * (api_preferences.MAX_NAME_LENGTH + 1)
    assert client.put(
        f"/api/v1/profile/ride-usuals/{over}", json={"settings": {}}
    ).status_code == 422
    assert client.get(f"/api/v1/profile/ride-usuals/{over}").status_code == 422
    assert client.delete(f"/api/v1/profile/ride-usuals/{over}").status_code == 422


def test_a_usual_cannot_be_created_without_a_name(client, store):
    """There is no nameless Usual: sql/050's name_matches_kind CHECK
    requires one, and the only write path addresses the row BY its name.
    An empty final path segment must not reach the handler at all."""
    r = client.put("/api/v1/profile/ride-usuals/", json={"settings": {}})
    assert r.status_code != 200
    assert store.rows == {}, "an unnamed Usual was stored"


def test_a_name_of_only_whitespace_is_still_a_name(client):
    """Not trimmed and not rejected — the map-settings endpoints do neither,
    and inventing a normalization rule here would make the two kinds
    disagree about what the same string means."""
    assert client.put(
        "/api/v1/profile/ride-usuals/%20", json={"settings": {}}
    ).status_code == 200
    assert client.get("/api/v1/profile/ride-usuals/%20").status_code == 200


# ---------------------------------------------------------------------------
# The 16 KB blob cap, reused from the map settings
# ---------------------------------------------------------------------------
def test_a_blob_over_16_kb_is_413(client, store):
    huge = {"pad": "x" * (api_preferences.MAX_BLOB_BYTES + 100)}
    r = client.put("/api/v1/profile/ride-usuals/big", json={"settings": huge})
    assert r.status_code == 413, r.text
    assert "16 KB" in r.json()["detail"]
    assert store.rows == {}, "an oversized Usual was stored anyway"


def test_a_blob_just_inside_the_cap_is_accepted(client):
    pad = api_preferences.MAX_BLOB_BYTES - len(json.dumps({"pad": ""}).encode())
    r = client.put(
        "/api/v1/profile/ride-usuals/big", json={"settings": {"pad": "x" * pad}}
    )
    assert r.status_code == 200, r.text


def test_the_blob_is_stored_opaquely(client):
    """The shape is the frontend's contract, not this module's. A nonsense
    ride_options value is stored and handed back unchanged — it is checked
    when it is USED to start a ride, by
    api_tracked_rides._serialize_ride_options, which is the one place that
    owns the vocabulary."""
    odd = {"label": "x", "ride_options": {"speedometer": "banana"}, "future_key": [1, 2]}
    assert client.put(
        "/api/v1/profile/ride-usuals/odd", json={"settings": odd}
    ).json()["settings"] == odd


def test_settings_is_required(client):
    assert client.put("/api/v1/profile/ride-usuals/x", json={}).status_code == 422


# ---------------------------------------------------------------------------
# Kind isolation — the whole reason one table can carry three kinds
# ---------------------------------------------------------------------------
def test_a_usual_never_appears_in_the_map_settings_listing(client, store):
    client.put("/api/v1/profile/ride-usuals/commute", json={"settings": {"label": "u"}})
    assert client.get("/api/v1/profile/map-settings").json()["map_settings"] == []
    assert client.get("/api/v1/profile/map-settings/commute").status_code == 404
    assert client.delete("/api/v1/profile/map-settings/commute").status_code == 404
    assert store.names("ride_mode_usual") == {"commute"}, "a map-settings DELETE hit a Usual"


def test_a_map_setting_never_appears_in_the_usuals_listing(client, store):
    client.put("/api/v1/profile/map-settings/commute", json={"settings": {"layer": "heat"}})
    assert client.get("/api/v1/profile/ride-usuals").json()["ride_usuals"] == []
    assert client.get("/api/v1/profile/ride-usuals/commute").status_code == 404
    assert client.delete("/api/v1/profile/ride-usuals/commute").status_code == 404
    assert store.names("saved_map_settings") == {"commute"}, \
        "a Usuals DELETE hit a map setting"


def test_the_same_name_can_hold_both_kinds_independently(client):
    """Both partial unique indexes are on (account_id, name); sql/050's is
    partial on kind, so the two namespaces do not collide."""
    client.put("/api/v1/profile/map-settings/commute", json={"settings": {"which": "map"}})
    client.put("/api/v1/profile/ride-usuals/commute", json={"settings": {"which": "usual"}})

    assert client.get("/api/v1/profile/map-settings/commute").json()["settings"] == {
        "which": "map"
    }
    assert client.get("/api/v1/profile/ride-usuals/commute").json()["settings"] == {
        "which": "usual"
    }


def test_every_usuals_statement_is_filtered_on_the_kind(client, store):
    """Belt and braces on the above: no statement this router issues for a
    Usual may reach user_preferences without binding 'ride_mode_usual'."""
    client.put("/api/v1/profile/ride-usuals/commute", json={"settings": {}})
    client.get("/api/v1/profile/ride-usuals")
    client.get("/api/v1/profile/ride-usuals/commute")
    client.delete("/api/v1/profile/ride-usuals/commute")

    touching_prefs = [
        (sql, params) for sql, params in store.executed
        if "user_preferences" in sql
    ]
    assert touching_prefs
    for sql, params in touching_prefs:
        assert "ride_mode_usual" in params or "ride_mode_usual" in sql, sql


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/profile/ride-usuals"),
        ("get", "/api/v1/profile/ride-usuals/commute"),
        ("put", "/api/v1/profile/ride-usuals/commute"),
        ("delete", "/api/v1/profile/ride-usuals/commute"),
    ],
)
def test_every_usuals_endpoint_requires_a_session(store, method, path):
    """No anonymous door: a Usual is rider-owned state and there is no
    cross-account read of a preference at any visibility."""
    c = TestClient(_app(authed=False))
    r = getattr(c, method)(path, **({"json": {"settings": {}}} if method == "put" else {}))
    assert r.status_code == 401, r.text
    assert store.rows == {}
