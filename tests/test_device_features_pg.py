"""Postgres-backed coverage for the device-feature state machine
(sql/055_device_features.sql + src/device_features.py:process_pending).

The transitions are the whole feature, and none of them exist against a
fake cursor — they are statements about rows read back after an UPDATE,
across multiple firings of the job. So this is the only place the owner's
three rules are actually verified end to end:

  * the first valid report is AUTHORITATIVE (up_to_date immediately);
  * a later report that disagrees flags the vehicle 'needs review' WITHOUT
    overwriting what we were publishing;
  * three reports inside the review window resolve it by 2/3, per field.

Plus the properties that only a real database can show: the column default
that labels every existing device 'needs features confirmed' with no
backfill, the CHECK that bounds the vocabulary, the idempotency of a
re-run, and that wrong-plate rows never vote but do leave the work queue.

SKIPS unless a reachable, migratable test database is provided via
VEO_TEST_PG_DSN (same contract as tests/test_ride_usuals_pg.py). NEVER
point that at production: the fixture executes every migration.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import device_features  # noqa: E402
from src.device_features import (  # noqa: E402
    STATUS_NEEDS_CONFIRMED,
    STATUS_NEEDS_REVIEW,
    STATUS_UP_TO_DATE,
    process_pending,
)

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

_BASE = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _apply_all(conn) -> None:
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — device features Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    _apply_all(conn)

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(device_features, "connection", _fake_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures-in-code
# ---------------------------------------------------------------------------

def _vehicle(pg_conn) -> str:
    """A device_state row, exactly as the ingest cycle would leave it —
    i.e. with NOTHING set about features."""
    vid = uuid.uuid4().hex[:16]
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO device_state (
                vehicle_identifier, vehicle_plate, current_lat, current_lon,
                first_observed_at_location, first_ever_observed_at,
                last_observed_at
            ) VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW())
            """,
            (vid, "1025543", 39.7392, -104.9876),
        )
    pg_conn.commit()
    return vid


def _report(
    pg_conn, vid, *, bell=True, cup=True, phone=True, basket=None, poor=(),
    plate_valid=True, minutes=0, status=STATUS_NEEDS_CONFIRMED,
):
    """`basket` defaults to None — an abstention, the shape of every report
    from a client older than sql/058 (see that migration on why the column is
    nullable where the other three are NOT NULL)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO device_feature_reports (
                vehicle_identifier, reported_at, submitted_plate, plate_valid,
                has_bell, has_cup_holder, has_phone_holder, has_basket,
                all_good_condition, poor_condition, status_at_report
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                vid, _BASE + timedelta(minutes=minutes), "1025543", plate_valid,
                bell, cup, phone, basket, not poor, list(poor), status,
            ),
        )
        (report_id,) = cur.fetchone()
    pg_conn.commit()
    return report_id


def _state(pg_conn, vid) -> dict:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT feature_status, has_bell, has_cup_holder, has_phone_holder,
                   features_poor_condition, features_confirmed_at,
                   features_review_since, features_report_count, has_basket
              FROM device_state WHERE vehicle_identifier = %s
            """,
            (vid,),
        )
        row = cur.fetchone()
    keys = ("status", "bell", "cup", "phone", "poor", "confirmed_at",
            "review_since", "count", "basket")
    return dict(zip(keys, row))


def _unprocessed(pg_conn, vid) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM device_feature_reports "
            "WHERE vehicle_identifier = %s AND processed_at IS NULL",
            (vid,),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------

def test_every_device_starts_needing_its_features_confirmed(pg_conn):
    """The owner's "all devices will at first be labeled 'Needs features
    confirmed'" is the column DEFAULT doing the work — there is no backfill
    pass, and there does not need to be one."""
    vid = _vehicle(pg_conn)
    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_CONFIRMED


def test_features_are_null_not_false_before_anyone_looks(pg_conn):
    """False would claim we know a scooter has no bell. NULL says nobody has
    looked, which is the truth and is what the payload's `device_features:
    null` reports."""
    s = _state(pg_conn, _vehicle(pg_conn))
    assert (s["bell"], s["cup"], s["phone"]) == (None, None, None)
    assert s["confirmed_at"] is None


def test_the_status_vocabulary_is_bounded(pg_conn):
    vid = _vehicle(pg_conn)
    with pg_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "UPDATE device_state SET feature_status = 'sort-of-confirmed' "
                "WHERE vehicle_identifier = %s",
                (vid,),
            )
    pg_conn.rollback()


def test_the_three_award_actions_are_legal_ledger_values(pg_conn):
    """sql/055 widens user_points_action_allowed. If it did not, every award
    this feature pays would fail its INSERT at runtime."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'user_points_action_allowed'"
        )
        definition = cur.fetchone()[0]
    for action in ("device_features_first", "device_features_review",
                   "device_features_reconfirm"):
        assert action in definition
    # And the widening did not narrow what was already there.
    for older in ("qr_scan", "battery_contribution", "ride_survey"):
        assert older in definition


# ---------------------------------------------------------------------------
# Rule 1 — the first entry is authoritative
# ---------------------------------------------------------------------------

def test_the_first_valid_report_becomes_the_answer(pg_conn):
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, bell=True, cup=False, phone=True)

    process_pending()

    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert (s["bell"], s["cup"], s["phone"]) == (True, False, True)
    assert s["poor"] == []
    assert s["confirmed_at"] is not None
    assert s["count"] == 1


def test_a_wrong_plate_report_never_votes_but_does_leave_the_queue(pg_conn):
    """Stored for audit, ignored by the consensus, and stamped processed so
    it is not re-scanned every ten minutes forever."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, bell=False, cup=False, phone=False, plate_valid=False)

    process_pending()

    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_CONFIRMED
    assert _unprocessed(pg_conn, vid) == 0


def test_processing_is_idempotent(pg_conn):
    """A firing that overlaps a slow predecessor, or a retry after a crash,
    must not re-count a report that already landed."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid)

    process_pending()
    first = _state(pg_conn, vid)
    process_pending()
    second = _state(pg_conn, vid)

    assert second["count"] == first["count"] == 1
    assert second["status"] == first["status"] == STATUS_UP_TO_DATE


# ---------------------------------------------------------------------------
# Rule 2 — a disagreement flags, and does not overwrite
# ---------------------------------------------------------------------------

def test_an_agreeing_second_report_just_counts(pg_conn):
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, minutes=0)
    process_pending()
    _report(pg_conn, vid, minutes=10, status=STATUS_UP_TO_DATE)
    process_pending()

    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert s["count"] == 2


def test_a_disagreeing_report_flags_the_vehicle_for_review(pg_conn):
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, cup=True, minutes=0)
    process_pending()
    _report(pg_conn, vid, cup=False, minutes=10, status=STATUS_UP_TO_DATE)
    process_pending()

    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_NEEDS_REVIEW
    assert s["review_since"] is not None


def test_flagging_for_review_does_not_overwrite_the_published_answer(pg_conn):
    """One dissenting voice does not get to rewrite the map. The label
    changes so more people are asked; the data stays until a vote replaces
    it."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, cup=True, minutes=0)
    process_pending()
    _report(pg_conn, vid, cup=False, minutes=10, status=STATUS_UP_TO_DATE)
    process_pending()

    assert _state(pg_conn, vid)["cup"] is True


def test_a_condition_change_alone_is_enough_to_flag(pg_conn):
    """The owner's review flow asks later reporters to confirm "features AND
    their condition" — so a bell that broke since the last report is a
    discrepancy worth a second look, not noise."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, minutes=0)
    process_pending()
    _report(pg_conn, vid, poor=("bell",), minutes=10, status=STATUS_UP_TO_DATE)
    process_pending()

    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_REVIEW


# ---------------------------------------------------------------------------
# Rule 3 — three reports resolve a review by 2/3
# ---------------------------------------------------------------------------

def _open_a_review(pg_conn):
    """First report says the cup holder is there; the second says it isn't.
    Leaves the vehicle in needs_review with one vote already cast."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, cup=True, minutes=0)
    process_pending()
    _report(pg_conn, vid, cup=False, minutes=10, status=STATUS_UP_TO_DATE)
    process_pending()
    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_REVIEW
    return vid


def test_a_review_does_not_resolve_on_two_reports(pg_conn):
    vid = _open_a_review(pg_conn)
    _report(pg_conn, vid, cup=False, minutes=20, status=STATUS_NEEDS_REVIEW)
    process_pending()
    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_REVIEW


def test_three_reports_resolve_the_review_by_majority(pg_conn):
    vid = _open_a_review(pg_conn)
    # Votes in the window: the disagreeing one (cup=False) plus these two.
    _report(pg_conn, vid, cup=False, minutes=20, status=STATUS_NEEDS_REVIEW)
    _report(pg_conn, vid, cup=False, minutes=30, status=STATUS_NEEDS_REVIEW)
    process_pending()

    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert s["cup"] is False, "the 3-0 majority replaced the original answer"
    assert s["review_since"] is None


def test_the_original_answer_can_win_its_own_review(pg_conn):
    """Two of the three votes in the window agree with what was published,
    so the review closes having changed nothing but the label — which is
    exactly the outcome that makes flagging cheap enough to do freely."""
    vid = _open_a_review(pg_conn)
    _report(pg_conn, vid, cup=True, minutes=20, status=STATUS_NEEDS_REVIEW)
    _report(pg_conn, vid, cup=True, minutes=30, status=STATUS_NEEDS_REVIEW)
    process_pending()

    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert s["cup"] is True


def test_the_vote_is_per_field_not_per_answer_set(pg_conn):
    """Three riders disagreeing about three different features have no
    majority answer SET, but a clear 2/3 on every field."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, bell=True, cup=True, phone=True, minutes=0)
    process_pending()
    _report(pg_conn, vid, bell=False, cup=True, phone=True, minutes=10,
            status=STATUS_UP_TO_DATE)
    _report(pg_conn, vid, bell=True, cup=False, phone=True, minutes=20,
            status=STATUS_NEEDS_REVIEW)
    _report(pg_conn, vid, bell=True, cup=True, phone=False, minutes=30,
            status=STATUS_NEEDS_REVIEW)
    process_pending()

    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert (s["bell"], s["cup"], s["phone"]) == (True, True, True)


def test_a_second_review_does_not_count_the_first_reviews_votes(pg_conn):
    """`features_review_since` bounds the window. Without it, a vehicle that
    has been through one review resolves its NEXT one instantly using stale
    ballots about a completely different question."""
    vid = _open_a_review(pg_conn)
    _report(pg_conn, vid, cup=False, minutes=20, status=STATUS_NEEDS_REVIEW)
    _report(pg_conn, vid, cup=False, minutes=30, status=STATUS_NEEDS_REVIEW)
    process_pending()
    assert _state(pg_conn, vid)["status"] == STATUS_UP_TO_DATE

    # A new disagreement, about the bell this time.
    _report(pg_conn, vid, cup=False, poor=("bell",), minutes=40,
            status=STATUS_UP_TO_DATE)
    process_pending()
    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_REVIEW, (
        "the second review must not resolve off the first review's ballots"
    )


def test_reports_arriving_in_one_batch_grade_the_same_as_across_firings(pg_conn):
    """The processor's cadence must not change its verdict — a ten-minute
    window that happens to catch three reports at once has to reach the same
    state as three separate firings would."""
    batched = _vehicle(pg_conn)
    _report(pg_conn, batched, cup=True, minutes=0)
    _report(pg_conn, batched, cup=False, minutes=10)
    _report(pg_conn, batched, cup=False, minutes=20)
    _report(pg_conn, batched, cup=False, minutes=30)
    process_pending()

    staggered = _vehicle(pg_conn)
    _report(pg_conn, staggered, cup=True, minutes=0)
    process_pending()
    _report(pg_conn, staggered, cup=False, minutes=10)
    process_pending()
    _report(pg_conn, staggered, cup=False, minutes=20)
    process_pending()
    _report(pg_conn, staggered, cup=False, minutes=30)
    process_pending()

    left, right = _state(pg_conn, batched), _state(pg_conn, staggered)
    assert left["status"] == right["status"] == STATUS_UP_TO_DATE
    assert left["cup"] == right["cup"] is False


def test_a_report_for_an_unknown_vehicle_is_retired_not_retried(pg_conn):
    """The device left the feed between the client reading the map and the
    job running. There is nothing to attach a consensus to, so the row stays
    in the log and leaves the queue."""
    orphan = uuid.uuid4().hex[:16]
    _report(pg_conn, orphan)
    process_pending()
    assert _unprocessed(pg_conn, orphan) == 0


# --- the basket, end to end (sql/058) ----------------------------------------

def test_a_first_report_publishes_its_basket_answer(pg_conn):
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=True)
    process_pending()
    state = _state(pg_conn, vid)
    assert state["status"] == STATUS_UP_TO_DATE
    assert state["basket"] is True


def test_a_pre_058_consensus_stays_unknown_rather_than_false(pg_conn):
    """Every vehicle confirmed before the question existed. NULL is "nobody
    has told us", which is a different fact from "no basket" — and the one
    the column is allowed to hold."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=None)
    process_pending()
    assert _state(pg_conn, vid)["basket"] is None


def test_a_later_report_fills_in_a_basket_nobody_had_answered(pg_conn):
    """The path that makes the migration worth anything for the existing
    fleet. The vehicle is up_to_date with a NULL basket; a current client
    agrees about the three features both were asked about, so this is a
    RECONFIRMATION — which normally does not rewrite the feature columns.
    `fill_abstentions` is why the basket lands anyway, instead of waiting for
    a review that may never come."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=None, minutes=0)
    process_pending()
    assert _state(pg_conn, vid)["basket"] is None

    _report(pg_conn, vid, basket=True, minutes=10)
    process_pending()
    state = _state(pg_conn, vid)
    assert state["basket"] is True
    assert state["status"] == STATUS_UP_TO_DATE, "still a reconfirmation"
    assert state["count"] == 2


def test_an_abstaining_report_does_not_flag_a_confirmed_basket(pg_conn):
    """The mirror image, and the rollout's real risk: an OLD client reports a
    vehicle whose consensus already includes a basket. Silence is not
    dissent, so the vehicle must not enter review."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=True, minutes=0)
    process_pending()
    _report(pg_conn, vid, basket=None, minutes=10)
    process_pending()
    state = _state(pg_conn, vid)
    assert state["status"] == STATUS_UP_TO_DATE
    assert state["basket"] is True


def test_two_riders_who_were_both_asked_can_still_open_a_review(pg_conn):
    """The abstention rule must not swallow a real basket discrepancy."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=True, minutes=0)
    process_pending()
    _report(pg_conn, vid, basket=False, minutes=10)
    process_pending()
    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_REVIEW


def test_a_review_resolves_the_basket_by_the_riders_who_were_asked(pg_conn):
    """Four reports, not three: the first is authoritative, the second opens
    the review AND casts the first vote in it, and the window needs
    REVIEW_CONSENSUS_REPORTS votes of its own before it resolves."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=True, minutes=0)
    process_pending()
    for minute in (10, 20, 30):
        _report(pg_conn, vid, basket=False, minutes=minute)
        process_pending()
    state = _state(pg_conn, vid)
    assert state["status"] == STATUS_UP_TO_DATE
    assert state["basket"] is False


def test_a_bent_basket_survives_the_round_trip(pg_conn):
    """The flow the API used to 422 outright: a rider standing at a Trike
    with a damaged cargo basket."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=True, poor=("basket",))
    process_pending()
    state = _state(pg_conn, vid)
    assert state["basket"] is True
    assert state["poor"] == ["basket"]


# ---------------------------------------------------------------------------
# sql/065 — ride-survey basket reports: abstaining on everything but the
# basket, folded by the same processor.
# ---------------------------------------------------------------------------

def _survey_report(pg_conn, vid, *, basket, poor=(), minutes=0,
                   status=STATUS_NEEDS_CONFIRMED):
    """The exact row src/api_ride_surveys.py files: basket only, no plate,
    plate_valid true (the ride is the proof of presence)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO device_feature_reports (
                vehicle_identifier, reported_at, submitted_plate, plate_valid,
                has_bell, has_cup_holder, has_phone_holder, has_basket,
                all_good_condition, poor_condition, status_at_report, source
            ) VALUES (%s, %s, NULL, TRUE, NULL, NULL, NULL, %s, %s, %s, %s,
                      'ride_survey')
            RETURNING id
            """,
            (vid, _BASE + timedelta(minutes=minutes), basket, not poor,
             list(poor), status),
        )
        (report_id,) = cur.fetchone()
    pg_conn.commit()
    return report_id


def test_a_survey_basket_answer_publishes_without_confirming_the_vehicle(pg_conn):
    """First thing ever known about the vehicle is its basket: the answer
    goes live, but three questions were never asked, so the map keeps
    soliciting a full confirmation."""
    vid = _vehicle(pg_conn)
    _survey_report(pg_conn, vid, basket=True)
    process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_NEEDS_CONFIRMED
    assert s["basket"] is True
    assert (s["bell"], s["cup"], s["phone"]) == (None, None, None)
    assert s["confirmed_at"] is not None
    assert s["count"] == 1


def test_a_survey_answer_matching_the_consensus_is_a_reconfirmation(pg_conn):
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, bell=True, cup=False, phone=True, basket=True)
    process_pending()
    _survey_report(pg_conn, vid, basket=True, minutes=10)
    process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert s["count"] == 2
    assert (s["bell"], s["cup"], s["phone"]) == (True, False, True)


def test_a_survey_answer_fills_a_basket_nobody_had_answered(pg_conn):
    """A vehicle confirmed by a pre-058 client learns its basket from a
    ride survey — same fill path a post-058 modal report takes."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=None)
    process_pending()
    _survey_report(pg_conn, vid, basket=True, minutes=10)
    process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert s["basket"] is True
    assert s["count"] == 2


def test_an_opposite_survey_answer_opens_a_review(pg_conn):
    """The one conflict a survey report CAN raise: the opposite basket."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=True)
    process_pending()
    _survey_report(pg_conn, vid, basket=False, minutes=10)
    process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_NEEDS_REVIEW
    # Flagging never overwrites the published answer.
    assert s["basket"] is True


def test_a_survey_report_cannot_conflict_over_features_it_never_answered(pg_conn):
    """A consensus with NO basket answer plus a survey that answers only
    the basket share no mutually-answered field that differs — whatever the
    bell/cup/phone answers are, there is nothing to disagree about."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, bell=False, cup=False, phone=False, basket=None)
    process_pending()
    _survey_report(pg_conn, vid, basket=True, minutes=10)
    process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert (s["bell"], s["cup"], s["phone"]) == (False, False, False)
    assert s["basket"] is True


def test_a_modal_report_confirms_a_survey_known_vehicle(pg_conn):
    """The inverse fill: stored knows only the basket; a full modal report
    agreeing on it contributes the other three AND confirms the vehicle."""
    vid = _vehicle(pg_conn)
    _survey_report(pg_conn, vid, basket=True)
    process_pending()
    _report(pg_conn, vid, bell=True, cup=True, phone=False, basket=True, minutes=10)
    process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert (s["bell"], s["cup"], s["phone"]) == (True, True, False)
    assert s["count"] == 2


def test_a_survey_only_review_keeps_what_the_vote_could_not_see(pg_conn):
    """Three basket-only votes settle the basket dispute without wiping the
    stored bell/cup/phone answers — a verdict about the basket is not a
    reason to forget everything else."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, bell=True, cup=False, phone=True, basket=True)
    process_pending()
    for minute in (10, 20, 30):
        _survey_report(pg_conn, vid, basket=False, minutes=minute)
        process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_UP_TO_DATE
    assert s["basket"] is False
    assert (s["bell"], s["cup"], s["phone"]) == (True, False, True)


def test_a_review_on_a_survey_only_vehicle_resolves_back_to_needs_confirmed(pg_conn):
    """Surveys disagreeing about a survey-known vehicle: the vote settles
    the basket, but nothing has ever answered the modal's questions, so the
    vehicle goes back to soliciting a confirmation rather than claiming
    up_to_date."""
    vid = _vehicle(pg_conn)
    _survey_report(pg_conn, vid, basket=True)
    process_pending()
    for minute in (10, 20, 30):
        _survey_report(pg_conn, vid, basket=False, minutes=minute)
        process_pending()
    s = _state(pg_conn, vid)
    assert s["status"] == STATUS_NEEDS_CONFIRMED
    assert s["basket"] is False
    assert (s["bell"], s["cup"], s["phone"]) == (None, None, None)


def test_a_poor_basket_survey_disputes_a_fine_one(pg_conn):
    """Condition is part of the answer, exactly as it is for modal reports."""
    vid = _vehicle(pg_conn)
    _report(pg_conn, vid, basket=True)
    process_pending()
    _survey_report(pg_conn, vid, basket=True, poor=("basket",), minutes=10)
    process_pending()
    assert _state(pg_conn, vid)["status"] == STATUS_NEEDS_REVIEW
