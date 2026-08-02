"""Consensus rules for crowdsourced device features (src/device_features.py).

Pure logic — no database. What is defended here is the owner's three rules,
each of which has a failure mode that is invisible until a real fleet is
running against it:

  * first entry is authoritative (not "first entry is one vote");
  * any disagreement flags the vehicle, and flagging does NOT overwrite what
    we were publishing;
  * three reports resolve a review by 2/3, voted PER FIELD.

Plus the one invariant that makes the round-trip through `device_state`
lossless: `all_good_condition` is derived from `poor_condition`, never
stored independently. Without it a vehicle ping-pongs into needs_review
forever on reports that in fact agree — see normalise()'s rule 2.

The DB-touching half (process_pending's transitions) lives in
tests/test_device_features_pg.py, which needs a real Postgres.
"""

from __future__ import annotations

import pytest

from src.device_features import (
    FEATURE_KEYS,
    REVIEW_CONSENSUS_REPORTS,
    STATUS_NEEDS_CONFIRMED,
    STATUS_NEEDS_REVIEW,
    STATUS_UP_TO_DATE,
    FeatureAnswers,
    answers_agree,
    consensus,
)


def _a(bell=True, cup=True, phone=True, poor=()):
    """A normalised answer set. `all_good_condition` is deliberately not a
    parameter: it is derived, and letting a test set it independently would
    be testing a shape the code refuses to produce."""
    return FeatureAnswers(
        has_bell=bell, has_cup_holder=cup, has_phone_holder=phone,
        all_good_condition=not poor, poor_condition=tuple(poor),
    ).normalise()


# --- vocabulary --------------------------------------------------------------

def test_the_three_statuses_are_the_three_the_owner_named():
    assert STATUS_NEEDS_CONFIRMED == "needs_features_confirmed"
    assert STATUS_NEEDS_REVIEW == "needs_review"
    assert STATUS_UP_TO_DATE == "up_to_date"


def test_feature_keys_are_the_three_the_modal_asks_about():
    """These strings are the wire vocabulary for `poor_condition` — the
    client sends them back verbatim, so a rename is a breaking change and
    should have to walk past this test to happen."""
    assert FEATURE_KEYS == ("bell", "cup_holder", "phone_holder")


# --- normalise ---------------------------------------------------------------

def test_condition_claims_about_absent_features_are_dropped():
    a = FeatureAnswers(
        has_bell=False, has_cup_holder=True, has_phone_holder=True,
        all_good_condition=False, poor_condition=("bell", "cup_holder"),
    ).normalise()
    assert a.poor_condition == ("cup_holder",)


def test_all_good_condition_is_derived_not_believed():
    """The blanket flag never wins over the itemised list in either
    direction — it IS the list being empty. This is what makes a stored
    consensus (which is only ever the list) compare equal to the report that
    produced it."""
    claims_good = FeatureAnswers(
        has_bell=True, has_cup_holder=True, has_phone_holder=True,
        all_good_condition=True, poor_condition=("bell",),
    ).normalise()
    assert claims_good.all_good_condition is False

    claims_bad = FeatureAnswers(
        has_bell=True, has_cup_holder=True, has_phone_holder=True,
        all_good_condition=False, poor_condition=(),
    ).normalise()
    assert claims_bad.all_good_condition is True


def test_poor_condition_is_sorted_and_deduped_by_the_key_order():
    a = FeatureAnswers(
        has_bell=True, has_cup_holder=True, has_phone_holder=True,
        all_good_condition=False, poor_condition=("phone_holder", "bell"),
    ).normalise()
    assert a.poor_condition == ("bell", "phone_holder")


def test_normalise_is_idempotent():
    a = _a(bell=False, poor=("bell", "cup_holder"))
    assert a.normalise() == a


def test_a_report_round_trips_through_the_state_row_representation():
    """`device_state` stores only the poor-condition list, so this is the
    exact reconstruction src/device_features.py:_process_vehicle does. If
    it ever stops equalling the original, every reconfirmation of that
    vehicle reads as a disagreement."""
    original = _a(bell=True, cup=False, phone=True, poor=("bell",))
    rebuilt = FeatureAnswers(
        has_bell=original.has_bell,
        has_cup_holder=original.has_cup_holder,
        has_phone_holder=original.has_phone_holder,
        all_good_condition=not original.poor_condition,
        poor_condition=original.poor_condition,
    ).normalise()
    assert rebuilt == original


# --- agreement ---------------------------------------------------------------

def test_identical_answers_agree():
    assert answers_agree(_a(), _a())


def test_a_presence_difference_is_a_disagreement():
    assert not answers_agree(_a(cup=True), _a(cup=False))


def test_a_condition_difference_is_also_a_disagreement():
    """The owner's needs_review flow asks later reporters to confirm
    "features AND their condition", so a broken bell nobody else reported is
    as much a discrepancy as a bell nobody else saw."""
    assert not answers_agree(_a(), _a(poor=("bell",)))


def test_ordering_noise_in_poor_condition_is_not_a_disagreement():
    left = FeatureAnswers(True, True, True, False, ("phone_holder", "bell"))
    right = FeatureAnswers(True, True, True, False, ("bell", "phone_holder"))
    assert answers_agree(left, right)


def test_a_stale_condition_tick_for_a_now_absent_feature_is_not_a_disagreement():
    """The UI can leave a condition tick behind when the rider flips a
    presence toggle back to No. Normalising before comparing means that
    client-side lint does not cost a vehicle its up_to_date label."""
    tidy = _a(cup=False, poor=("bell",))
    messy = FeatureAnswers(True, False, True, False, ("bell", "cup_holder"))
    assert answers_agree(tidy, messy)


# --- consensus ---------------------------------------------------------------

def test_the_majority_threshold_is_two_of_three():
    assert REVIEW_CONSENSUS_REPORTS == 3


def test_unanimous_reports_produce_themselves():
    assert consensus([_a(), _a(), _a()]) == _a()


def test_two_of_three_wins_each_presence_field():
    votes = [_a(cup=True), _a(cup=False), _a(cup=True)]
    assert consensus(votes).has_cup_holder is True

    votes = [_a(cup=True), _a(cup=False), _a(cup=False)]
    assert consensus(votes).has_cup_holder is False


def test_fields_are_voted_independently_not_as_whole_answer_sets():
    """Three riders who each disagree on a DIFFERENT feature have no
    majority answer set at all, but they do have a 2/3 majority on every
    individual field. Whole-set voting would deadlock here; per-field voting
    is what makes the owner's "2/3 of what's correct" reachable."""
    votes = [
        _a(bell=False, cup=True, phone=True),
        _a(bell=True, cup=False, phone=True),
        _a(bell=True, cup=True, phone=False),
    ]
    won = consensus(votes)
    assert (won.has_bell, won.has_cup_holder, won.has_phone_holder) == (True, True, True)


def test_a_condition_complaint_only_two_riders_share_wins():
    votes = [_a(poor=("bell",)), _a(poor=("bell",)), _a()]
    won = consensus(votes)
    assert won.poor_condition == ("bell",)
    assert won.all_good_condition is False


def test_three_riders_itemising_three_different_faults_read_as_fine():
    """No single complaint reaches 2/3, so none survives — and the derived
    `all_good_condition` follows the surviving list rather than being voted
    on separately. That is the honest summary of a vote where nobody agreed
    on what was wrong; the alternative (a vehicle flagged as faulty with no
    fault named) is not actionable by anyone."""
    votes = [_a(poor=("bell",)), _a(poor=("cup_holder",)), _a(poor=("phone_holder",))]
    won = consensus(votes)
    assert won.poor_condition == ()
    assert won.all_good_condition is True


def test_condition_never_survives_its_features_absence_vote():
    """Two riders say there is no cup holder; two say the cup holder is
    broken. The presence vote wins, and a consensus describing the condition
    of a part the same consensus says isn't there can't be produced."""
    votes = [
        _a(cup=False),
        _a(cup=False),
        _a(cup=True, poor=("cup_holder",)),
    ]
    won = consensus(votes)
    assert won.has_cup_holder is False
    assert won.poor_condition == ()


def test_consensus_output_is_already_normalised():
    won = consensus([_a(poor=("bell",)), _a(poor=("bell",)), _a(poor=("bell",))])
    assert won.normalise() == won


def test_consensus_of_nothing_is_an_error_not_an_empty_answer():
    """An empty vote must never silently produce "no features" — that would
    publish "this scooter has nothing on it" as though someone had looked."""
    with pytest.raises(ValueError):
        consensus([])
