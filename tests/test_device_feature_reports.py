"""POST /api/v1/reports/device-features (src/api_device_features.py).

Mirrors the fake-cursor idiom of tests/test_device_report_points_credit.py.
The behaviours defended here are the ones a reader would otherwise have to
take on faith from the module docstring:

  * a WRONG plate is a 200 with points_awarded 0, not a 4xx — the owner's
    "we will accept but give no points for wrong entered plate numbers";
  * the award tier is chosen by the status the vehicle carried WHEN the
    report landed (12 / 14 / 6), not by anything the client sent;
  * an anonymous report is stored and earns nothing;
  * the endpoint never grades, never votes, and never writes a feature
    column — that is the ten-minute processor's job alone;
  * the condition follow-up cannot contradict itself.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_device_features
from src.accounts import SessionUser, optional_session
from src.points import (
    POINTS_DEVICE_FEATURES_FIRST,
    POINTS_DEVICE_FEATURES_RECONFIRM,
    POINTS_DEVICE_FEATURES_REVIEW,
)

_VID = "8c4a1f0d2e9b7a35"
_PLATE = "1025543"
_TS = datetime(2026, 7, 5, tzinfo=timezone.utc)
_USER = SessionUser(
    account_id=42, email="rider@example.com", scopes=("rider",),
    expires_at=datetime.now(timezone.utc),
    sliding=True, method="google", token_sha256="x",
)

_BODY = {
    "vehicle_identifier": _VID,
    "device_id": "bike-77",
    "submitted_plate": _PLATE,
    "has_bell": True,
    "has_cup_holder": False,
    "has_phone_holder": True,
    "all_good_condition": True,
    "poor_condition": [],
    "lat": 39.7392,
    "lng": -104.9876,
}


class _FakeCursor:
    def __init__(self, fetch):
        self._fetch = list(fetch)
        self.statements: list[str] = []
        self.params: list[tuple] = []

    def execute(self, sql, params=None, *a, **k):
        self.statements.append(" ".join(str(sql).split()))
        self.params.append(params)

    def fetchone(self):
        return self._fetch.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetch):
        self.cur = _FakeCursor(fetch)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _client(monkeypatch, fetch, *, authenticated=True):
    conn = _FakeConn(fetch)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_device_features, "connection", _fake_connection)
    monkeypatch.setattr(api_device_features, "enforce", lambda cur, **kw: None)
    app = FastAPI()
    app.include_router(api_device_features.router)
    if authenticated:
        app.dependency_overrides[optional_session] = lambda: _USER
    else:
        app.dependency_overrides[optional_session] = lambda: None
    return TestClient(app), conn


def _fetch(status="needs_features_confirmed", plate=_PLATE, awarded=True):
    """The handler's reads, in order:
      1. dedupe probe            -> None (no recent identical report)
      2. device_state lookup     -> (plate, status, h3_10, lat, lon)
      3. report INSERT RETURNING -> (id, reported_at)
    then, only when points are actually credited:
      4. credit_device_feature_points' cooldown probe -> None
      5. credit_points' INSERT RETURNING              -> (id, created_at)
    (pg_advisory_xact_lock and the two UPDATEs return nothing.)
    """
    rows = [None, (plate, status, None, 39.7392, -104.9876), (7, _TS)]
    if awarded:
        rows += [None, (99, _TS)]
    return rows


# --- the plate rule ----------------------------------------------------------

def test_matching_plate_is_valid_and_pays(monkeypatch):
    client, _ = _client(monkeypatch, _fetch())
    r = client.post("/api/v1/reports/device-features", json=_BODY)
    assert r.status_code == 200
    body = r.json()
    assert body["plate_valid"] is True
    assert body["points_awarded"] == POINTS_DEVICE_FEATURES_FIRST


def test_wrong_plate_is_accepted_and_stored_but_pays_nothing(monkeypatch):
    """The owner's rule, verbatim. A 4xx here would both throw away a real
    data-quality signal (riders mixing up adjacent scooters) and hand an
    attacker a plate oracle."""
    client, conn = _client(monkeypatch, _fetch(awarded=False))
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "submitted_plate": "9999999"},
    )
    assert r.status_code == 200
    assert r.json()["plate_valid"] is False
    assert r.json()["points_awarded"] == 0
    inserts = [s for s in conn.cur.statements if "INSERT INTO device_feature_reports" in s]
    assert len(inserts) == 1, "the report is still written"
    assert not any("INSERT INTO user_points" in s for s in conn.cur.statements)


@pytest.mark.parametrize("typed", ["1025543", " 1025543 ", "#1025543", "1025-543"])
def test_plate_matching_forgives_punctuation_and_whitespace(monkeypatch, typed):
    """A rider who typed the right digits read the right scooter, which is
    the only thing this check is actually asking."""
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "submitted_plate": typed},
    )
    assert r.json()["plate_valid"] is True


def test_the_plate_is_stored_as_typed(monkeypatch):
    """Verbatim, so "why did this vehicle flip to needs_review?" stays
    answerable — a rash of near-miss plates reads very differently from a
    rash of empty ones."""
    client, conn = _client(monkeypatch, _fetch(awarded=False))
    client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "submitted_plate": "  10255XX  "},
    )
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO device_feature_reports" in s
    )
    assert "10255XX" in conn.cur.params[idx]


# --- award tiers -------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected",
    [
        ("needs_features_confirmed", POINTS_DEVICE_FEATURES_FIRST),
        ("needs_review", POINTS_DEVICE_FEATURES_REVIEW),
        ("up_to_date", POINTS_DEVICE_FEATURES_RECONFIRM),
    ],
)
def test_the_tier_follows_the_vehicles_status(monkeypatch, status, expected):
    client, _ = _client(monkeypatch, _fetch(status=status))
    r = client.post("/api/v1/reports/device-features", json=_BODY)
    assert r.json()["points_awarded"] == expected
    # Echoed back so the modal can say WHY it paid what it paid.
    assert r.json()["feature_status"] == status


def test_the_award_values_are_the_ones_the_owner_specified():
    """The review tier shipped briefly as 124 — flagged in src/points.py as
    implausible (larger than the whole per-ride cap) and confirmed as a typo
    for 14. Pinned here so a retune is a deliberate edit rather than a
    drive-by."""
    assert POINTS_DEVICE_FEATURES_FIRST == 12
    assert POINTS_DEVICE_FEATURES_REVIEW == 14
    assert POINTS_DEVICE_FEATURES_RECONFIRM == 6


def test_every_award_tier_is_even():
    """The program-wide even-points invariant (sql/053's CHECK, the assert in
    credit_points, and this sweep). 14 satisfies it exactly as 124 did — the
    correction did not quietly break the rule."""
    for value in (POINTS_DEVICE_FEATURES_FIRST, POINTS_DEVICE_FEATURES_REVIEW,
                  POINTS_DEVICE_FEATURES_RECONFIRM):
        assert value % 2 == 0, value


def test_clearing_a_review_outearns_a_reconfirmation():
    """The ordering is the product decision, and it is what a rider actually
    responds to: a needs-review device is the scarcer, more valuable act, so
    it has to be worth more than routine reconfirmation. Pinned because the
    three values are otherwise independent constants that could drift into
    an order nobody intended."""
    assert POINTS_DEVICE_FEATURES_REVIEW > POINTS_DEVICE_FEATURES_FIRST
    assert POINTS_DEVICE_FEATURES_FIRST > POINTS_DEVICE_FEATURES_RECONFIRM


def test_anonymous_reports_are_stored_and_earn_nothing(monkeypatch):
    """Points are never anonymous (sql/028), but the data is still worth
    having — an anonymous report votes in the consensus exactly like any
    other."""
    client, conn = _client(monkeypatch, _fetch(awarded=False), authenticated=False)
    r = client.post("/api/v1/reports/device-features", json=_BODY)
    assert r.status_code == 200
    assert r.json()["points_awarded"] == 0
    assert any(
        "INSERT INTO device_feature_reports" in s for s in conn.cur.statements
    )


# --- separation of concerns --------------------------------------------------

def test_the_endpoint_never_writes_a_feature_column_or_a_status(monkeypatch):
    """The ten-minute processor is the single writer of the consensus. If
    this endpoint ever starts grading inline, the award tier and the
    published state stop being independently reproducible from the log."""
    client, conn = _client(monkeypatch, _fetch())
    client.post("/api/v1/reports/device-features", json=_BODY)
    for sql in conn.cur.statements:
        if "UPDATE device_state" in sql:
            pytest.fail(f"endpoint wrote device_state: {sql}")
        if "feature_status =" in sql:
            pytest.fail(f"endpoint set a feature_status: {sql}")


def test_the_status_the_award_used_is_recorded_on_the_report(monkeypatch):
    """`status_at_report` is what makes a ledger row auditable months later,
    when the vehicle's live status has long since moved on."""
    client, conn = _client(monkeypatch, _fetch(status="needs_review"))
    client.post("/api/v1/reports/device-features", json=_BODY)
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO device_feature_reports" in s
    )
    assert "needs_review" in conn.cur.params[idx]


def test_an_unknown_vehicle_is_a_404(monkeypatch):
    """Unlike a wrong plate: there is no record of the vehicle at all, so
    there is nothing for the report to attach to and no status to award
    against."""
    client, _ = _client(monkeypatch, [None, None])
    r = client.post("/api/v1/reports/device-features", json=_BODY)
    assert r.status_code == 404


def test_a_duplicate_submission_is_a_no_op(monkeypatch):
    """A double-tapped Send must not write a second vote — one rider looking
    at one scooter once is one opinion."""
    dup = (5, _TS, True, POINTS_DEVICE_FEATURES_FIRST, "needs_features_confirmed")
    client, conn = _client(monkeypatch, [dup])
    r = client.post("/api/v1/reports/device-features", json=_BODY)
    assert r.json() == {
        "id": 5,
        "reported_at": _TS.isoformat(),
        "deduped": True,
        "plate_valid": True,
        "points_awarded": POINTS_DEVICE_FEATURES_FIRST,
        "feature_status": "needs_features_confirmed",
        "vehicle_identifier": _VID,
        "qr_matched": None,
    }
    assert not any("INSERT INTO" in s for s in conn.cur.statements)


# --- validation --------------------------------------------------------------

def test_every_presence_answer_is_required(monkeypatch):
    """"Neither pressed by default" is a rule about the modal's initial
    state, not permission to send a half-answered survey."""
    client, _ = _client(monkeypatch, _fetch())
    body = {k: v for k, v in _BODY.items() if k != "has_cup_holder"}
    assert client.post("/api/v1/reports/device-features", json=body).status_code == 422


def test_poor_condition_cannot_name_an_absent_feature(monkeypatch):
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "all_good_condition": False, "poor_condition": ["cup_holder"]},
    )
    assert r.status_code == 422


def test_poor_condition_cannot_name_an_unknown_feature(monkeypatch):
    """`basket` used to be this test's example of an unknown key. sql/058
    made it a real one, so the example moved rather than the rule."""
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "all_good_condition": False, "poor_condition": ["rear_rack"]},
    )
    assert r.status_code == 422


def test_all_good_true_with_an_itemised_fault_is_rejected(monkeypatch):
    """The contradiction is surfaced rather than silently normalised, so a
    client with a bug hears about it instead of having its blanket answer
    quietly overridden."""
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "all_good_condition": True, "poor_condition": ["bell"]},
    )
    assert r.status_code == 422


def test_all_good_false_with_nothing_itemised_is_rejected(monkeypatch):
    """`device_state` stores only the poor-condition list, so an
    un-itemised "something is wrong" would round-trip as "all good" and
    ping-pong the vehicle into needs_review forever."""
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "all_good_condition": False, "poor_condition": []},
    )
    assert r.status_code == 422


def test_a_valid_condition_report_is_accepted(monkeypatch):
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "all_good_condition": False, "poor_condition": ["bell"]},
    )
    assert r.status_code == 200


def test_the_vehicle_identifier_must_be_16_hex(monkeypatch):
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "vehicle_identifier": "not-an-identifier"},
    )
    assert r.status_code == 422


# --- drift-proofing (Copilot review, PR #39) ---------------------------------

def test_poor_condition_is_canonicalised_in_vocabulary_order(monkeypatch):
    """Ordered by FEATURE_KEYS, not by `sorted()`.

    The two agree today only because the vocabulary happens to be
    alphabetical. The dedupe probe compares `poor_condition = %s` against a
    stored array literally, and src/device_features.py's
    FeatureAnswers.normalise() orders by FEATURE_KEYS — so the day a key
    breaks the coincidence ("basket", "rear_rack"), a lexicographic sort here
    would write arrays that no longer match either the stored ones or the
    processor's. This test fails on a reordering of FEATURE_KEYS, which is
    the moment someone needs to know.
    """
    from src.device_features import FEATURE_KEYS

    client, conn = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={
            **_BODY,
            "has_cup_holder": True,
            "all_good_condition": False,
            # Deliberately reversed, and with a duplicate.
            "poor_condition": ["phone_holder", "bell", "bell"],
        },
    )
    assert r.status_code == 200, r.text
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO device_feature_reports" in s
    )
    stored = next(p for p in conn.cur.params[idx] if isinstance(p, list))
    assert stored == [k for k in FEATURE_KEYS if k in {"bell", "phone_holder"}]
    assert stored == ["bell", "phone_holder"]


def test_the_cooldown_probe_derives_its_action_list(monkeypatch):
    """No hand-maintained copy of the award actions in SQL.

    A fourth tier added to FEATURE_STATUS_POINTS but forgotten in a
    hardcoded `action IN (...)` would earn points while being invisible to
    its own cooldown — i.e. farmable on a loop. Deriving the list means
    adding the tier is the only edit required, and this test is what says so.
    """
    from src import points as points_module

    client, conn = _client(monkeypatch, _fetch())
    client.post("/api/v1/reports/device-features", json=_BODY)

    probe_idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "SELECT 1 FROM user_points" in s
    )
    sql = conn.cur.statements[probe_idx]
    for action in points_module.FEATURE_POINT_ACTIONS:
        assert action not in sql, f"{action} is hardcoded in the cooldown SQL"
    assert "= ANY(" in sql
    # The window rides in as a value too, so the statement text is constant.
    assert "make_interval" in sql
    params = conn.cur.params[probe_idx]
    assert list(points_module.FEATURE_POINT_ACTIONS) in params
    assert points_module.FEATURE_POINTS_ACCOUNT_COOLDOWN_HOURS in params


def test_the_derived_action_list_matches_the_mapping():
    from src import points as points_module

    assert set(points_module.FEATURE_POINT_ACTIONS) == {
        action for action, _ in points_module.FEATURE_STATUS_POINTS.values()
    }


# --- the basket (sql/058) ----------------------------------------------------
#
# Asked of every device, not only the models that ship with one — the Trike's
# cargo basket is standard equipment and a bent one has to be reportable.
# OPTIONAL on the wire, and only because the question is newer than the
# clients: the frontend already deployed asks three questions and knows
# nothing about a fourth, so a required field would 422 every report it sends
# the moment this ships.

def _stored_report_params(conn):
    """The params of the INSERT INTO device_feature_reports."""
    idx = next(
        i for i, s in enumerate(conn.cur.statements)
        if "INSERT INTO device_feature_reports" in s
    )
    return conn.cur.params[idx]


def test_a_basket_answer_is_stored(monkeypatch):
    client, conn = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features", json={**_BODY, "has_basket": True},
    )
    assert r.status_code == 200, r.text
    assert True in _stored_report_params(conn)


def test_an_omitted_basket_is_stored_as_NULL_not_false(monkeypatch):
    """The distinction the whole rollout rests on: a client that never asked
    abstains, and NULL is what an abstention looks like in the table. Storing
    False would put "this scooter has no basket" on the record in the name of
    a rider who was never shown the question."""
    client, conn = _client(monkeypatch, _fetch())
    r = client.post("/api/v1/reports/device-features", json=_BODY)
    assert r.status_code == 200, r.text
    params = _stored_report_params(conn)
    # The has_basket slot sits between has_phone_holder and
    # all_good_condition in the INSERT's column list.
    assert params[10] is None


def test_a_basket_may_be_named_in_poor_condition(monkeypatch):
    """The flow that 422'd before this migration: a rider standing at a
    Trike with a bent cargo basket."""
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={
            **_BODY, "has_basket": True,
            "all_good_condition": False, "poor_condition": ["basket"],
        },
    )
    assert r.status_code == 200, r.text


def test_a_report_that_abstained_cannot_itemise_a_basket(monkeypatch):
    """Same rule as any other absent feature — a client that never asked
    about baskets cannot coherently report a broken one."""
    client, _ = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={**_BODY, "all_good_condition": False, "poor_condition": ["basket"]},
    )
    assert r.status_code == 422


def test_poor_condition_orders_the_basket_by_vocabulary_not_alphabet(monkeypatch):
    """`basket` is the key that broke the FEATURE_KEYS/sorted() coincidence
    the old comment here anticipated. Stored arrays are compared literally by
    the dedupe probe, so the endpoint and src/device_features.py have to
    canonicalise identically — they now share `canonical_poor`."""
    from src.device_features import FEATURE_KEYS

    client, conn = _client(monkeypatch, _fetch())
    r = client.post(
        "/api/v1/reports/device-features",
        json={
            **_BODY, "has_basket": True,
            "all_good_condition": False,
            "poor_condition": ["basket", "bell"],
        },
    )
    assert r.status_code == 200, r.text
    stored = next(p for p in _stored_report_params(conn) if isinstance(p, list))
    assert stored == ["bell", "basket"]
    assert stored == [k for k in FEATURE_KEYS if k in {"bell", "basket"}]
    assert stored != sorted(stored)


def test_the_dedupe_probe_matches_an_abstaining_report(monkeypatch):
    """`has_basket = NULL` never equals anything, so `=` would miss every row
    an older client wrote and a double-tapped Send would cast a second vote.
    IS NOT DISTINCT FROM is what makes NULL match NULL."""
    client, conn = _client(monkeypatch, _fetch())
    client.post("/api/v1/reports/device-features", json=_BODY)
    probe = next(
        s for s in conn.cur.statements if "FROM device_feature_reports" in s
    )
    assert "has_basket IS NOT DISTINCT FROM" in probe


# ---------------------------------------------------------------------------
# feature_payload — the wire object, tri-state per feature
# ---------------------------------------------------------------------------

def test_feature_payload_is_none_only_when_nothing_is_known():
    assert api_device_features.feature_payload(None, None, None, None, None) is None


def test_feature_payload_keeps_unknown_features_null_not_false():
    """A survey-known or catalog-seeded vehicle (sql/065/066) knows ONLY its
    basket. The other three must serialize as null — unknown — because false
    is a claim that somebody looked and saw nothing, and publishing it for a
    whole model cohort would be a fleet-wide lie. Clients filtering on
    `=== true` read null and false identically, which is the right answer
    for an equipment filter."""
    payload = api_device_features.feature_payload(None, None, None, [], True)
    assert payload == {
        "bell": None,
        "cup_holder": None,
        "phone_holder": None,
        "basket": True,
        "poor_condition": [],
    }


def test_feature_payload_still_reports_a_real_no_as_false():
    payload = api_device_features.feature_payload(True, False, True, ["bell"], False)
    assert payload == {
        "bell": True,
        "cup_holder": False,
        "phone_holder": True,
        "basket": False,
        "poor_condition": ["bell"],
    }
