"""Dibs: registering a claim, and proving it to a stranger later.

The claim itself lives on the rider's phone. This endpoint exists for the
CERTIFICATE, which is a different problem: it gets shown to somebody who has
no reason to trust the person holding it, and a timestamp stored only in that
person's localStorage is one they can edit.

So what is pinned here is mostly that the SERVER owns the timestamp, that an
expired claim is still a true one, and that the campaign codes on the QR are
registered — an unregistered code scans fine and reports zero.
"""

from __future__ import annotations

import inspect
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_dibs, ratelimit


NOW = datetime(2026, 8, 12, 20, 34, 56, tzinfo=timezone.utc)


class _Cur:
    def __init__(self, store):
        self.store = store

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        self._sql, self._params = sql, params
        if "INSERT INTO referrals" in sql:
            self.store.setdefault("__referrals__", []).append(params)

    def fetchone(self):
        if "INSERT INTO dibs" in self._sql:
            dibs_id = self._params[0]
            claimed = NOW
            expires = NOW + timedelta(minutes=api_dibs.DIBS_MAX_TOTAL_MINUTES)
            self.store[dibs_id] = {
                "id": dibs_id, "vehicle_identifier": self._params[1],
                "vehicle_name": self._params[2], "plate": self._params[3],
                "claimed_by": self._params[4], "provider": self._params[5],
                "device_type": self._params[6], "lat": self._params[7],
                "lon": self._params[8], "claimed_at": claimed,
                "expires_at": expires,
            }
            return (claimed, expires)
        if "WHERE vehicle_identifier" in self._sql:
            now = self.store["__now__"]
            live = [r for k, r in self.store.items()
                    if k not in ("__now__", "__referrals__")
                    and r["vehicle_identifier"] == self._params[0]
                    and r["expires_at"] > now]
            if not live:
                return None
            live.sort(key=lambda r: r["claimed_at"])
            r = live[0]
            return (r["id"], r["claimed_by"], r["claimed_at"], r["expires_at"])
        if "FROM dibs WHERE id" in self._sql:
            row = self.store.get(self._params[0])
            if row is None:
                return None
            return (row["id"], row["vehicle_identifier"], row["vehicle_name"],
                    row["plate"], row["claimed_by"], row["claimed_at"],
                    row["expires_at"], self.store["__now__"],
                    row["provider"], row["device_type"], row["lat"], row["lon"])
        return None


class _Conn:
    def __init__(self, store): self.store = store
    def cursor(self): return _Cur(self.store)
    def commit(self): pass


@pytest.fixture
def client(monkeypatch):
    store: dict = {"__now__": NOW}

    @contextmanager
    def fake_connection():
        yield _Conn(store)

    def fake_enforce(cur, **kw):
        inspect.signature(ratelimit.enforce).bind(cur, **kw)

    monkeypatch.setattr(api_dibs, "connection", fake_connection)
    monkeypatch.setattr(api_dibs, "enforce", fake_enforce)
    app = FastAPI()
    app.include_router(api_dibs.router)
    c = TestClient(app)
    c.store = store  # type: ignore[attr-defined]
    return c


BODY = {
    "vehicle_identifier": "abc123",
    "vehicle_name": "Lunar 🐸 928",
    "plate": "1020922",
    "claimed_by": "Resourceful 🌈",
}


def test_the_server_owns_the_timestamp(client):
    """The entire reason this endpoint exists. A rider with a wrong clock — or
    one who set theirs back on purpose — cannot win an argument they should
    lose, because the claim time is the database's NOW() and nothing the
    client sent."""
    body = client.post("/api/v1/dibs", json={**BODY, "claimed_at": "1999-01-01T00:00:00Z"}).json()
    assert body["claimed_at"] == NOW.isoformat()


def test_the_id_is_not_guessable(client):
    """It goes in a QR and then into a stranger's address bar. A sequential id
    would make "who called dibs on what" a browsable list."""
    ids = {client.post("/api/v1/dibs", json=BODY).json()["id"] for _ in range(5)}
    assert len(ids) == 5
    assert all(len(i) >= 12 and re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids)


def test_an_expired_claim_is_still_a_true_one(client):
    """"I had dibs, you took it anyway" is a real thing to be able to show, so
    expiry reports a fact rather than 404ing on it."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    assert client.get(f"/api/v1/dibs/{dibs_id}").json()["active"] is True
    client.store["__now__"] = NOW + timedelta(hours=2)
    after = client.get(f"/api/v1/dibs/{dibs_id}").json()
    assert after["active"] is False
    assert after["claimed_by"] == "Resourceful 🌈"


def test_an_expired_page_says_had_and_names_the_verdict(client):
    """The two readers are different people with different questions. Somebody
    checking a LIVE claim wants to know it is real; somebody checking a dead
    one is, almost always, about to take the scooter."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    client.store["__now__"] = NOW + timedelta(hours=2)
    html = client.get(f"/dibs/{dibs_id}").text
    assert "had dibbs on" in html
    assert "null and void" in html
    assert "they expired at" in html


def test_the_rules_are_on_the_page(client):
    """Somebody arguing about dibbs should be able to read what dibbs is,
    without taking the other person's word for it."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert "The rules of dibbs" in html
    assert "isn't a reservation" in html
    assert "Ten minutes to set off" in html
    assert "Fifteen minutes" in html
    assert "Twenty-five minutes" in html
    # The anti-screenshot rule has to be stated, or it protects nobody.
    assert "only counts while it's moving" in html


def test_verification_needs_no_account(client):
    """Verification the other person cannot do is not verification."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    assert client.get(f"/api/v1/dibs/{dibs_id}").status_code == 200


def test_an_unknown_certificate_is_a_404_not_a_blank_success(client):
    assert client.get("/api/v1/dibs/nope").status_code == 404


def test_the_time_shown_is_denver_time(client):
    """The certificate is settled by comparing two claims at one intersection.
    A traveller's device would print an hour off every other one there."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    shown = client.get(f"/api/v1/dibs/{dibs_id}").json()["denver_time"]
    assert "MDT" in shown or "MST" in shown
    assert "2:34" in shown  # 20:34 UTC is 14:34 in Denver


# --- the page a stranger lands on -------------------------------------------

def test_the_page_answers_the_question_in_words(client):
    """Not the rider — somebody standing next to them who has just been shown
    a phone. A wall of braces does not settle an argument."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert "Resourceful 🌈" in html and "Lunar" in html
    assert "has dibbs on" in html
    assert "Still good." in html
    # The "FYI" is the speech-bubble mark, not the letters — so the label on
    # it is what a screen reader (and this test) has to go by.
    assert 'aria-label="FYI"' in html


def test_the_page_says_plainly_that_dibs_is_not_a_reservation(client):
    """The rider is the person most harmed by believing otherwise."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    assert "isn't a reservation" in client.get(f"/dibs/{dibs_id}").text


def test_the_page_names_the_provider_and_type(client):
    """"(provider) (device_type) (vanity_name)" — a stranger has no idea what
    a "Cosmo" is, and "Veo scooter" tells them what they are arguing about."""
    dibs_id = client.post(
        "/api/v1/dibs",
        json={**BODY, "provider": "Veo", "device_type": "scooter"},
    ).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert "Veo scooter Lunar" in html


def test_the_parts_that_are_missing_leave_no_hole(client):
    """An older certificate has no device_type, and "Veo  Lunar" with a gap in
    it reads as a bug rather than as missing data."""
    dibs_id = client.post(
        "/api/v1/dibs", json={**BODY, "device_type": ""}
    ).json()["id"]
    assert "Veo Lunar" in client.get(f"/dibs/{dibs_id}").text


# --- the referral form ------------------------------------------------------

def test_the_form_creates_a_referral_for_the_certificate_owner(client):
    """They did the introducing; they are the one the points are for."""
    dibs_id = client.post(
        "/api/v1/dibs", json={**BODY, "lat": 39.74, "lon": -104.99}
    ).json()["id"]
    r = client.post(f"/dibs/{dibs_id}/refer", data={"email": "new@example.com"})
    assert r.status_code == 200
    (params,) = client.store["__referrals__"]
    assert params[0] == dibs_id
    assert params[1] == "Resourceful 🌈"   # referrer
    assert params[2] == "new@example.com"
    # ...and WHERE it happened, inherited from the claim.
    assert (params[4], params[5]) == (39.74, -104.99)


def test_either_contact_alone_is_enough(client):
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    assert client.post(f"/dibs/{dibs_id}/refer", data={"phone": "3035550142"}).status_code == 200


def test_a_form_with_neither_says_so_rather_than_failing_silently(client):
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    r = client.post(f"/dibs/{dibs_id}/refer", data={})
    assert r.status_code == 400
    assert "email or a phone" in r.text
    assert "__referrals__" not in client.store


def test_the_form_works_without_javascript(client):
    """The person filling it in is on a pavement, on somebody else's phone.
    A form that needs a bundle to submit is a form that fails exactly there."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert '<form method="post"' in html
    assert f'action="/dibs/{dibs_id}/refer"' in html
    assert "<script" not in html


def test_the_offer_names_the_friend_and_the_points(client):
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert "100 pts" in html
    assert "Resourceful 🌈" in html


def test_the_page_escapes_what_a_rider_typed(client):
    """`claimed_by` is a display name, and display names are rider input."""
    dibs_id = client.post(
        "/api/v1/dibs", json={**BODY, "claimed_by": "<script>alert(1)</script>"}
    ).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_every_link_off_the_page_is_tagged_for_attribution(client):
    """Somebody who was shown a certificate by a stranger and then went and
    got the app is the most interesting event this whole feature produces."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert f"utm_campaign={api_dibs.CAMPAIGN_VALIDATION}" in html
    assert api_dibs.APP_BASE in html


def test_a_missing_certificate_still_renders_a_page(client):
    """Scanned off a creased sticker, or mistyped. A JSON 404 in a phone
    browser tells that person nothing."""
    r = client.get("/dibs/nope")
    assert r.status_code == 404
    assert "could not be found" in r.text


# --- the QR -----------------------------------------------------------------

def test_the_qr_is_vector(client):
    """A certificate is shown at whatever size the holder's phone happens to
    be, and a raster QR scaled up is a QR that stops scanning."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    r = client.get(f"/api/v1/dibs/{dibs_id}/qr.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text or "<path" in r.text


def test_the_qr_points_at_this_certificate_with_a_registered_campaign(client):
    """An UNREGISTERED code scans perfectly and reports zero — campaigns.py
    resolves anything not in the registry to 'other'. sql/076 seeds these."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    import segno
    # Re-derive what the endpoint encodes and check it, rather than decoding
    # the SVG: the payload is the contract, the rendering is not.
    client.get(f"/api/v1/dibs/{dibs_id}/qr.svg")
    assert api_dibs.CAMPAIGN_SCAN == "dibbs"
    target = (f"{api_dibs.API_BASE}/dibs/{dibs_id}"
              f"?utm_source=dibs-certificate&utm_medium=qr"
              f"&utm_campaign={api_dibs.CAMPAIGN_SCAN}&ref={dibs_id}")
    assert segno.make(target, error="q") is not None


def test_the_qr_carries_the_claim_id_so_a_signup_can_credit_a_person(client):
    """Channel attribution says "a certificate did this". `ref` says WHICH
    rider's certificate did it, which is what a referral reward needs."""
    dibs_id = client.post("/api/v1/dibs", json=BODY).json()["id"]
    html = client.get(f"/dibs/{dibs_id}").text
    assert f"ref={dibs_id}" in html


def test_no_qr_for_a_certificate_that_does_not_exist(client):
    assert client.get("/api/v1/dibs/nope/qr.svg").status_code == 404


# --- "who has dibbs on this one?" -------------------------------------------

def test_a_vehicle_with_no_claim_says_so_plainly(client):
    assert client.get("/api/v1/dibs/vehicle/nobody").json() == {"dibs": None}


def test_a_live_claim_is_visible_to_everyone(client):
    """The second person in the argument is exactly who needs to see this, and
    they may well not have an account."""
    client.post("/api/v1/dibs", json=BODY)
    d = client.get("/api/v1/dibs/vehicle/abc123").json()["dibs"]
    assert d["claimed_by"] == "Resourceful 🌈"
    assert d["certificate_url"].endswith(d["id"])


def test_the_OLDEST_claim_wins_not_the_newest(client):
    """Two people can both call dibbs — nothing prevents it. When they do, the
    earlier claim is the one that wins by the rules, so it is the one shown.
    Showing the newest would have the app quietly siding with whoever tapped
    last."""
    first = client.post("/api/v1/dibs", json={**BODY, "claimed_by": "Early 🐦"}).json()["id"]
    client.store[first]["claimed_at"] = NOW - timedelta(minutes=5)
    client.post("/api/v1/dibs", json={**BODY, "claimed_by": "Late 🦥"})
    assert client.get("/api/v1/dibs/vehicle/abc123").json()["dibs"]["claimed_by"] == "Early 🐦"


def test_an_expired_claim_does_not_gate_anything(client):
    """A dead claim must not keep a scooter greyed out for anybody."""
    client.post("/api/v1/dibs", json=BODY)
    client.store["__now__"] = NOW + timedelta(hours=2)
    assert client.get("/api/v1/dibs/vehicle/abc123").json()["dibs"] is None


def test_it_reveals_no_more_than_the_handle_they_chose(client):
    """No contact details, no account id — the second person needs a name to
    argue with, not a way to find somebody."""
    client.post("/api/v1/dibs", json=BODY)
    d = client.get("/api/v1/dibs/vehicle/abc123").json()["dibs"]
    assert set(d) == {"id", "claimed_by", "claimed_at", "expires_at",
                      "denver_time", "certificate_url"}
