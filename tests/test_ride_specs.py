"""Ride specs — the FOURTH kind of user_preferences row
(sql/080_ride_specs.sql, src/api_preferences.py).

Fake-cursor tests over the shared in-memory table in
tests/fake_preferences_store.py, which tests/test_ride_usuals.py uses too.
Sharing it is the point: three families of near-identical handlers now live
in one module, kept apart only by `kind` — in every WHERE clause, in the
ON CONFLICT arbiter predicate, and in each kind's partial unique index. The
fake enforces the arbiter/kind agreement itself, so a spec handler that
inherited the Usuals' index name fails here rather than in production, where
it would upsert against the wrong uniqueness rule.

So the questions this file asks that test_ride_usuals.py cannot:

  * can THREE same-named rows coexist and stay separately addressable?
  * does the cap count specs only, with two other kinds already at theirs?
  * is the blob genuinely opaque — does a vocabulary this module has never
    heard of survive a round trip? That is the property that keeps an API
    deploy from standing in front of every new client-side requirement.

The schema-side rules — that `ride_spec` is a permitted kind at all, that it
REQUIRES a name, and that idx_user_prefs_spec_name exists for the upsert to
infer — are Postgres facts and live in tests/test_ride_specs_pg.py.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_preferences
from src.accounts import SessionUser, require_session
from tests.fake_preferences_store import EPOCH as _EPOCH
from tests.fake_preferences_store import FakeConn as _FakeConn
from tests.fake_preferences_store import FakeStore as _FakeStore

_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=_EPOCH, sliding=True, method="google", token_sha256="x",
)
_OTHER_ACCOUNT = 2

# What the frontend actually stores (ALONG_THE_WAY_PLAN.md §5.1): the
# requirements, plus `must` — the list that says which of them are HARD. That
# field is the whole difference between a spec and a map filter, and it is
# also the field this module must never look at.
_SPEC_BLOB = {
    "label": "Commuter",
    "models": ["cosmo", "rover"],
    "features": ["basket"],
    "min_battery": 40,
    "min_quality": "no-risk",
    "must_reach": True,
    "max_walk_minutes": 12,
    "must": ["features", "must_reach"],
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
def test_a_spec_round_trips_verbatim(client):
    assert client.get("/api/v1/profile/ride-specs").json()["ride_specs"] == []

    created = client.put(
        "/api/v1/profile/ride-specs/commuter", json={"settings": _SPEC_BLOB}
    )
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "commuter"
    assert created.json()["settings"] == _SPEC_BLOB

    fetched = client.get("/api/v1/profile/ride-specs/commuter")
    assert fetched.status_code == 200
    assert fetched.json()["settings"] == _SPEC_BLOB

    listed = client.get("/api/v1/profile/ride-specs").json()["ride_specs"]
    assert [s["name"] for s in listed] == ["commuter"]


def test_put_replaces_wholesale_rather_than_merging(client):
    """PUT is a replace, per sql/043's header: a partial merge would require
    the server to know which keys are meaningful, which is exactly what it
    must not know."""
    client.put("/api/v1/profile/ride-specs/commuter", json={"settings": _SPEC_BLOB})
    replaced = client.put(
        "/api/v1/profile/ride-specs/commuter", json={"settings": {"models": None}}
    )
    assert replaced.json()["settings"] == {"models": None}


def test_the_list_is_newest_updated_first(client):
    client.put("/api/v1/profile/ride-specs/a", json={"settings": {}})
    client.put("/api/v1/profile/ride-specs/b", json={"settings": {}})
    client.put("/api/v1/profile/ride-specs/a", json={"settings": {"touched": True}})

    listed = client.get("/api/v1/profile/ride-specs").json()["ride_specs"]
    assert [s["name"] for s in listed] == ["a", "b"]


def test_delete_removes_only_the_named_spec(client, store):
    client.put("/api/v1/profile/ride-specs/keep", json={"settings": {}})
    client.put("/api/v1/profile/ride-specs/drop", json={"settings": {}})

    r = client.delete("/api/v1/profile/ride-specs/drop")
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "name": "drop"}
    assert store.names("ride_spec") == {"keep"}


def test_deleting_an_absent_spec_is_404(client):
    assert client.delete("/api/v1/profile/ride-specs/never").status_code == 404


def test_getting_an_absent_spec_is_404(client):
    r = client.get("/api/v1/profile/ride-specs/never")
    assert r.status_code == 404
    assert "never" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------
def test_specs_are_scoped_to_the_caller(client, store):
    """Another account's spec of the same name is invisible and untouched —
    every statement is keyed on account_id, not just on name."""
    store.seed("ride_spec", "commuter", {"label": "theirs"},
               account_id=_OTHER_ACCOUNT)

    assert client.get("/api/v1/profile/ride-specs").json()["ride_specs"] == []
    assert client.get("/api/v1/profile/ride-specs/commuter").status_code == 404
    assert client.delete("/api/v1/profile/ride-specs/commuter").status_code == 404

    client.put("/api/v1/profile/ride-specs/commuter", json={"settings": {"label": "mine"}})
    assert store.rows[(_OTHER_ACCOUNT, "ride_spec", "commuter")]["settings"] == {
        "label": "theirs"
    }


def test_a_vanished_account_is_401_not_a_foreign_key_error(client, store):
    store.account_exists = False
    r = client.put("/api/v1/profile/ride-specs/commuter", json={"settings": {}})
    assert r.status_code == 401


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/profile/ride-specs"),
    ("get", "/api/v1/profile/ride-specs/commuter"),
    ("put", "/api/v1/profile/ride-specs/commuter"),
    ("delete", "/api/v1/profile/ride-specs/commuter"),
])
def test_every_spec_endpoint_needs_a_session(store, method, path):
    c = TestClient(_app(authed=False))
    r = getattr(c, method)(path, **({"json": {"settings": {}}} if method == "put" else {}))
    assert r.status_code == 401, r.text
    assert store.rows == {}


# ---------------------------------------------------------------------------
# Three kinds, one table
# ---------------------------------------------------------------------------
def test_all_three_named_kinds_can_share_one_name(client, store):
    """The namespaces are separate by design (sql/080's header): a rider may
    hold a saved map setting, a Usual AND a spec all called 'commute', reached
    through three endpoints that mean three different things. Fusing them
    would be an accident of sharing a table."""
    store.seed("saved_map_settings", "commute", {"which": "map"})
    store.seed("ride_mode_usual", "commute", {"which": "usual"})
    client.put("/api/v1/profile/ride-specs/commute", json={"settings": {"which": "spec"}})

    assert client.get("/api/v1/profile/map-settings/commute").json()["settings"] == {"which": "map"}
    assert client.get("/api/v1/profile/ride-usuals/commute").json()["settings"] == {"which": "usual"}
    assert client.get("/api/v1/profile/ride-specs/commute").json()["settings"] == {"which": "spec"}


def test_a_spec_delete_leaves_the_other_kinds_alone(client, store):
    store.seed("saved_map_settings", "commute", {"which": "map"})
    store.seed("ride_mode_usual", "commute", {"which": "usual"})
    client.put("/api/v1/profile/ride-specs/commute", json={"settings": {}})

    assert client.delete("/api/v1/profile/ride-specs/commute").status_code == 200
    assert store.names("saved_map_settings") == {"commute"}, "a spec DELETE hit a map setting"
    assert store.names("ride_mode_usual") == {"commute"}, "a spec DELETE hit a Usual"


def test_a_usual_endpoint_cannot_read_a_spec(client, store):
    """The inverse direction, which a shared-kind bug would also break."""
    client.put("/api/v1/profile/ride-specs/commute", json={"settings": {}})
    assert client.get("/api/v1/profile/ride-usuals/commute").status_code == 404
    assert client.get("/api/v1/profile/map-settings/commute").status_code == 404


# ---------------------------------------------------------------------------
# The cap: 5 per account
# ---------------------------------------------------------------------------
def test_the_cap_is_five():
    """Pinned as a constant, not just as behaviour. ALONG_THE_WAY_PLAN §5.3
    names five rather than the Usuals' ten, and the number is the contract the
    spec sheet's picker is designed around."""
    assert api_preferences.MAX_RIDE_SPECS == 5


def test_the_sixth_spec_is_409(client):
    for i in range(api_preferences.MAX_RIDE_SPECS):
        r = client.put(f"/api/v1/profile/ride-specs/s{i}", json={"settings": {}})
        assert r.status_code == 200, r.text

    r = client.put("/api/v1/profile/ride-specs/one-too-many", json={"settings": {}})
    assert r.status_code == 409, r.text
    assert "5" in r.json()["detail"]


def test_at_the_cap_an_existing_spec_can_still_be_edited(client):
    """A limit on how much you may store, not a lock on your own data."""
    for i in range(api_preferences.MAX_RIDE_SPECS):
        client.put(f"/api/v1/profile/ride-specs/s{i}", json={"settings": {"v": i}})

    r = client.put("/api/v1/profile/ride-specs/s0", json={"settings": {"v": 99}})
    assert r.status_code == 200, "at the cap, editing an existing spec was refused"
    assert r.json()["settings"] == {"v": 99}


def test_the_three_caps_are_counted_separately(client, store):
    """A rider at both other caps still has all five spec slots: the COUNT(*)
    is filtered on kind."""
    for i in range(api_preferences.MAX_SAVED_MAP_SETTINGS):
        store.seed("saved_map_settings", f"m{i}", {})
    for i in range(api_preferences.MAX_RIDE_USUALS):
        store.seed("ride_mode_usual", f"u{i}", {})

    for i in range(api_preferences.MAX_RIDE_SPECS):
        r = client.put(f"/api/v1/profile/ride-specs/s{i}", json={"settings": {}})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# The blob
# ---------------------------------------------------------------------------
def test_the_blob_is_stored_opaquely(client):
    """A vocabulary this module has never heard of survives a round trip.

    This is the property that keeps an API deploy from standing in front of
    every new client-side requirement — and that keeps a rider's saved spec
    editable on the day the vocabulary changes. The strict shape check lives
    in the trip search, which is the one endpoint that disqualifies vehicles
    against a spec and therefore has to understand it.
    """
    weird = {"min_hoverboard_thrust": 9000, "must": ["min_hoverboard_thrust"],
             "nested": {"deep": [1, 2, {"three": None}]}}
    r = client.put("/api/v1/profile/ride-specs/weird", json={"settings": weird})
    assert r.status_code == 200, r.text
    assert client.get("/api/v1/profile/ride-specs/weird").json()["settings"] == weird


def test_an_oversized_blob_is_413_and_is_not_stored(client, store):
    huge = {"pad": "x" * (api_preferences.MAX_BLOB_BYTES + 1)}
    r = client.put("/api/v1/profile/ride-specs/big", json={"settings": huge})
    assert r.status_code == 413, r.text
    assert "16 KB" in r.json()["detail"]
    assert store.rows == {}, "an oversized spec was stored anyway"


def test_a_blob_just_inside_the_cap_is_accepted(client):
    pad = api_preferences.MAX_BLOB_BYTES - len(json.dumps({"pad": ""}).encode())
    r = client.put(
        "/api/v1/profile/ride-specs/big", json={"settings": {"pad": "x" * pad}}
    )
    assert r.status_code == 200, r.text


def test_a_name_longer_than_the_column_is_422(client, store):
    long_name = "n" * (api_preferences.MAX_NAME_LENGTH + 1)
    r = client.put(f"/api/v1/profile/ride-specs/{long_name}", json={"settings": {}})
    assert r.status_code == 422, r.text
    assert store.rows == {}
