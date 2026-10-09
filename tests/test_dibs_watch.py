"""src/dibs_watch.py — the pure half.

What is pure here is the only part with interesting cases: which claims'
vehicles this cycle says are out, and what the rider reads when one is. The
database orchestration and the once-only guard are in
tests/test_dibs_watch_pg.py, against a real Postgres, because "it texts once
per claim however many cycles the rental spans" is a statement about rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import api_dibs, dibs_watch
from src.dibs_watch import WatchedClaim
from src.ingest import TaggedDevice

NOW = datetime(2026, 10, 8, 17, 30, tzinfo=timezone.utc)


def _device(vehicle_identifier: str, is_reserved: bool | None = None,
            is_disabled: bool | None = None) -> TaggedDevice:
    return TaggedDevice(
        device_id="bike-1", vehicle_type_id="1", form_factor="scooter",
        lat=39.74, lon=-104.98, spatial_status="denver_core",
        vehicle_identifier=vehicle_identifier,
        is_reserved=is_reserved, is_disabled=is_disabled,
    )


def _claim(vid: str = "aaaa000000000000", minutes_ago: int = 7) -> WatchedClaim:
    return WatchedClaim(
        dibs_id="c1aimId",
        vehicle_identifier=vid,
        vehicle_name="Perseus 🎯 619",
        claimed_at=NOW - timedelta(minutes=minutes_ago),
        account_id=42,
    )


# ---------- taken_claims (pure) ---------------------------------------------

def test_a_reserved_vehicle_is_out():
    """Veo's convention: the vehicle stays in the feed, flagged."""
    claim = _claim()
    observed = {claim.vehicle_identifier: _device(claim.vehicle_identifier, is_reserved=True)}
    assert dibs_watch.taken_claims([claim], observed) == [claim]


def test_a_vehicle_that_left_the_feed_is_out():
    """The other operator convention, and a genuine feed dropout besides."""
    assert dibs_watch.taken_claims([_claim()], observed={}) == [_claim()]


def test_a_free_vehicle_is_not_out():
    claim = _claim()
    observed = {claim.vehicle_identifier: _device(claim.vehicle_identifier, is_reserved=False)}
    assert dibs_watch.taken_claims([claim], observed) == []


def test_a_missing_reservation_flag_reads_as_free():
    """A feed that stops publishing `is_reserved` must not text everybody.
    `None` is "upstream said nothing", which ride_watch already reads as
    available — the degradation is to the old presence-only model, not to
    alerting on the whole fleet."""
    claim = _claim()
    observed = {claim.vehicle_identifier: _device(claim.vehicle_identifier, is_reserved=None)}
    assert dibs_watch.taken_claims([claim], observed) == []


def test_out_of_service_is_not_the_same_as_ridden():
    """is_disabled marks a vehicle taken out of service, not one in use —
    ride_watch's own measurement, and the reason this module reuses its
    reading instead of writing a second one."""
    claim = _claim()
    observed = {
        claim.vehicle_identifier: _device(claim.vehicle_identifier, is_disabled=True)
    }
    assert dibs_watch.taken_claims([claim], observed) == []


def test_only_the_claimed_vehicle_matters():
    """Half the fleet being out says nothing about this claim."""
    claim = _claim(vid="aaaa000000000000")
    observed = {
        "aaaa000000000000": _device("aaaa000000000000", is_reserved=False),
        "bbbb000000000000": _device("bbbb000000000000", is_reserved=True),
    }
    assert dibs_watch.taken_claims([claim], observed) == []


def test_each_claim_is_judged_on_its_own_vehicle():
    mine = _claim(vid="aaaa000000000000")
    theirs = WatchedClaim(
        dibs_id="other", vehicle_identifier="bbbb000000000000",
        vehicle_name="Liftoff 🍉 167", claimed_at=NOW, account_id=9,
    )
    observed = {
        "aaaa000000000000": _device("aaaa000000000000", is_reserved=False),
        "bbbb000000000000": _device("bbbb000000000000", is_reserved=True),
    }
    assert dibs_watch.taken_claims([mine, theirs], observed) == [theirs]


# ---------- alert_text (pure) -----------------------------------------------

def test_the_text_names_the_scooter_and_the_claim_age():
    body = dibs_watch.alert_text(_claim(minutes_ago=7), NOW)
    assert "Perseus 🎯 619" in body
    assert "7 min ago" in body


def test_a_brand_new_claim_says_just_now_rather_than_0_min():
    body = dibs_watch.alert_text(_claim(minutes_ago=0), NOW)
    assert "just now" in body
    assert "0 min" not in body


def test_the_text_does_not_accuse_anybody():
    """THE POINT OF THE WHOLE MODULE'S HONESTY. The feed tells us a rental
    started, never who started it, and the commonest rental on a claimed
    scooter is the claimant's own. A message that states as fact that
    somebody took it is wrong exactly in the case the rider can check."""
    body = dibs_watch.alert_text(_claim(), NOW)
    assert "gone out on rental" in body
    assert "if that is you" in body.lower()
    # No form of "somebody took it" as an assertion.
    lowered = body.lower()
    assert "someone took" not in lowered
    assert "somebody took" not in lowered


def test_the_text_carries_the_certificate_the_rider_would_need():
    claim = _claim()
    body = dibs_watch.alert_text(claim, NOW)
    assert f"{dibs_watch.API_BASE}/dibs/{claim.dibs_id}" in body


def test_the_certificate_host_matches_the_one_that_serves_it():
    """dibs_watch duplicates API_BASE rather than importing api_dibs (which
    would drag the FastAPI app into the ingest worker). Duplication is fine;
    drifting is not — the link in the text would 404."""
    assert dibs_watch.API_BASE == api_dibs.API_BASE
