"""Dibs, watched: tell the claimant when their scooter goes out on rental.

Called once per ingest cycle from src/cycle.py:run_once(), right after
ride_watch, under the same isolation contract — a failure here must never
fail the cycle, and the caller wraps it in try/except.

WHAT WAS MISSING. The app has carried a "notify me if somebody takes the
scooter I called dibs on" switch with nothing behind it. The claim was
registered (sql/076_dibs.sql), the fleet was observed every cycle, and the
two never met.

WHAT WE CAN SEE, which is less than the switch's wording implies.
src/ride_watch.py measured this against production: a rented Veo stays in
free_bike_status with `is_reserved` true for the whole rental, and other
operators drop a rented vehicle from the feed instead. Both readings are
honoured here, through ride_watch's own `is_checked_out`, because the
question is identical and two answers to it would differ exactly when it
mattered.

What that gives us is "a rental started on this vehicle". It does NOT give
us who started it.

SO THE MESSAGE SAYS WHAT WE OBSERVED. We cannot tell the claimant's own
rental from a stranger's. The app tells us when it can — `POST
/api/v1/dibs/{id}/mine` stamps `mine_at` and suppresses the alert — but a
rider who walks up and unlocks through Veo's own app never passes through
our code, and no amount of wanting to say "somebody took your scooter"
makes that observable. The copy below therefore reports the rental and
leaves the conclusion to the one person who knows. That is also the more
useful message: "if that was you, nothing to do" costs a sentence, and a
confident accusation that turns out to be the rider's own ride is the kind
of wrong that stops people trusting any alert we send.

ONCE PER CLAIM. The cycle runs every couple of minutes and a rental spans
many of them, so `notified_at` is the guard. A transient send failure (quota,
comms down) deliberately records neither `notified_at` nor `notify_skipped`,
so the next cycle tries again while the claim is still live; an unverified
number, an opt-out and a number comms cannot use are permanent for this claim
and are recorded as such, because "the switch was on and nothing arrived" is
otherwise unanswerable.

NOTHING IS LOCKED WHILE THE GATEWAY IS CALLED, and this is a correctness
requirement rather than a performance one. The first version selected every
candidate `FOR UPDATE` and sent before committing. A `/mine` arriving mid-send
would then block on the locked row until the watcher committed: the alert went
out, and the rider's "I've got it" landed just after it — the exact own-rental
false alert this module exists to prevent. A slow gateway also held locks on
every live claim at once.

So each claim moves through three short transactions:

  1. READ + OBSERVE. Select the candidates, classify them against this cycle's
     feed, stamp `taken_at`. Commit. No row stays locked.
  2. RESERVE. One UPDATE per claim that re-tests every gate — `mine_at` among
     them — and stamps `notify_attempt_at`. It returns nothing if the rider
     said "mine" in the meantime, which is the `mine_at` re-check the send
     path needs, done atomically rather than as a read that could go stale
     between the looking and the sending. Commit.
  3. SEND, with no transaction open, then RECORD the outcome in a third.

What remains is a genuine race of the send's own duration: a `/mine` landing
after the reservation gets a text anyway. That is irreducible without locking
across the gateway, which is the thing being removed — and it now fails in the
harmless direction, because `/mine` returns immediately instead of waiting.

A VERIFIED PHONE IS REQUIRED, and `accounts.phone_verified_at` is the only
thing that counts. A number typed into a profile is a number nobody has
proved they answer (sql/045's own words), and texting it would mean texting
a stranger about a scooter because a rider mistyped a digit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from .comms import (
    CommsError,
    OptedOut,
    QuotaExceeded,
    UnusableRecipient,
    send_sms,
)
from .ingest import TaggedDevice
from .pg import connection
from .ride_watch import is_checked_out

log = logging.getLogger(__name__)

#: Mirrors api_dibs.API_BASE. Duplicated rather than imported: this module
#: runs in the ingest worker and importing the router would drag the whole
#: FastAPI app in behind it. `test_dibs_watch.py` asserts the two agree.
API_BASE = "https://data.scooter.fyi"

#: How long a reservation holds a claim before another cycle may retry it.
#: Generously longer than a send, short enough that a process that died
#: mid-send does not strand the claim for its whole 25-minute life. The retry
#: cannot double-text: `send_sms` carries an idempotency key naming the claim,
#: so comms collapses the second attempt.
DIBS_NOTIFY_LEASE = timedelta(minutes=5)

#: Why no text went out, when none did. Transient failures are NOT in here —
#: they leave the claim untouched so the next cycle retries it.
SKIP_NO_PHONE = "no_phone"
SKIP_UNVERIFIED = "unverified"
SKIP_OPTED_OUT = "opted_out"
SKIP_UNUSABLE = "unusable"


@dataclass(frozen=True)
class WatchedClaim:
    """One live claim that asked to be watched."""

    dibs_id: str
    vehicle_identifier: str
    vehicle_name: str
    claimed_at: datetime
    account_id: int


@dataclass
class DibsWatchStats:
    open_claims: int = 0
    newly_taken: int = 0
    texted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    deferred: int = 0
    #: Judged taken, but the reservation lost — the rider said "mine", or
    #: another cycle already has it. Counted apart from `skipped` because
    #: nothing was decided and nothing was recorded.
    unreserved: int = 0


def taken_claims(
    claims: Iterable[WatchedClaim],
    observed: dict[str, TaggedDevice],
) -> list[WatchedClaim]:
    """Which watched claims' vehicles are out on rental this cycle.

    Pure partitioning, no DB and no IO, for the same reason
    ride_watch._classify is: the interesting cases are "absent from the
    feed", "present and reserved", "present and free" and "present with the
    flag missing", and none of them needs a database to express.
    """
    out: list[WatchedClaim] = []
    for claim in claims:
        if is_checked_out(observed.get(claim.vehicle_identifier)):
            out.append(claim)
    return out


def alert_text(claim: WatchedClaim, now: datetime) -> str:
    """What the rider reads.

    Three jobs, in order: name the scooter so they know which claim this is
    about, say what we actually saw, and give them the one thing they can do
    with it — the certificate, which is the artifact for the argument.

    It does NOT say somebody took it. See the module docstring: we cannot see
    that, and a confident accusation that turns out to be the rider's own
    ride is how an alert channel loses its credibility.
    """
    minutes = max(0, int((now - claim.claimed_at).total_seconds() // 60))
    ago = "just now" if minutes < 1 else f"{minutes} min ago"
    return (
        f"{claim.vehicle_name} — the scooter you called dibs on {ago} — "
        "has just gone out on rental. If that is you riding it, nothing to "
        "do. If it is not, your dibs were not respected, and your "
        f"certificate is at {API_BASE}/dibs/{claim.dibs_id}"
    )


def _candidates(snapshot_time: datetime) -> list[WatchedClaim]:
    """Transaction 1a: the claims this cycle might have to text about.

    NO `FOR UPDATE`. Nothing is decided off this read — the reservation below
    re-tests every gate atomically — and a lock taken here would still be held
    when the gateway is called. `notify_attempt_at` keeps an overlapping cycle
    from picking up a claim whose send is already in flight.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, vehicle_identifier, vehicle_name, claimed_at, account_id
                FROM dibs
                WHERE notify_sms
                  AND account_id IS NOT NULL
                  AND notified_at IS NULL
                  AND notify_skipped IS NULL
                  AND mine_at IS NULL
                  AND expires_at > %s
                  AND (notify_attempt_at IS NULL OR notify_attempt_at < %s)
                """,
                (snapshot_time, snapshot_time - DIBS_NOTIFY_LEASE),
            )
            rows = cur.fetchall()
        conn.commit()
    return [
        WatchedClaim(
            dibs_id=r[0],
            vehicle_identifier=r[1],
            vehicle_name=r[2],
            claimed_at=r[3],
            account_id=r[4],
        )
        for r in rows
    ]


def _mark_taken(snapshot_time: datetime, claims: list[WatchedClaim]) -> None:
    """Transaction 1b: record the observation, before any sending.

    "Were dibs disrespected" is a different question from "did we manage to
    tell anybody", and a send that fails must not erase the answer to the
    first. `COALESCE` so a second cycle over the same rental does not move the
    moment it was first seen.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dibs SET taken_at = COALESCE(taken_at, %s) WHERE id = ANY(%s)",
                (snapshot_time, [c.dibs_id for c in claims]),
            )
        conn.commit()


def _reserve(claim: WatchedClaim, snapshot_time: datetime) -> tuple[bool, str | None, bool]:
    """Transaction 2: claim this send, and re-read the gates while doing it.

    Returns `(reserved, phone, verified)`. `reserved` false means somebody got
    there first — the rider said "mine", another cycle took it, the claim
    expired, or an outcome was already recorded — and NOTHING should be sent.

    ONE STATEMENT, so the test and the stamp cannot be separated by anything.
    A read-then-write would reopen the window this whole restructure closes:
    `/mine` could land between the two and the send would go out regardless.
    The row lock this takes lasts as long as one UPDATE, not as long as an
    SMS.

    The phone comes back from the same statement rather than a second read,
    so the gate is decided on one snapshot of the claim and its account.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dibs AS d SET notify_attempt_at = %s
                WHERE d.id = %s
                  AND d.notified_at IS NULL
                  AND d.notify_skipped IS NULL
                  AND d.mine_at IS NULL
                  AND d.expires_at > %s
                  AND (d.notify_attempt_at IS NULL OR d.notify_attempt_at < %s)
                RETURNING
                  (SELECT a.phone_number FROM accounts a WHERE a.id = d.account_id),
                  (SELECT a.phone_verified_at IS NOT NULL
                     FROM accounts a WHERE a.id = d.account_id)
                """,
                (
                    snapshot_time,
                    claim.dibs_id,
                    snapshot_time,
                    snapshot_time - DIBS_NOTIFY_LEASE,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    if row is None:
        return False, None, False
    phone, verified = row
    return True, (phone or None), bool(verified)


def _record(dibs_id: str, **fields: object) -> None:
    """Transaction 3: what became of the send.

    `notify_attempt_at = NULL` releases the reservation, which is what a
    transient failure wants: the next cycle picks the claim straight back up
    instead of waiting out the lease.
    """
    sets = ", ".join(f"{k} = %s" for k in fields)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE dibs SET {sets} WHERE id = %s",
                (*fields.values(), dibs_id),
            )
        conn.commit()


def watch_claims_for_cycle(
    snapshot_time: datetime,
    devices: Iterable[TaggedDevice],
) -> DibsWatchStats:
    """One cycle's pass over the watched claims."""
    observed = {d.vehicle_identifier: d for d in devices if d.vehicle_identifier}
    stats = DibsWatchStats()

    claims = _candidates(snapshot_time)
    stats.open_claims = len(claims)
    if not claims:
        return stats

    taken = taken_claims(claims, observed)
    stats.newly_taken = len(taken)
    if not taken:
        return stats

    _mark_taken(snapshot_time, taken)

    for claim in taken:
        reserved, phone, verified = _reserve(claim, snapshot_time)
        if not reserved:
            # The rider said "mine" (or another cycle has it). Not a skip: no
            # decision was made about this claim and none should be recorded.
            stats.unreserved += 1
            continue
        if not phone:
            _skip(claim, SKIP_NO_PHONE, stats)
            continue
        if not verified:
            _skip(claim, SKIP_UNVERIFIED, stats)
            continue
        try:
            # NO TRANSACTION IS OPEN HERE. That is the point of the whole
            # three-step shape above.
            send_sms(
                phone,
                alert_text(claim, snapshot_time),
                # The CLAIM, not the attempt. A retry after a dropped
                # connection — or after a reservation whose process died —
                # must not text twice, and a fresh UUID per call would defend
                # against nothing.
                idempotency_key=f"dibs-taken:{claim.dibs_id}",
                metadata={"kind": "dibs_taken", "dibs_id": claim.dibs_id},
            )
        except OptedOut:
            _skip(claim, SKIP_OPTED_OUT, stats)
            continue
        except UnusableRecipient:
            _skip(claim, SKIP_UNUSABLE, stats)
            continue
        except (QuotaExceeded, CommsError) as e:
            # TRANSIENT, so no outcome is recorded and the reservation is
            # released for the next cycle. Recording a skip here would turn a
            # thirty-second comms outage into a permanently unanswered claim.
            log.warning("dibs alert deferred for %s: %s", claim.dibs_id, e)
            _record(claim.dibs_id, notify_attempt_at=None)
            stats.deferred += 1
            continue
        _record(claim.dibs_id, notified_at=snapshot_time)
        stats.texted += 1
    return stats


def _skip(claim: WatchedClaim, reason: str, stats: DibsWatchStats) -> None:
    """A permanent outcome for this claim, and the reservation goes with it."""
    _record(claim.dibs_id, notify_skipped=reason, notify_attempt_at=None)
    stats.skipped[reason] = stats.skipped.get(reason, 0) + 1
