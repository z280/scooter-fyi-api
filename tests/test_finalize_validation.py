"""The validation finisher (PLAN_RIDE_MODE_API.md phase A2, "Validation
finisher"; src/ride_watch.py:finalize_validation).

Fake-cursor tests exercising finalize_validation directly (as opposed to
tests/test_ride_watch.py, which covers its WIRING into
update_watches_for_cycle/src/cli.py:expire_stale_watches). The fake cursor
here routes on distinctive SQL prefixes rather than a plain fetchone()
sequence, because finalize_validation's control flow branches on what each
SELECT finds (no such ride / not pending_feed / no donation / a donation to
settle) and a positional sequence would silently pass even if a branch
issued the wrong number of queries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import ride_watch

_RIDE_ID = "22222222-2222-2222-2222-222222222222"
_DONATION_ID = "33333333-3333-3333-3333-333333333333"
_VID = "aaaa000000000000"
_STARTED_AT = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
_ENDED_AT = datetime(2026, 7, 1, 12, 30, 0, tzinfo=timezone.utc)


def _ride_select_row(
    *,
    validation_status="pending_feed",
    ride_options=None,
    gbfs_reappeared_at=None,
    gbfs_end_lat=None,
    gbfs_end_lon=None,
    feed_start_battery_percent=80,
    reported_start_battery_percent=78.0,
    reported_battery_percent=65.0,
    start_lat=39.74,
    start_lon=-104.98,
    account_id=1,
) -> tuple:
    """Column order matches finalize_validation's own SELECT list."""
    return (
        _VID, {} if ride_options is None else ride_options, validation_status,
        gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon,
        _STARTED_AT, _ENDED_AT,
        feed_start_battery_percent, reported_start_battery_percent,
        reported_battery_percent, start_lat, start_lon, account_id,
    )


class _FakeCursor:
    def __init__(
        self,
        *,
        ride_row: tuple | None,
        donation_row: tuple | None = None,
        last_point: tuple | None = None,
        has_ride_routes: bool = False,
        ride_route_exists: bool = False,
    ):
        self.ride_row = ride_row
        self.donation_row = donation_row
        self.last_point = last_point
        self.has_ride_routes = has_ride_routes
        self.ride_route_exists = ride_route_exists
        self.executed: list[tuple[str, tuple]] = []
        self._pending = None

    def execute(self, sql, params=()):
        joined = " ".join(sql.split())
        self.executed.append((joined, params))

        if "pg_advisory_xact_lock" in joined:
            self._pending = None
        elif joined.startswith("SELECT vehicle_identifier, ride_options"):
            assert "FOR UPDATE" in joined
            self._pending = self.ride_row
        elif joined.startswith("SELECT id, vehicle_model, distance_meters"):
            assert "FROM track_donations" in joined
            assert "points_settled_at IS NULL" in joined
            self._pending = self.donation_row
        elif joined.startswith("SELECT recorded_ms, lat, lon"):
            assert "FROM donated_track_points" in joined
            self._pending = self.last_point
        elif joined == "SELECT to_regclass('ride_routes')":
            self._pending = (12345,) if self.has_ride_routes else (None,)
        elif joined.startswith("SELECT 1 FROM ride_routes"):
            self._pending = (1,) if self.ride_route_exists else None
        elif joined.startswith("UPDATE track_donations SET points_settled_at"):
            self.points_settled_call = params
            self._pending = None
        elif joined.startswith("UPDATE track_donations SET points_awarded"):
            self.points_awarded_call = params
            self._pending = None
        elif joined.startswith("UPDATE tracked_rides SET"):
            self.ride_update_call = params
            self._pending = None
        else:
            raise AssertionError(f"unexpected SQL: {joined}")

    def fetchone(self):
        return self._pending

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# Lock-before-write ordering
# ---------------------------------------------------------------------------

def test_the_advisory_lock_is_the_very_first_statement():
    """THE load-bearing assertion: if a future edit moved the lock after
    the ride SELECT (or dropped it), this fails immediately -- a resolve
    path or the donation transaction that ran a row read/write before
    locking is exactly the inversion PLAN_RIDE_MODE_API.md's A2 spec warns
    deadlocks against a concurrent participant on the same ride."""
    cur = _FakeCursor(ride_row=None)
    ride_watch.finalize_validation(cur, _RIDE_ID)
    assert cur.executed, "expected at least the lock statement"
    first_sql, first_params = cur.executed[0]
    assert first_sql == "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
    assert first_params == (f"ride_validation:{_RIDE_ID}",)


def test_the_lock_is_taken_even_when_the_ride_does_not_exist():
    """Locking unconditionally (not "lock only if we're about to write")
    is what makes the ordering safe against a concurrent donation for the
    SAME ride_id racing a bogus/late call for it."""
    cur = _FakeCursor(ride_row=None)
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result is None
    assert any("pg_advisory_xact_lock" in sql for sql, _ in cur.executed)


# ---------------------------------------------------------------------------
# No-op branches (idempotence)
# ---------------------------------------------------------------------------

def test_no_such_ride_is_a_noop():
    cur = _FakeCursor(ride_row=None)
    assert ride_watch.finalize_validation(cur, _RIDE_ID) is None


@pytest.mark.parametrize("status", ["pending", "eligible", "ineligible", "error"])
def test_a_ride_not_in_pending_feed_is_a_noop(status):
    """Idempotence: a ride already settled (or never reached pending_feed)
    must not be re-decided on a second call."""
    cur = _FakeCursor(ride_row=_ride_select_row(validation_status=status))
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result is None
    # And it never even looks for a donation.
    assert not any("track_donations" in sql for sql, _ in cur.executed)


# ---------------------------------------------------------------------------
# Situation 1: no donation yet -- refresh the provisional status
# ---------------------------------------------------------------------------

def test_no_donation_refreshes_to_pending_once_gbfs_resolves():
    cur = _FakeCursor(ride_row=_ride_select_row(
        ride_options={"save_tracks": True}, gbfs_reappeared_at=_ENDED_AT))
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result == {"ride_id": _RIDE_ID, "status": "pending",
                      "reasons": [], "ingested": False}
    assert cur.ride_update_call[0] == "pending"
    assert cur.ride_update_call[2] is None  # validated_at NOT stamped -- still not terminal


def test_no_donation_and_tracking_never_opted_in_settles_ineligible():
    cur = _FakeCursor(ride_row=_ride_select_row(
        ride_options={}, gbfs_reappeared_at=_ENDED_AT))
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "ineligible"
    assert result["reasons"] == ["tracking_not_opted"]
    assert cur.ride_update_call[2] is not None  # validated_at IS stamped -- terminal


def test_no_donation_and_gbfs_still_unresolved_is_a_clean_noop():
    """The expire_stale_watches call path for a ride that was NEVER
    donated and whose gbfs never resolved: _provisional_validation
    recomputes the same 'pending_feed' it started with (it has no way to
    express "gbfs will now never resolve"), so this must be a genuine
    no-op -- no UPDATE, no result -- rather than perpetually re-writing
    the identical row every time expire_stale_watches re-selects it."""
    cur = _FakeCursor(ride_row=_ride_select_row(
        ride_options={"save_tracks": True}, gbfs_reappeared_at=None))
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result is None
    assert not any(sql.startswith("UPDATE tracked_rides SET") for sql, _ in cur.executed)


def test_no_donation_never_calls_ingest(monkeypatch):
    called = []
    monkeypatch.setattr(ride_watch, "ingest_donated_observation",
                        lambda cur, **kw: called.append(1))
    cur = _FakeCursor(ride_row=_ride_select_row(
        ride_options={"save_tracks": True}, gbfs_reappeared_at=_ENDED_AT))
    ride_watch.finalize_validation(cur, _RIDE_ID)
    assert called == []


# ---------------------------------------------------------------------------
# Situation 2: a pending donation -- settle eligible/ineligible
# ---------------------------------------------------------------------------

def _pending_donation_cursor(*, gbfs_reappeared_at, gbfs_end_lat, gbfs_end_lon,
                              last_point, ride_options=None, points_status="ok",
                              **kwargs):
    return _FakeCursor(
        ride_row=_ride_select_row(
            ride_options={"save_tracks": True} if ride_options is None else ride_options,
            gbfs_reappeared_at=gbfs_reappeared_at,
            gbfs_end_lat=gbfs_end_lat, gbfs_end_lon=gbfs_end_lon,
        ),
        donation_row=(_DONATION_ID, "Cosmo", 4312.5, {"points_status": points_status}),
        last_point=last_point,
        **kwargs,
    )


def test_a_matching_gbfs_end_settles_eligible_and_ingests_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ride_watch, "ingest_donated_observation",
        lambda cur, *, ride_row, donation_row: calls.append((ride_row, donation_row)) or {"id": 1},
    )
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.75001, -104.99001),
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)

    assert result == {"ride_id": _RIDE_ID, "status": "eligible",
                      "reasons": [], "ingested": True, "points_awarded": []}
    assert cur.ride_update_call[0] == "eligible"
    assert len(calls) == 1
    ride_row, donation_row = calls[0]
    assert ride_row == {
        "vehicle_identifier": _VID,
        "track_key_issued_at": _STARTED_AT,
        "user_reported_ended_at": _ENDED_AT,
        "feed_start_battery_percent": 80,
        "reported_start_battery_percent": 78.0,
        "reported_battery_percent": 65.0,
    }
    assert donation_row == {
        "id": _DONATION_ID, "vehicle_model": "Cosmo", "distance_meters": 4312.5,
    }


def test_a_far_last_waypoint_settles_ineligible_end_mismatch(monkeypatch):
    monkeypatch.setattr(ride_watch, "ingest_donated_observation",
                        lambda cur, **kw: pytest.fail("must not ingest on ineligible"))
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.90, -105.20),  # far away
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "ineligible"
    assert result["reasons"] == ["end_mismatch"]
    assert result["ingested"] is False


def test_a_late_last_waypoint_settles_ineligible_end_mismatch(monkeypatch):
    monkeypatch.setattr(ride_watch, "ingest_donated_observation",
                        lambda cur, **kw: pytest.fail("must not ingest on ineligible"))
    too_late_ms = int((_ENDED_AT + timedelta(minutes=30)).timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(too_late_ms, 39.75001, -104.99001),
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "ineligible"
    assert result["reasons"] == ["end_mismatch"]


def test_gbfs_never_resolving_settles_ineligible_not_pending_feed(monkeypatch):
    """The expire_stale_watches call path: the watch window elapsed and
    GBFS never reappeared at all. 'pending_feed' is no longer an available
    answer once the finisher has been asked to settle."""
    monkeypatch.setattr(ride_watch, "ingest_donated_observation",
                        lambda cur, **kw: pytest.fail("must not ingest on ineligible"))
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=None, gbfs_end_lat=None, gbfs_end_lon=None,
        last_point=(int(_ENDED_AT.timestamp() * 1000), 39.75, -104.99),
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "ineligible"
    assert result["reasons"] == ["end_mismatch"]


def test_a_donation_with_no_stored_last_point_is_ineligible(monkeypatch):
    monkeypatch.setattr(ride_watch, "ingest_donated_observation",
                        lambda cur, **kw: pytest.fail("must not ingest on ineligible"))
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=None,
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "ineligible"


# ---------------------------------------------------------------------------
# points_settled_at stamped on BOTH outcomes
# ---------------------------------------------------------------------------

def test_points_settled_at_is_stamped_on_an_eligible_settle(monkeypatch):
    monkeypatch.setattr(ride_watch, "ingest_donated_observation", lambda cur, **kw: {"id": 1})
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.75001, -104.99001),
    )
    ride_watch.finalize_validation(cur, _RIDE_ID)
    assert cur.points_settled_call is not None
    assert cur.points_settled_call[0] is not None  # a real timestamp, not None
    assert cur.points_settled_call[1] == _DONATION_ID


def test_track_donations_points_awarded_column_is_updated_with_the_real_total(monkeypatch):
    """track_donations.points_awarded defaults to 0 at donation time (GBFS
    hadn't resolved, so donate_track never knew the eventual award) --
    finalize_validation must stamp the REAL total once it credits one."""
    monkeypatch.setattr(ride_watch, "ingest_donated_observation", lambda cur, **kw: {"id": 1})
    monkeypatch.setattr(
        ride_watch, "credit_battery_contribution",
        lambda cur, **kw: {"action": "battery_contribution", "points": 12},
    )
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.75001, -104.99001),
        ride_options={"save_tracks": True, "battery_modeling": True},
    )
    ride_watch.finalize_validation(cur, _RIDE_ID)
    assert cur.points_awarded_call == (12, _DONATION_ID)


def test_points_awarded_column_is_left_untouched_when_nothing_is_credited(monkeypatch):
    """The common case (no ride-mode options on, or an ineligible settle)
    must not issue a spurious UPDATE at all."""
    monkeypatch.setattr(ride_watch, "ingest_donated_observation", lambda cur, **kw: {"id": 1})
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.75001, -104.99001),
    )
    ride_watch.finalize_validation(cur, _RIDE_ID)
    assert not hasattr(cur, "points_awarded_call")


def test_points_settled_at_is_stamped_on_an_ineligible_settle():
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=None, gbfs_end_lat=None, gbfs_end_lon=None,
        last_point=None,
    )
    ride_watch.finalize_validation(cur, _RIDE_ID)
    assert cur.points_settled_call is not None
    assert cur.points_settled_call[0] is not None
    assert cur.points_settled_call[1] == _DONATION_ID


# ---------------------------------------------------------------------------
# pending_feed -> eligible triggers ingestion exactly once
# ---------------------------------------------------------------------------

def test_ingestion_happens_exactly_once_on_the_eligible_transition(monkeypatch):
    call_count = [0]

    def _spy(cur, **kw):
        call_count[0] += 1
        return {"id": 1}

    monkeypatch.setattr(ride_watch, "ingest_donated_observation", _spy)
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.75001, -104.99001),
    )
    ride_watch.finalize_validation(cur, _RIDE_ID)
    assert call_count == [1]


def test_a_second_call_after_settling_is_a_clean_noop(monkeypatch):
    """Once validation_status is no longer 'pending_feed' (as it would be
    on a real re-read after the first call committed), a second call must
    not re-ingest or re-stamp points_settled_at."""
    called = []
    monkeypatch.setattr(ride_watch, "ingest_donated_observation",
                        lambda cur, **kw: called.append(1) or {"id": 1})
    cur = _FakeCursor(ride_row=_ride_select_row(validation_status="eligible"))
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result is None
    assert called == []


def test_ingested_is_false_when_ingest_donated_observation_no_ops(monkeypatch):
    """ingest_donated_observation itself can legitimately no-op (e.g. an
    unresolvable battery) -- the finisher's own 'ingested' flag must
    reflect that, not just "we called it"."""
    monkeypatch.setattr(ride_watch, "ingest_donated_observation", lambda cur, **kw: None)
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.75001, -104.99001),
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "eligible"
    assert result["ingested"] is False


# ---------------------------------------------------------------------------
# Late-eligible settle: the "distance-dependent points held" award, per
# src/api_tracked_rides.py:donate_track's own docstring for a pending_feed
# donation. Same gating as the donation endpoint; credit_battery_contribution
# / credit_nav_distance_bonus are monkeypatched (same idiom as
# ingest_donated_observation above) rather than exercised through a second
# fake ledger — the ceiling/dedupe/even-points behavior of those functions
# is tests/test_points_ride_mode.py's job, not this file's.
# ---------------------------------------------------------------------------

def _settle_eligible(monkeypatch, *, ride_options, points_status="ok",
                      battery_return=None, nav_return=None, **cursor_kwargs):
    battery_calls = []
    nav_calls = []
    monkeypatch.setattr(ride_watch, "ingest_donated_observation", lambda cur, **kw: {"id": 1})
    monkeypatch.setattr(
        ride_watch, "credit_battery_contribution",
        lambda cur, **kw: (battery_calls.append(kw), battery_return)[1],
    )
    monkeypatch.setattr(
        ride_watch, "credit_nav_distance_bonus",
        lambda cur, **kw: (nav_calls.append(kw), nav_return)[1],
    )
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(last_ms, 39.75001, -104.99001),
        ride_options=ride_options, points_status=points_status,
        **cursor_kwargs,
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "eligible"
    return result, battery_calls, nav_calls


def test_battery_contribution_is_credited_on_a_late_eligible_settle(monkeypatch):
    result, battery_calls, nav_calls = _settle_eligible(
        monkeypatch,
        ride_options={"save_tracks": True, "battery_modeling": True},
        battery_return={"action": "battery_contribution", "points": 18},
    )
    assert result["points_awarded"] == [{"action": "battery_contribution", "points": 18}]
    assert len(battery_calls) == 1
    call = battery_calls[0]
    assert call["account_id"] == 1
    assert call["vehicle_identifier"] == _VID
    assert call["distance_m"] == 4312.5
    assert call["start_lat"] == 39.74 and call["start_lng"] == -104.98
    assert call["ride_id"] == _RIDE_ID
    assert nav_calls == []


def test_battery_contribution_is_not_credited_for_an_own_device_ride(monkeypatch):
    result, battery_calls, _ = _settle_eligible(
        monkeypatch,
        ride_options={"save_tracks": True, "battery_modeling": True, "own_device": True},
    )
    assert result["points_awarded"] == []
    assert battery_calls == []


def test_battery_contribution_is_not_credited_when_a_battery_end_is_unresolvable(monkeypatch):
    """feed_start_battery_percent AND reported_start_battery_percent both
    NULL -- neither start battery is known, so credit_battery_contribution
    must never be reached even though battery_modeling is on."""
    battery_calls = []
    monkeypatch.setattr(ride_watch, "ingest_donated_observation", lambda cur, **kw: {"id": 1})
    monkeypatch.setattr(
        ride_watch, "credit_battery_contribution",
        lambda cur, **kw: battery_calls.append(kw),
    )
    last_ms = int(_ENDED_AT.timestamp() * 1000)
    cur = _FakeCursor(
        ride_row=_ride_select_row(
            ride_options={"save_tracks": True, "battery_modeling": True},
            gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
            feed_start_battery_percent=None, reported_start_battery_percent=None,
        ),
        donation_row=(_DONATION_ID, "Cosmo", 4312.5, {"points_status": "ok"}),
        last_point=(last_ms, 39.75001, -104.99001),
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "eligible"
    assert result["points_awarded"] == []
    assert battery_calls == []


def test_a_pending_review_donation_holds_the_award_on_late_settle(monkeypatch):
    """track_verify.py's points_status flag ("ok" | "pending_review"),
    computed at donation time and persisted on track_donations.verification
    (see src/api_tracked_rides.py:donate_track) because the raw batches it
    was computed from are long gone by settle time -- read back here to
    apply the SAME hold the donation endpoint applies immediately."""
    result, battery_calls, nav_calls = _settle_eligible(
        monkeypatch,
        ride_options={"save_tracks": True, "battery_modeling": True, "nav_improvement": True},
        points_status="pending_review",
        has_ride_routes=True, ride_route_exists=True,
    )
    assert result["points_awarded"] == []
    assert battery_calls == []
    assert nav_calls == []


def test_nav_distance_bonus_is_credited_when_a_ride_route_row_exists(monkeypatch):
    result, battery_calls, nav_calls = _settle_eligible(
        monkeypatch,
        ride_options={"save_tracks": True, "nav_improvement": True},
        nav_return={"action": "nav_distance_bonus", "points": 6},
        has_ride_routes=True, ride_route_exists=True,
    )
    assert result["points_awarded"] == [{"action": "nav_distance_bonus", "points": 6}]
    assert len(nav_calls) == 1
    assert battery_calls == []


def test_nav_distance_bonus_is_skipped_when_ride_routes_table_does_not_exist(monkeypatch):
    """A3 (sql/052) may not have landed -- to_regclass returns NULL and the
    nav award is gracefully skipped, same to_regclass guard as
    src/api_tracked_rides.py:donate_track and src/cli.py:deidentify_donations."""
    result, _, nav_calls = _settle_eligible(
        monkeypatch,
        ride_options={"save_tracks": True, "nav_improvement": True},
        has_ride_routes=False,
    )
    assert result["points_awarded"] == []
    assert nav_calls == []


def test_nav_distance_bonus_is_skipped_when_no_route_row_is_linked_to_this_ride(monkeypatch):
    result, _, nav_calls = _settle_eligible(
        monkeypatch,
        ride_options={"save_tracks": True, "nav_improvement": True},
        has_ride_routes=True, ride_route_exists=False,
    )
    assert result["points_awarded"] == []
    assert nav_calls == []


def test_both_awards_land_in_points_awarded_together(monkeypatch):
    result, battery_calls, nav_calls = _settle_eligible(
        monkeypatch,
        ride_options={"save_tracks": True, "battery_modeling": True, "nav_improvement": True},
        battery_return={"action": "battery_contribution", "points": 18},
        nav_return={"action": "nav_distance_bonus", "points": 6},
        has_ride_routes=True, ride_route_exists=True,
    )
    assert result["points_awarded"] == [
        {"action": "battery_contribution", "points": 18},
        {"action": "nav_distance_bonus", "points": 6},
    ]
    assert len(battery_calls) == 1
    assert len(nav_calls) == 1


def test_no_awards_at_all_when_neither_option_is_on(monkeypatch):
    result, battery_calls, nav_calls = _settle_eligible(
        monkeypatch, ride_options={"save_tracks": True},
    )
    assert result["points_awarded"] == []
    assert battery_calls == []
    assert nav_calls == []


def test_an_ineligible_late_settle_never_awards_points(monkeypatch):
    """The gate is on `eligible`, not just 'a donation existed' -- an
    end_mismatch settle must not credit anything even with every option on."""
    battery_calls = []
    monkeypatch.setattr(
        ride_watch, "credit_battery_contribution",
        lambda cur, **kw: battery_calls.append(kw),
    )
    cur = _pending_donation_cursor(
        gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
        last_point=(int(_ENDED_AT.timestamp() * 1000), 39.90, -105.20),  # far away
        ride_options={"save_tracks": True, "battery_modeling": True},
    )
    result = ride_watch.finalize_validation(cur, _RIDE_ID)
    assert result["status"] == "ineligible"
    assert result["points_awarded"] == []
    assert battery_calls == []


# ---------------------------------------------------------------------------
# _gbfs_end_matches (pure)
# ---------------------------------------------------------------------------

def test_gbfs_end_matches_within_radius_and_window():
    reappeared = _ENDED_AT
    last_ms = int(reappeared.timestamp() * 1000)
    assert ride_watch._gbfs_end_matches(
        (last_ms, 39.75, -104.99),
        gbfs_reappeared_at=reappeared, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
    ) is True


def test_gbfs_end_rejects_beyond_the_radius():
    reappeared = _ENDED_AT
    last_ms = int(reappeared.timestamp() * 1000)
    assert ride_watch._gbfs_end_matches(
        (last_ms, 39.80, -105.10),
        gbfs_reappeared_at=reappeared, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
    ) is False


def test_gbfs_end_rejects_beyond_the_time_window():
    reappeared = _ENDED_AT
    too_late = int((reappeared + timedelta(minutes=11)).timestamp() * 1000)
    assert ride_watch._gbfs_end_matches(
        (too_late, 39.75, -104.99),
        gbfs_reappeared_at=reappeared, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
    ) is False


def test_gbfs_end_rejects_when_gbfs_never_resolved():
    assert ride_watch._gbfs_end_matches(
        (0, 39.75, -104.99),
        gbfs_reappeared_at=None, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
    ) is False


def test_gbfs_end_rejects_when_no_waypoint_is_stored():
    assert ride_watch._gbfs_end_matches(
        None, gbfs_reappeared_at=_ENDED_AT, gbfs_end_lat=39.75, gbfs_end_lon=-104.99,
    ) is False
