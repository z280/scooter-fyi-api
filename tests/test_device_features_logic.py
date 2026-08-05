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
    canonical_poor,
    consensus,
    fill_abstentions,
)


def _a(bell=True, cup=True, phone=True, poor=(), basket=None):
    """A normalised answer set. `all_good_condition` is deliberately not a
    parameter: it is derived, and letting a test set it independently would
    be testing a shape the code refuses to produce.

    `basket` defaults to None — an ABSTENTION, which is the shape of every
    report from a client older than sql/058. That default is deliberate: the
    tests that predate the basket go on describing exactly the scooters they
    always described, and the abstention path gets exercised by all of them
    rather than only by the few that name it.
    """
    return FeatureAnswers(
        has_bell=bell, has_cup_holder=cup, has_phone_holder=phone,
        has_basket=basket,
        all_good_condition=not poor, poor_condition=tuple(poor),
    ).normalise()


# --- vocabulary --------------------------------------------------------------

def test_the_three_statuses_are_the_three_the_owner_named():
    assert STATUS_NEEDS_CONFIRMED == "needs_features_confirmed"
    assert STATUS_NEEDS_REVIEW == "needs_review"
    assert STATUS_UP_TO_DATE == "up_to_date"


def test_feature_keys_are_the_ones_the_modal_asks_about():
    """These strings are the wire vocabulary for `poor_condition` — the
    client sends them back verbatim, so a rename is a breaking change and
    should have to walk past this test to happen.

    The basket (sql/058) is asked of EVERY device, not only the models that
    ship with one: the Trike carries a cargo basket as standard equipment,
    so a model gate would have made a bent Trike basket unreportable."""
    assert FEATURE_KEYS == ("bell", "cup_holder", "phone_holder", "basket")


def test_poor_condition_is_ordered_by_vocabulary_not_alphabet():
    """The ordering the old code got away with by coincidence.

    Until `basket` landed, FEATURE_KEYS order and `sorted()` agreed, because
    the first three keys happen to be alphabetical. `basket` breaks that,
    and the endpoint (which canonicalises what it stores) and the processor
    (which canonicalises what it compares) MUST still produce the same array
    for the same answer — the dedupe probe compares stored arrays literally.
    """
    assert canonical_poor(["basket", "bell"]) == ("bell", "basket")
    assert canonical_poor(["basket", "bell"]) != tuple(sorted(["basket", "bell"]))


def test_canonical_poor_dedupes_and_drops_the_unknown():
    assert canonical_poor(["bell", "bell", "rear_rack"]) == ("bell",)


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


# --- abstentions (sql/058) ---------------------------------------------------
#
# `has_basket` is the first feature a report is allowed to have no opinion
# about, because the question is newer than the clients: the frontend already
# deployed asks three questions and knows nothing about a fourth. NULL means
# "this rider was never asked", which is neither yes nor no, and the whole
# point of the machinery below is that it never gets rounded to either.
#
# This is a ROLLOUT AFFORDANCE. Once no client omits the field, every report
# answers every feature and each of these paths becomes unreachable.

def test_an_abstention_is_not_a_no():
    asked = _a(basket=False)
    never_asked = _a(basket=None)
    assert asked != never_asked
    assert asked.answered("basket") and not never_asked.answered("basket")


def test_normalise_keeps_an_abstention_rather_than_defaulting_it():
    """Rounding None to False here is exactly the lie the abstention exists
    to avoid — it would publish "no basket" on the word of a rider who was
    never shown the question."""
    assert _a(basket=None).has_basket is None
    assert _a(basket=False).has_basket is False


def test_a_condition_claim_about_an_abstained_feature_is_dropped():
    """A client that never asked about baskets cannot coherently report a
    broken one."""
    a = FeatureAnswers(
        has_bell=True, has_cup_holder=True, has_phone_holder=True,
        has_basket=None, all_good_condition=False,
        poor_condition=("basket", "bell"),
    ).normalise()
    assert a.poor_condition == ("bell",)


def test_an_abstention_is_not_a_disagreement():
    """The rollout's whole risk in one test: an old client and a current one
    report the same scooter and differ only in that one of them was never
    asked about the basket. Reading that as a discrepancy would flip healthy
    vehicles into needs_review for the length of the rollout, burning three
    riders' work to settle a dispute nobody had."""
    assert answers_agree(_a(basket=None), _a(basket=True))
    assert answers_agree(_a(basket=True), _a(basket=None))


def test_two_riders_who_were_both_asked_can_still_disagree_about_a_basket():
    """The abstention rule must not swallow a real discrepancy."""
    assert not answers_agree(_a(basket=True), _a(basket=False))


def test_a_basket_fault_only_one_of_them_could_report_is_not_a_disagreement():
    """An abstained feature drops out of the condition comparison too — the
    old client had no way to itemise a basket it never asked about."""
    assert answers_agree(
        _a(basket=True, poor=("basket",)),
        _a(basket=None),
    )


def test_abstentions_do_not_vote_and_do_not_lower_the_bar():
    """One rider who WAS asked carries the field over two who were not.

    Counting silences as "no" would mean a lone current client could never
    establish a basket during the rollout — it would lose 2-1 to two clients
    that had no opinion to give."""
    won = consensus([_a(basket=True), _a(basket=None), _a(basket=None)])
    assert won.has_basket is True


def test_a_feature_nobody_was_asked_about_stays_unknown():
    """Not False. A review that resolves on three pre-058 reports must not
    publish a confident "no basket" nobody ever looked for."""
    won = consensus([_a(), _a(), _a()])
    assert won.has_basket is None


def test_a_basket_vote_is_decided_by_the_riders_who_were_asked():
    two_of_three_asked = consensus([_a(basket=True), _a(basket=False), _a()])
    # One-all among the two who answered: no majority, so the field reads
    # absent — the same convention every other presence field follows when
    # nobody reaches the threshold.
    assert two_of_three_asked.has_basket is False

    assert consensus(
        [_a(basket=True), _a(basket=True), _a(basket=False)]
    ).has_basket is True


# --- filling in a pre-058 consensus ------------------------------------------

def test_fill_abstentions_publishes_the_first_answer_for_an_unasked_feature():
    """How a vehicle confirmed before sql/058 ever learns about its basket.

    Such a vehicle is up_to_date with has_basket NULL, and a later report
    that answers the basket AGREES with it, so it takes the reconfirmation
    path — which by design does not rewrite the feature columns. Without
    this, the basket would stay unknown for most of the fleet forever."""
    stored = _a(basket=None)
    filled = fill_abstentions(stored, _a(basket=True))
    assert filled.has_basket is True
    # Nothing else moved.
    assert (filled.has_bell, filled.has_cup_holder, filled.has_phone_holder) == (
        stored.has_bell, stored.has_cup_holder, stored.has_phone_holder
    )


def test_fill_abstentions_carries_the_new_features_condition_too():
    filled = fill_abstentions(_a(basket=None), _a(basket=True, poor=("basket",)))
    assert filled.poor_condition == ("basket",)
    assert filled.all_good_condition is False


def test_fill_abstentions_never_overwrites_an_answer_we_already_had():
    """A feature the stored consensus has an opinion about is left alone. A
    report that contradicts that opinion opens a review; it never reaches
    here to quietly win."""
    stored = _a(basket=False)
    assert fill_abstentions(stored, _a(basket=True)) == stored


def test_fill_abstentions_is_a_no_op_when_there_is_nothing_to_fill():
    stored = _a(basket=True)
    assert fill_abstentions(stored, _a(basket=True)) is stored
    assert fill_abstentions(stored, _a(basket=None)) is stored
