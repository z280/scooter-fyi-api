"""Crowdsourced device features: vocabulary, consensus rules, and the
ten-minute report processor (sql/055).

WHAT THIS IS FOR ----------------------------------------------------------
Veo's feed says nothing about what is bolted to a given scooter, so a Cosmo
with a cup holder and a Cosmo without one are indistinguishable on the map.
Riders standing next to one can see the difference in a second, so they tell
us, and the fleet becomes filterable on equipment for the first time.

THE STATE MACHINE ---------------------------------------------------------
`device_state.feature_status` is the published answer to "how much do we
trust what we know about this vehicle's features?", and it has exactly three
values:

    needs_features_confirmed   nobody has ever reported this vehicle.
                               Every device starts here (sql/055 makes it
                               the column default, so there is no backfill).
    needs_review               two reports disagreed. What we are showing
                               may be wrong, and we want more eyes.
    up_to_date                 we have an authoritative answer.

and exactly three transitions, all of them owned by `process_pending()`
below and none of them by the endpoint:

    needs_features_confirmed --(first valid report)-------> up_to_date
    up_to_date --------------(a later report disagrees)---> needs_review
    needs_review ------------(3 valid reports, 2/3 vote)--> up_to_date

FIRST REPORT IS AUTHORITATIVE. Not "first report is a vote" — the owner's
rule is that entry one simply becomes what we believe, and every later entry
is graded against it. That is deliberately optimistic, and `needs_review` is
the mechanism that makes it safe: the cost of a wrong first report is one
disagreement away from being corrected by a three-way vote.

WHY A CRON JOB AND NOT INLINE ---------------------------------------------
A rider's POST writes one row to `device_feature_reports` and returns. It
never reads other people's reports, never takes a lock on the vehicle, and
never runs the vote — so its latency is independent of how many other people
are reporting the same scooter, and a burst of reports on one popular device
cannot serialize behind a per-vehicle lock. The grading runs every ten
minutes on the 8s (crontab) over whatever accumulated.

That means a device's status lags its reports by up to ten minutes. This is
fine and is the reason the AWARD is decided at submit time from the status
the device carried THEN (`device_feature_reports.status_at_report`), not at
processing time: a rider who does the work of clearing a needs_review device
gets the needs_review award, even if two other people also reported it in
the same ten-minute window and the vote resolves before the job next runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .pg import connection

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

STATUS_NEEDS_CONFIRMED = "needs_features_confirmed"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_UP_TO_DATE = "up_to_date"

#: Every legal `device_state.feature_status`. Mirrors sql/055's CHECK.
FEATURE_STATUSES = (STATUS_NEEDS_CONFIRMED, STATUS_NEEDS_REVIEW, STATUS_UP_TO_DATE)

#: The three features a rider is asked about, in the order the modal asks.
#: These strings are the wire vocabulary for `poor_condition` — the client
#: sends them back verbatim, so renaming one is a breaking API change.
FEATURE_KEYS = ("bell", "cup_holder", "phone_holder")

#: `FEATURE_KEYS` -> the `device_feature_reports` / `device_state` column
#: holding "is it present?". One mapping so the report writer, the consensus
#: vote and the payload builder cannot drift on which column is which.
FEATURE_PRESENCE_COLUMNS = {
    "bell": "has_bell",
    "cup_holder": "has_cup_holder",
    "phone_holder": "has_phone_holder",
}

#: How many valid reports a `needs_review` vehicle needs before the vote
#: runs, and therefore what "2/3" means. The owner specified three; the
#: majority threshold below is derived from it rather than hardcoded as 2 so
#: the two can never disagree.
REVIEW_CONSENSUS_REPORTS = 3


def _majority_threshold() -> int:
    """Votes needed to win a field. 3 -> 2, 5 -> 3. Derived so raising
    REVIEW_CONSENSUS_REPORTS cannot leave a stale "2" behind."""
    return REVIEW_CONSENSUS_REPORTS // 2 + 1


# ---------------------------------------------------------------------------
# One report's answers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureAnswers:
    """The four toggles plus the condition follow-up, normalised.

    `poor_condition` is always sorted and always a subset of the features
    this same answer set reports PRESENT — `normalise()` enforces both, so
    equality comparison is meaningful and an answer claiming a broken cup
    holder on a scooter with no cup holder cannot exist downstream.
    """
    has_bell: bool
    has_cup_holder: bool
    has_phone_holder: bool
    all_good_condition: bool
    poor_condition: tuple[str, ...] = ()

    def present(self, key: str) -> bool:
        return bool(getattr(self, FEATURE_PRESENCE_COLUMNS[key]))

    def normalise(self) -> "FeatureAnswers":
        """Drop condition claims about absent features, sort what's left,
        and DERIVE `all_good_condition` from the result.

        Two rules, and the second one is load-bearing:

        1. A condition claim about a feature the same answer says isn't
           there is dropped. The UI only offers "which are not in good
           condition?" over the features the rider just confirmed present,
           so this only fires on a hand-rolled or stale payload.

        2. `all_good_condition` is not an independent field — it is exactly
           `poor_condition == ()`. It has to be, because `device_state`
           stores the consensus as the poor-condition list ALONE: if the two
           could disagree, an answer of "not all good, but I won't itemise
           it" would round-trip through the state row as "all good", the
           next identical report would compare unequal to it, and the
           vehicle would ping-pong into `needs_review` forever on reports
           that in fact agree perfectly. The endpoint enforces the same
           equivalence at the edge (422) so a client cannot silently have
           its blanket answer overridden here; this is the backstop that
           makes the round-trip lossless regardless.
        """
        cleaned = tuple(sorted(
            k for k in FEATURE_KEYS
            if k in set(self.poor_condition) and self.present(k)
        ))
        return FeatureAnswers(
            has_bell=bool(self.has_bell),
            has_cup_holder=bool(self.has_cup_holder),
            has_phone_holder=bool(self.has_phone_holder),
            all_good_condition=not cleaned,
            poor_condition=cleaned,
        )


def answers_agree(a: FeatureAnswers, b: FeatureAnswers) -> bool:
    """Do two reports say the same thing? Compared on all five fields —
    condition included, because the owner's needs_review flow explicitly
    asks later reporters to "confirm features AND their condition", so a
    disagreement about a broken bell is as much a discrepancy as one about
    whether the bell exists.

    Both sides are normalised first, so two answers that differ only in
    noise the UI can produce (a stale condition tick for a feature the rider
    then toggled to absent) agree rather than triggering a review.
    """
    return a.normalise() == b.normalise()


def consensus(reports: Sequence[FeatureAnswers]) -> FeatureAnswers:
    """Field-by-field majority over `reports`.

    Each of the three presence booleans and `all_good_condition` is voted
    independently, and `poor_condition` is voted per feature ("did a
    majority say THIS feature is present but not in good condition?"). With
    an odd `REVIEW_CONSENSUS_REPORTS` every boolean vote has a strict
    winner, so there is no tie-break rule to get wrong.

    Voting per FIELD rather than picking the most popular whole ANSWER SET
    is what makes 2/3 mean what the owner said it means: three riders who
    each agree on two of three features but disagree on the third produce a
    correct answer for the two they agree on, where whole-set voting would
    find no majority at all and be stuck.
    """
    if not reports:
        raise ValueError("consensus() needs at least one report")
    normalised = [r.normalise() for r in reports]
    need = _majority_threshold()

    def wins(pred) -> bool:
        return sum(1 for r in normalised if pred(r)) >= need

    presence = {
        key: wins(lambda r, k=key: r.present(k)) for key in FEATURE_KEYS
    }
    # A feature can only be voted "in poor condition" if it also won its
    # presence vote — otherwise the consensus would describe the condition
    # of something the same consensus says isn't there. normalise() below
    # would strip it anyway; doing it in the predicate keeps the two from
    # relying on each other.
    poor = tuple(sorted(
        key for key in FEATURE_KEYS
        if presence[key] and wins(lambda r, k=key: k in r.poor_condition)
    ))
    return FeatureAnswers(
        has_bell=presence["bell"],
        has_cup_holder=presence["cup_holder"],
        has_phone_holder=presence["phone_holder"],
        # Not voted on: `normalise()` derives it from the poor-condition
        # vote below (see rule 2 there). Passing the derived value in rather
        # than a vote result keeps the two from ever disagreeing — three
        # riders who each itemise a DIFFERENT broken feature produce no
        # majority for any single item, and this consensus correctly reads
        # "everything is fine", which is the honest summary of a vote where
        # nobody agreed on what was wrong.
        all_good_condition=not poor,
        poor_condition=poor,
    ).normalise()


def answers_from_row(row: Any, offset: int = 0) -> FeatureAnswers:
    """Build answers from a SELECT of
    (has_bell, has_cup_holder, has_phone_holder, all_good_condition,
     poor_condition) starting at `offset`."""
    return FeatureAnswers(
        has_bell=bool(row[offset]),
        has_cup_holder=bool(row[offset + 1]),
        has_phone_holder=bool(row[offset + 2]),
        all_good_condition=bool(row[offset + 3]),
        poor_condition=tuple(row[offset + 4] or ()),
    ).normalise()


# ---------------------------------------------------------------------------
# The processor (cron: every ten minutes, on the 8s)
# ---------------------------------------------------------------------------

_PENDING_SQL = """
    SELECT DISTINCT vehicle_identifier
      FROM device_feature_reports
     WHERE processed_at IS NULL
       AND plate_valid
     ORDER BY vehicle_identifier
"""

# Reports for one vehicle, oldest first. Order is load-bearing twice over:
# "the first entry is authoritative" and "the first three reports after a
# review opened decide the vote" are both statements about arrival order.
# `id` breaks ties on identical reported_at (two riders submitting in the
# same millisecond) so the ordering is total and the job is deterministic.
_VEHICLE_REPORTS_SQL = """
    SELECT id, reported_at, has_bell, has_cup_holder, has_phone_holder,
           all_good_condition, poor_condition
      FROM device_feature_reports
     WHERE vehicle_identifier = %s
       AND processed_at IS NULL
       AND plate_valid
     ORDER BY reported_at, id
"""


@dataclass
class ProcessStats:
    vehicles: int = 0
    reports: int = 0
    first_confirmations: int = 0
    reconfirmations: int = 0
    flagged_for_review: int = 0
    reviews_resolved: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "vehicles": self.vehicles,
            "reports": self.reports,
            "first_confirmations": self.first_confirmations,
            "reconfirmations": self.reconfirmations,
            "flagged_for_review": self.flagged_for_review,
            "reviews_resolved": self.reviews_resolved,
        }


def process_pending() -> dict[str, int]:
    """Fold every unprocessed VALID report into its vehicle's consensus.

    Idempotent and safe to re-run: a report is claimed by stamping
    `processed_at`, inside the same transaction that writes the state it
    produced, so a crash mid-job leaves the un-stamped remainder for the
    next firing rather than double-counting the stamped ones.

    One transaction per VEHICLE, not one for the whole job: a single
    vehicle's reports must be graded together (the vote needs all three at
    once), but two vehicles have nothing to do with each other, and a job-
    wide transaction would mean one bad row costs every vehicle its
    progress.

    Reports whose plate did not match are never selected here — they are
    stored for audit and nothing else — but they ARE stamped processed at
    the end, so they leave the work queue instead of being re-scanned
    forever.
    """
    stats = ProcessStats()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_PENDING_SQL)
            vehicles = [r[0] for r in cur.fetchall()]

        for vehicle in vehicles:
            try:
                with conn.cursor() as cur:
                    _process_vehicle(cur, vehicle, stats)
                conn.commit()
                stats.vehicles += 1
            except Exception:  # noqa: BLE001
                conn.rollback()
                # One vehicle's bad data must not stop the queue. The row
                # stays unprocessed and will be retried on the next firing;
                # if it is genuinely poisonous it will log every ten minutes
                # until someone looks, which is the intended noise level for
                # "a report we cannot grade".
                log.exception(
                    "device features: processing vehicle %s failed", vehicle
                )

        # Retire the rows nothing will ever grade (wrong plate). Separate
        # statement, separate commit: it is unrelated to any one vehicle's
        # consensus and must happen even if every vehicle above failed.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE device_feature_reports
                   SET processed_at = NOW()
                 WHERE processed_at IS NULL AND NOT plate_valid
                """
            )
            invalid = cur.rowcount or 0
        conn.commit()

    result = stats.as_dict()
    result["invalid_plate_reports_retired"] = invalid
    log.info("process_device_feature_reports: %r", result)
    return result


def _process_vehicle(cur, vehicle: str, stats: ProcessStats) -> None:
    """Grade one vehicle's backlog. Runs inside the caller's transaction."""
    # Lock the state row for the duration: the ingest cycle writes other
    # columns on this same row every two minutes, and two overlapping runs
    # of this job (a slow run still going when cron fires the next) must not
    # both read the same pre-vote state.
    cur.execute(
        """
        SELECT feature_status, has_bell, has_cup_holder, has_phone_holder,
               features_poor_condition, features_confirmed_at,
               features_review_since, features_report_count
          FROM device_state
         WHERE vehicle_identifier = %s
           FOR UPDATE
        """,
        (vehicle,),
    )
    state = cur.fetchone()

    cur.execute(_VEHICLE_REPORTS_SQL, (vehicle,))
    rows = cur.fetchall()
    if not rows:
        return
    stats.reports += len(rows)
    report_ids = [r[0] for r in rows]

    if state is None:
        # A report for a vehicle ingest has never seen. Can only happen if
        # the device left the feed between the client reading the map and
        # the job running. Nothing to attach the consensus to, so stamp the
        # reports processed (they stay in the log) and move on rather than
        # re-scanning them forever.
        log.warning(
            "device features: %d report(s) for unknown vehicle %s — retiring",
            len(rows), vehicle,
        )
        _stamp_processed(cur, report_ids)
        return

    status = state[0]
    poor = tuple(state[4] or ())
    confirmed_at = state[5]
    review_since = state[6]
    count = int(state[7] or 0)
    # device_state has no `all_good_condition` column: the consensus there is
    # expressed purely as "which features are in poor condition", and "all
    # good" is that list being empty. Reconstituting the flag from the list
    # is exactly what `normalise()` would do to it anyway, so a stored
    # consensus and a fresh report compare on equal terms.
    stored = FeatureAnswers(
        has_bell=bool(state[1]),
        has_cup_holder=bool(state[2]),
        has_phone_holder=bool(state[3]),
        all_good_condition=not poor,
        poor_condition=poor,
    ).normalise() if confirmed_at is not None else None

    for report_id, reported_at, *answer_cols in rows:
        answers = answers_from_row(answer_cols)

        if stored is None:
            # ---- First entry is authoritative.
            _write_consensus(cur, vehicle, answers, count=1)
            stored, status, count = answers, STATUS_UP_TO_DATE, 1
            confirmed_at, review_since = reported_at, None
            stats.first_confirmations += 1
            continue

        if status != STATUS_NEEDS_REVIEW:
            # ---- Grading against the authoritative answer.
            if answers_agree(answers, stored):
                count += 1
                cur.execute(
                    "UPDATE device_state SET features_report_count = %s "
                    "WHERE vehicle_identifier = %s",
                    (count, vehicle),
                )
                stats.reconfirmations += 1
            else:
                # Discrepancy. The vehicle's PUBLISHED features stay exactly
                # as they were — we do not overwrite an authoritative answer
                # with a single dissenting one. Only the label changes, and
                # `features_review_since` opens the window the vote below
                # counts from. This report is the first vote in it.
                review_since = reported_at
                status = STATUS_NEEDS_REVIEW
                cur.execute(
                    """
                    UPDATE device_state
                       SET feature_status = %s, features_review_since = %s
                     WHERE vehicle_identifier = %s
                    """,
                    (STATUS_NEEDS_REVIEW, reported_at, vehicle),
                )
                stats.flagged_for_review += 1
            continue

        # ---- In review: collect votes, resolve at REVIEW_CONSENSUS_REPORTS.
        # The window's votes are re-read from the table (rather than
        # accumulated in a list) because it spans firings: the report that
        # opened the review was stamped processed ten minutes ago.
        votes = _review_votes(cur, vehicle, review_since, upto_id=report_id)
        if len(votes) < REVIEW_CONSENSUS_REPORTS:
            continue
        winner = consensus(votes[:REVIEW_CONSENSUS_REPORTS])
        _write_consensus(cur, vehicle, winner, count=len(votes))
        stored, status, count = winner, STATUS_UP_TO_DATE, len(votes)
        confirmed_at, review_since = reported_at, None
        stats.reviews_resolved += 1

    _stamp_processed(cur, report_ids)


def _review_votes(
    cur, vehicle: str, review_since, *, upto_id: int
) -> list[FeatureAnswers]:
    """Valid reports for `vehicle` from `review_since` onward, oldest first,
    up to and including report `upto_id`.

    Bounded by `upto_id` so the vote sees exactly the reports that had
    arrived when this one did. Without the bound, a backlog of five reports
    would resolve the review using all five at the moment the third is
    graded — a different (and unreproducible) answer than the same five
    reports arriving across five separate firings.
    """
    cur.execute(
        """
        SELECT has_bell, has_cup_holder, has_phone_holder,
               all_good_condition, poor_condition
          FROM device_feature_reports
         WHERE vehicle_identifier = %s
           AND plate_valid
           AND reported_at >= %s
           AND id <= %s
         ORDER BY reported_at, id
        """,
        (vehicle, review_since, upto_id),
    )
    return [answers_from_row(r) for r in cur.fetchall()]


def _write_consensus(
    cur, vehicle: str, answers: FeatureAnswers, *, count: int
) -> None:
    """Publish `answers` as the vehicle's features and mark it up to date.

    This is the ONLY thing that writes the feature columns on device_state,
    so "what the map shows" has exactly one writer — the endpoint never
    touches them, and neither does ingest.
    """
    cur.execute(
        """
        UPDATE device_state
           SET has_bell = %s,
               has_cup_holder = %s,
               has_phone_holder = %s,
               features_poor_condition = %s,
               feature_status = %s,
               features_confirmed_at = NOW(),
               features_review_since = NULL,
               features_report_count = %s
         WHERE vehicle_identifier = %s
        """,
        (
            answers.has_bell,
            answers.has_cup_holder,
            answers.has_phone_holder,
            list(answers.poor_condition),
            STATUS_UP_TO_DATE,
            count,
            vehicle,
        ),
    )


def _stamp_processed(cur, report_ids: Iterable[int]) -> None:
    ids = list(report_ids)
    if not ids:
        return
    cur.execute(
        "UPDATE device_feature_reports SET processed_at = NOW() "
        "WHERE id = ANY(%s)",
        (ids,),
    )
