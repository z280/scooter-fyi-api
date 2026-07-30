"""Tests for src/api_ride_surveys.py (PLAN_RIDE_MODE_API.md phase A3,
sql/052) and the survey-award functions it drives in src/points.py.

Fake-cursor idiom, following tests/test_ride_usuals.py's small in-memory
store rather than tests/test_ride_session_fields.py's scripted
fetchone-sequence style: this endpoint's handler makes a data-dependent
NUMBER of queries (an extra one only when ride_route_id is present, and the
real src/points.py:credit_points is exercised UNMOCKED so the award-gating
tests are true integration tests of the endpoint + the ledger primitive
together), so a fixed positional list of fetchone() results would be
unreadable to maintain. The fake mirrors the actual tables touched:
tracked_rides, ride_surveys, device_state, ride_routes, user_points.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_ride_surveys, api_tracked_rides
from src.accounts import SessionUser, require_session

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=1, email="rider@example.com", scopes=("rider",),
    expires_at=_NOW, sliding=True, method="google", token_sha256="x",
)
_OTHER_ACCOUNT = 2
_VID = "aaaa000000000000"


# ---------------------------------------------------------------------------
# A small in-memory Postgres standing in for tracked_rides / ride_surveys /
# device_state / ride_routes / user_points.
# ---------------------------------------------------------------------------
class _FakeDB:
    def __init__(self) -> None:
        self.tracked_rides: dict[str, dict[str, Any]] = {}
        self.ride_surveys: dict[str, dict[str, Any]] = {}   # keyed by tracked_ride_id
        self.device_state: dict[str, str | None] = {}       # vehicle_identifier -> model
        self.ride_routes: dict[str, dict[str, Any]] = {}    # keyed by ride_route_id
        self.user_points: list[dict[str, Any]] = []
        self.executed: list[tuple[str, tuple]] = []
        self._next_survey_id = 1
        self._next_points_id = 1

    def add_ride(
        self, ride_id, *, account_id: int = 1, ended: bool = True,
        vehicle_identifier: str = _VID, ride_options: dict | None = None,
        start_lat: float = 39.74, start_lon: float = -104.98,
        vehicle_model: str | None = None,
    ) -> str:
        rid = str(ride_id)
        self.tracked_rides[rid] = {
            "account_id": account_id,
            "user_reported_ended_at": _NOW if ended else None,
            "vehicle_identifier": vehicle_identifier,
            "ride_options": {} if ride_options is None else ride_options,
            "start_lat": start_lat, "start_lon": start_lon,
        }
        self.device_state.setdefault(vehicle_identifier, vehicle_model)
        return rid

    def add_route(
        self, route_id, *, account_id: int | None = 1, tracked_ride_id=None,
    ) -> str:
        rid = str(route_id)
        self.ride_routes[rid] = {
            "account_id": account_id,
            "tracked_ride_id": str(tracked_ride_id) if tracked_ride_id is not None else None,
        }
        return rid

    def points_for(self, action: str) -> list[dict[str, Any]]:
        return [r for r in self.user_points if r["action"] == action]


class _FakeCursor:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db
        self._result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def execute(self, sql, params=()) -> None:
        s = " ".join(sql.split())
        params = tuple(params)
        self._db.executed.append((s, params))
        self._result = []
        db = self._db

        if s.startswith("SELECT user_reported_ended_at, vehicle_identifier, ride_options,"):
            ride_id, account_id = params
            row = db.tracked_rides.get(str(ride_id))
            if row is not None and row["account_id"] == account_id:
                self._result = [(
                    row["user_reported_ended_at"], row["vehicle_identifier"],
                    row["ride_options"], row["start_lat"], row["start_lon"],
                )]
            return

        if s.startswith("SELECT 1 FROM ride_surveys WHERE tracked_ride_id = %s"):
            (ride_id,) = params
            self._result = [(1,)] if str(ride_id) in db.ride_surveys else []
            return

        if s.startswith("SELECT current_vehicle_model_name FROM device_state"):
            (vid,) = params
            if vid in db.device_state:
                self._result = [(db.device_state[vid],)]
            return

        if s.startswith("SELECT tracked_ride_id FROM ride_routes WHERE id = %s AND account_id = %s"):
            route_id, account_id = params
            row = db.ride_routes.get(str(route_id))
            if row is not None and row["account_id"] == account_id:
                self._result = [(row["tracked_ride_id"],)]
            return

        if s.startswith("UPDATE ride_routes SET tracked_ride_id = %s WHERE id = %s"):
            new_ride_id, route_id = params
            db.ride_routes[str(route_id)]["tracked_ride_id"] = str(new_ride_id)
            return

        if s.startswith("INSERT INTO ride_surveys ("):
            (tracked_ride_id, account_id, vehicle_model, would_ride_again, was_perfect,
             issues, model_bonus, nav_route_rating, nav_deviated,
             nav_deviated_needs_improvement, nav_nps, nav_qualitative, ride_route_id) = params
            survey_id = db._next_survey_id
            db._next_survey_id += 1
            db.ride_surveys[str(tracked_ride_id)] = {
                "id": survey_id, "account_id": account_id, "vehicle_model": vehicle_model,
                "would_ride_again": would_ride_again, "was_perfect": was_perfect,
                "issues": issues, "model_bonus": model_bonus,
                "nav_route_rating": nav_route_rating, "nav_deviated": nav_deviated,
                "nav_deviated_needs_improvement": nav_deviated_needs_improvement,
                "nav_nps": nav_nps, "nav_qualitative": nav_qualitative,
                "ride_route_id": ride_route_id,
            }
            self._result = [(survey_id, _NOW)]
            return

        if s.startswith("SELECT COALESCE(SUM(points), 0) FROM user_points"):
            source_table, source_id = params
            total = sum(
                r["points"] for r in db.user_points
                if r["source_table"] == source_table and r["source_id"] == source_id
            )
            self._result = [(total,)]
            return

        if s.startswith("INSERT INTO user_points ("):
            (account_id, action, points, lat, lng, h3_8, vehicle_identifier,
             source_table, source_id) = params
            if source_table is not None and source_id is not None:
                dup = any(
                    r["source_table"] == source_table and r["source_id"] == source_id
                    and r["action"] == action
                    for r in db.user_points
                )
                if dup:
                    return  # ON CONFLICT ... DO NOTHING -> no RETURNING row
            new_id = db._next_points_id
            db._next_points_id += 1
            db.user_points.append({
                "id": new_id, "account_id": account_id, "action": action, "points": points,
                "lat": lat, "lng": lng, "h3_8_index": h3_8,
                "vehicle_identifier": vehicle_identifier,
                "source_table": source_table, "source_id": source_id,
            })
            self._result = [(new_id, _NOW)]
            return

        raise AssertionError(f"unexpected SQL reached the fake cursor: {s}")


class _FakeConn:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self._db)

    def commit(self):
        self.commits += 1


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api_ride_surveys.router)
    app.dependency_overrides[require_session] = lambda: _USER
    return app


@pytest.fixture()
def db(monkeypatch) -> _FakeDB:
    d = _FakeDB()

    @contextmanager
    def _fake_connection():
        yield _FakeConn(d)

    monkeypatch.setattr(api_ride_surveys, "connection", _fake_connection)
    return d


@pytest.fixture()
def client(db) -> TestClient:
    return TestClient(_app())


def _post_survey(client, ride_id, **body):
    return client.post(f"/api/v1/tracked-rides/{ride_id}/survey", json=body)


def _actions(response_json) -> set[str]:
    return {p["action"] for p in response_json["points"]}


# ---------------------------------------------------------------------------
# issues: the 16-item vocabulary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("issue", api_ride_surveys.ISSUE_VOCABULARY)
def test_every_vocabulary_issue_is_accepted(client, db, issue):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, issues=[issue])
    assert r.status_code == 200, r.text
    assert r.json()["issues"] == [issue]


def test_sixteen_item_vocabulary_is_exactly_sixteen():
    assert len(api_ride_surveys.ISSUE_VOCABULARY) == 16


def test_an_issue_outside_the_vocabulary_is_422(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, issues=["not_a_real_issue"])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_issue"


def test_a_mix_of_good_and_bad_issues_is_422(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, issues=["battery", "made_up"])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_issue"


# ---------------------------------------------------------------------------
# model_bonus matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model,key,value", [
    ("Cosmo", "cosmo_front_basket", True),
    ("Apollo", "apollo_top_speed_mph", 22),
    ("Astro", "astro_landscape_holder", False),
])
def test_each_models_valid_bonus_key_is_accepted(client, db, model, key, value):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model=model)
    r = _post_survey(client, ride_id, model_bonus={key: value})
    assert r.status_code == 200, r.text
    assert r.json()["model_bonus"] == {key: value}
    assert db.ride_surveys[ride_id]["vehicle_model"] == model


@pytest.mark.parametrize("model,key,value", [
    ("Astro", "cosmo_front_basket", True),
    ("Astro", "apollo_top_speed_mph", 10),
    ("Cosmo", "astro_landscape_holder", True),
    ("Cosmo", "apollo_top_speed_mph", 10),
    ("Apollo", "cosmo_front_basket", True),
    ("Apollo", "astro_landscape_holder", True),
])
def test_a_bonus_key_for_the_wrong_model_is_422(client, db, model, key, value):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model=model)
    r = _post_survey(client, ride_id, model_bonus={key: value})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_model_bonus"
    assert str(ride_id) not in db.ride_surveys, "a rejected survey must not be stored"


@pytest.mark.parametrize("key,value", [
    ("cosmo_front_basket", True), ("apollo_top_speed_mph", 10),
    ("astro_landscape_holder", True),
])
def test_any_bonus_key_is_422_when_the_model_is_unconfirmed(client, db, key, value):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model=None)
    r = _post_survey(client, ride_id, model_bonus={key: value})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_model_bonus"


def test_an_unknown_model_bonus_key_is_422(client, db):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model="Cosmo")
    r = _post_survey(client, ride_id, model_bonus={"turbo_mode": True})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_model_bonus"


def test_apollo_top_speed_must_be_numeric_in_bounds(client, db):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model="Apollo")
    assert _post_survey(client, ride_id, model_bonus={"apollo_top_speed_mph": 41}).status_code == 422
    ride_id_2 = db.add_ride(uuid.uuid4(), vehicle_model="Apollo")
    assert _post_survey(client, ride_id_2, model_bonus={"apollo_top_speed_mph": -1}).status_code == 422
    ride_id_3 = db.add_ride(uuid.uuid4(), vehicle_model="Apollo")
    ok = _post_survey(client, ride_id_3, model_bonus={"apollo_top_speed_mph": 0})
    assert ok.status_code == 200, ok.text
    ride_id_4 = db.add_ride(uuid.uuid4(), vehicle_model="Apollo")
    ok2 = _post_survey(client, ride_id_4, model_bonus={"apollo_top_speed_mph": 40})
    assert ok2.status_code == 200, ok2.text


def test_a_boolean_bonus_key_rejects_a_non_boolean(client, db):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model="Cosmo")
    r = _post_survey(client, ride_id, model_bonus={"cosmo_front_basket": "yes"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# single-shot / ride-state gates
# ---------------------------------------------------------------------------
def test_a_second_survey_on_the_same_ride_is_409(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    first = _post_survey(client, ride_id, would_ride_again=True)
    assert first.status_code == 200, first.text
    second = _post_survey(client, ride_id, would_ride_again=False)
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "survey_already_submitted"


def test_surveying_a_ride_that_has_not_ended_is_409(client, db):
    ride_id = db.add_ride(uuid.uuid4(), ended=False)
    r = _post_survey(client, ride_id)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ride_not_ended"


def test_surveying_a_ride_owned_by_another_account_is_404(client, db):
    ride_id = db.add_ride(uuid.uuid4(), account_id=_OTHER_ACCOUNT)
    r = _post_survey(client, ride_id)
    assert r.status_code == 404


def test_surveying_a_nonexistent_ride_is_404(client, db):
    r = _post_survey(client, uuid.uuid4())
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# ride_survey award gate: end_survey on/off x own_device true/false
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("end_survey,own_device,expect_award", [
    (True, False, True),
    (False, False, False),
    (True, True, False),
    (False, True, False),
])
def test_ride_survey_award_gate(client, db, end_survey, own_device, expect_award):
    ride_id = db.add_ride(uuid.uuid4(), ride_options={
        "end_survey": end_survey, "own_device": own_device,
    })
    r = _post_survey(client, ride_id, would_ride_again=True)
    assert r.status_code == 200, r.text
    assert ("ride_survey" in _actions(r.json())) == expect_award


def test_ride_survey_award_requires_a_scooter_feedback_field(client, db):
    """end_survey on, not own-device, but nothing in the left pane was
    actually answered -> nothing to award."""
    ride_id = db.add_ride(uuid.uuid4(), ride_options={"end_survey": True, "own_device": False})
    r = _post_survey(client, ride_id)
    assert r.status_code == 200, r.text
    assert "ride_survey" not in _actions(r.json())


@pytest.mark.parametrize("field,value", [
    ("would_ride_again", True), ("was_perfect", False),
    ("issues", ["battery"]),
])
def test_any_scooter_feedback_field_present_triggers_the_gate(client, db, field, value):
    ride_id = db.add_ride(uuid.uuid4(), ride_options={"end_survey": True, "own_device": False})
    r = _post_survey(client, ride_id, **{field: value})
    assert r.status_code == 200, r.text
    assert "ride_survey" in _actions(r.json())


def test_ride_survey_awards_four_points(client, db):
    ride_id = db.add_ride(uuid.uuid4(), ride_options={"end_survey": True, "own_device": False})
    r = _post_survey(client, ride_id, would_ride_again=True)
    award = next(p for p in r.json()["points"] if p["action"] == "ride_survey")
    assert award["points"] == 4


# ---------------------------------------------------------------------------
# nav_route_feedback award gate: rating present/absent x ride_route_id present/absent
# ---------------------------------------------------------------------------
def test_nav_route_feedback_awarded_when_rating_present_and_route_resolves(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    route_id = db.add_route(uuid.uuid4())
    r = _post_survey(client, ride_id, nav_route_rating=8, ride_route_id=route_id)
    assert r.status_code == 200, r.text
    assert "nav_route_feedback" in _actions(r.json())
    award = next(p for p in r.json()["points"] if p["action"] == "nav_route_feedback")
    assert award["points"] == 4
    assert db.ride_routes[route_id]["tracked_ride_id"] == ride_id


def test_nav_route_feedback_not_awarded_without_a_ride_route_id(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, nav_route_rating=8)
    assert r.status_code == 200, r.text
    assert "nav_route_feedback" not in _actions(r.json())


def test_nav_route_feedback_not_awarded_without_a_rating(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    route_id = db.add_route(uuid.uuid4())
    r = _post_survey(client, ride_id, ride_route_id=route_id)
    assert r.status_code == 200, r.text
    assert "nav_route_feedback" not in _actions(r.json())
    # still links the route even though no rating was given
    assert db.ride_routes[route_id]["tracked_ride_id"] == ride_id


# ---------------------------------------------------------------------------
# nav_qualitative_feedback award gate: >=20 chars post-trim
# ---------------------------------------------------------------------------
def test_nineteen_chars_does_not_earn_the_qualitative_award(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, nav_qualitative="a" * 19)
    assert r.status_code == 200, r.text
    assert "nav_qualitative_feedback" not in _actions(r.json())


def test_twenty_chars_earns_the_qualitative_award(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, nav_qualitative="a" * 20)
    assert r.status_code == 200, r.text
    award = next(p for p in r.json()["points"] if p["action"] == "nav_qualitative_feedback")
    assert award["points"] == 6


def test_whitespace_padding_does_not_count_toward_the_threshold(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, nav_qualitative="  " + "a" * 19 + "  ")
    assert r.status_code == 200, r.text
    assert "nav_qualitative_feedback" not in _actions(r.json())


def test_whitespace_padded_twenty_chars_still_earns_the_award(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, nav_qualitative="  " + "a" * 20 + "  ")
    assert r.status_code == 200, r.text
    assert "nav_qualitative_feedback" in _actions(r.json())


# ---------------------------------------------------------------------------
# ride_route_id ownership / linking
# ---------------------------------------------------------------------------
def test_an_unlinked_owned_route_succeeds_and_links(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    route_id = db.add_route(uuid.uuid4(), tracked_ride_id=None)
    r = _post_survey(client, ride_id, ride_route_id=route_id)
    assert r.status_code == 200, r.text
    assert r.json()["ride_route_id"] == route_id
    assert db.ride_routes[route_id]["tracked_ride_id"] == ride_id


def test_a_route_already_linked_to_this_ride_succeeds_idempotently(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    route_id = db.add_route(uuid.uuid4(), tracked_ride_id=ride_id)
    r = _post_survey(client, ride_id, ride_route_id=route_id)
    assert r.status_code == 200, r.text
    assert db.ride_routes[route_id]["tracked_ride_id"] == ride_id


def test_a_route_linked_to_a_different_ride_is_422(client, db):
    other_ride_id = db.add_ride(uuid.uuid4())
    this_ride_id = db.add_ride(uuid.uuid4())
    route_id = db.add_route(uuid.uuid4(), tracked_ride_id=other_ride_id)
    r = _post_survey(client, this_ride_id, ride_route_id=route_id)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_ride_route_id"
    assert db.ride_routes[route_id]["tracked_ride_id"] == other_ride_id, \
        "a rejected link must not overwrite the existing one"
    assert this_ride_id not in db.ride_surveys


def test_a_nonexistent_route_id_is_422(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    r = _post_survey(client, ride_id, ride_route_id=str(uuid.uuid4()))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_ride_route_id"


def test_a_deidentified_route_id_is_422_the_same_way_as_a_nonexistent_one(client, db):
    """de-id nulls ride_routes.account_id (PLAN_RIDE_MODE_API.md's A2/A3
    28h sweep) — the ownership predicate alone excludes it, same 422 as a
    made-up id. A >28h-late survey retries without ride_route_id and still
    earns the scooter-feedback award (tested separately)."""
    ride_id = db.add_ride(uuid.uuid4())
    route_id = db.add_route(uuid.uuid4(), account_id=None, tracked_ride_id=None)
    r = _post_survey(client, ride_id, ride_route_id=route_id)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_ride_route_id"


def test_a_route_owned_by_another_account_is_422(client, db):
    ride_id = db.add_ride(uuid.uuid4())
    route_id = db.add_route(uuid.uuid4(), account_id=_OTHER_ACCOUNT, tracked_ride_id=None)
    r = _post_survey(client, ride_id, ride_route_id=route_id)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "bad_ride_route_id"


def test_a_late_survey_without_ride_route_id_still_earns_the_scooter_feedback_award(client, db):
    """The master plan's 'limited window' rule: forfeiting the nav awards
    by omitting a stale/de-identified ride_route_id does not forfeit
    ride_survey, which has no route dependency at all."""
    ride_id = db.add_ride(uuid.uuid4(), ride_options={"end_survey": True, "own_device": False})
    r = _post_survey(client, ride_id, would_ride_again=True)
    assert r.status_code == 200, r.text
    assert "ride_survey" in _actions(r.json())
    assert "nav_route_feedback" not in _actions(r.json())


# ---------------------------------------------------------------------------
# response shape / vehicle_model stamping
# ---------------------------------------------------------------------------
def test_vehicle_model_is_stamped_from_device_state_not_the_client(client, db):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model="Cosmo")
    r = _post_survey(client, ride_id)
    assert r.status_code == 200, r.text
    assert r.json()["vehicle_model"] == "Cosmo"


def test_vehicle_model_is_null_for_an_unconfirmed_model(client, db):
    ride_id = db.add_ride(uuid.uuid4(), vehicle_model=None)
    r = _post_survey(client, ride_id)
    assert r.status_code == 200, r.text
    assert r.json()["vehicle_model"] is None


def test_response_echoes_the_survey_row_and_a_points_array(client, db):
    ride_id = db.add_ride(uuid.uuid4(), ride_options={"end_survey": True, "own_device": False})
    r = _post_survey(
        client, ride_id,
        would_ride_again=True, was_perfect=False, issues=["battery"],
        nav_route_rating=7, nav_deviated=True,
        nav_deviated_needs_improvement=True, nav_nps=9,
        nav_qualitative="a" * 25,
    )
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["ride_id"] == str(ride_id)
    assert body["would_ride_again"] is True
    assert body["was_perfect"] is False
    assert body["issues"] == ["battery"]
    assert body["nav_route_rating"] == 7
    assert body["nav_deviated"] is True
    assert body["nav_deviated_needs_improvement"] is True
    assert body["nav_nps"] == 9
    assert body["nav_qualitative"] == "a" * 25
    assert isinstance(body["points"], list)
    assert {"ride_survey", "nav_qualitative_feedback"} <= _actions(body)


def test_a_rejected_survey_takes_no_ride_option_gate_on_faith(client, db):
    """own_device defensively blocks the award even though an own-device
    ride cannot honestly reach this endpoint (no tracked_rides row) — the
    gate is enforced against whatever ride_options the row actually
    carries, contradictory or not."""
    ride_id = db.add_ride(uuid.uuid4(), ride_options={"end_survey": True, "own_device": True})
    r = _post_survey(client, ride_id, would_ride_again=True)
    assert r.status_code == 200, r.text
    assert "ride_survey" not in _actions(r.json())


# ---------------------------------------------------------------------------
# survey_submitted flag (src/api_tracked_rides.py:_row_to_ride /
# _survey_submitted_ids) — the mechanism GET/list/active use to compute it
# ---------------------------------------------------------------------------
class _SurveyExistsCursor:
    """A minimal fake covering only the two statements
    api_tracked_rides._survey_submitted_ids issues, standing in for the
    live ride_surveys read every ride payload now does."""

    def __init__(self, submitted_ride_ids) -> None:
        self._submitted = {str(x) for x in submitted_ride_ids}
        self._result: list[tuple] = []

    def execute(self, sql, params=()) -> None:
        s = " ".join(sql.split())
        if s.startswith("SELECT to_regclass"):
            self._result = [("ride_surveys",)]
            return
        if s.startswith("SELECT tracked_ride_id FROM ride_surveys WHERE tracked_ride_id = ANY"):
            (ride_ids,) = params
            self._result = [(str(r),) for r in ride_ids if str(r) in self._submitted]
            return
        raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def test_survey_submitted_flag_flips_after_a_successful_submission(client, db):
    """End to end: the flag is False for a fresh ride, POSTing a survey
    through this router actually persists a ride_surveys row (proven via
    the fake store), and api_tracked_rides._survey_submitted_ids — the
    exact function GET/list/active call to build every ride payload's
    survey_submitted field — flips from False to True once that row
    exists."""
    ride_id = uuid.UUID(db.add_ride(uuid.uuid4()))

    before = api_tracked_rides._survey_submitted_ids(
        _SurveyExistsCursor(submitted_ride_ids=[]), [ride_id])
    assert str(ride_id) not in before

    r = _post_survey(client, ride_id)
    assert r.status_code == 200, r.text
    assert str(ride_id) in db.ride_surveys, "the survey was not actually persisted"

    after = api_tracked_rides._survey_submitted_ids(
        _SurveyExistsCursor(submitted_ride_ids=[ride_id]), [ride_id])
    assert str(ride_id) in after


def test_survey_submitted_ids_is_empty_when_the_table_does_not_exist_yet(monkeypatch):
    """Guards against this lane's PR reaching production ahead of sql/052
    (the sibling ride-routes lane's migration): to_regclass returns NULL
    and every pre-existing ride payload keeps working with the flag simply
    false, rather than 500ing."""
    class _NoTableCursor:
        def execute(self, sql, params=()):
            self._result = [(None,)]

        def fetchone(self):
            return self._result[0]

        def fetchall(self):
            return []

    result = api_tracked_rides._survey_submitted_ids(_NoTableCursor(), [uuid.uuid4()])
    assert result == set()


def test_survey_submitted_ids_is_empty_for_an_empty_ride_list():
    """No ride_ids -> no query at all, not even the to_regclass probe."""
    class _ExplodingCursor:
        def execute(self, *a, **kw):
            raise AssertionError("must not query for an empty ride_ids list")

    assert api_tracked_rides._survey_submitted_ids(_ExplodingCursor(), []) == set()
