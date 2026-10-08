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
many of them, so `notified_at` is the guard and it is written in the same
transaction as the decision. A transient send failure (quota, comms down)
deliberately stamps neither `notified_at` nor `notify_skipped`, so the next
cycle tries again while the claim is still live; an unverified number, an
opt-out and a number comms cannot use are permanent for this claim and are
recorded as such, because "the switch was on and nothing arrived" is
otherwise unanswerable.

A VERIFIED PHONE IS REQUIRED, and `accounts.phone_verified_at` is the only
thing that counts. A number typed into a profile is a number nobody has
proved they answer (sql/045's own words), and texting it would mean texting
a stranger about a scooter because a rider mistyped a digit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
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


def _claimant_phone(cur, account_id: int) -> tuple[str | None, bool]:
    """The account's number and whether anybody has proved they answer it."""
    cur.execute(
        "SELECT phone_number, phone_verified_at IS NOT NULL "
        "FROM accounts WHERE id = %s",
        (account_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None, False
    phone, verified = row
    return (phone or None), bool(verified)


def watch_claims_for_cycle(
    snapshot_time: datetime,
    devices: Iterable[TaggedDevice],
) -> DibsWatchStats:
    """One cycle's pass over the watched claims."""
    observed = {d.vehicle_identifier: d for d in devices if d.vehicle_identifier}
    stats = DibsWatchStats()

    with connection() as conn:
        with conn.cursor() as cur:
            # The partial index in sql/097 covers every term but the expiry,
            # which cannot live in an index predicate. `FOR UPDATE` so two
            # overlapping cycles cannot both decide to text the same claim.
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
                FOR UPDATE
                """,
                (snapshot_time,),
            )
            claims = [
                WatchedClaim(
                    dibs_id=r[0],
                    vehicle_identifier=r[1],
                    vehicle_name=r[2],
                    claimed_at=r[3],
                    account_id=r[4],
                )
                for r in cur.fetchall()
            ]
            stats.open_claims = len(claims)
            if not claims:
                return stats

            taken = taken_claims(claims, observed)
            stats.newly_taken = len(taken)
            if not taken:
                return stats

            # Stamped for every claim we judged taken, before any sending —
            # "did dibs get disrespected" is a different question from "did we
            # manage to tell anybody", and a send that fails must not erase
            # the observation.
            cur.execute(
                "UPDATE dibs SET taken_at = COALESCE(taken_at, %s) WHERE id = ANY(%s)",
                (snapshot_time, [c.dibs_id for c in taken]),
            )

            for claim in taken:
                phone, verified = _claimant_phone(cur, claim.account_id)
                if not phone:
                    _skip(cur, claim, SKIP_NO_PHONE, stats)
                    continue
                if not verified:
                    _skip(cur, claim, SKIP_UNVERIFIED, stats)
                    continue
                try:
                    send_sms(
                        phone,
                        alert_text(claim, snapshot_time),
                        # The CLAIM, not the attempt. A retry after a dropped
                        # connection must not text twice, and a fresh UUID per
                        # call would defend against nothing.
                        idempotency_key=f"dibs-taken:{claim.dibs_id}",
                        metadata={"kind": "dibs_taken", "dibs_id": claim.dibs_id},
                    )
                except OptedOut:
                    _skip(cur, claim, SKIP_OPTED_OUT, stats)
                    continue
                except UnusableRecipient:
                    _skip(cur, claim, SKIP_UNUSABLE, stats)
                    continue
                except (QuotaExceeded, CommsError) as e:
                    # TRANSIENT, so nothing is recorded and the next cycle
                    # tries again while the claim is still live. Recording a
                    # skip here would turn a thirty-second comms outage into a
                    # permanently unanswered claim.
                    log.warning(
                        "dibs alert deferred for %s: %s", claim.dibs_id, e
                    )
                    stats.deferred += 1
                    continue
                cur.execute(
                    "UPDATE dibs SET notified_at = %s WHERE id = %s",
                    (snapshot_time, claim.dibs_id),
                )
                stats.texted += 1
        conn.commit()
    return stats


def _skip(cur, claim: WatchedClaim, reason: str, stats: DibsWatchStats) -> None:
    cur.execute(
        "UPDATE dibs SET notify_skipped = %s WHERE id = %s",
        (reason, claim.dibs_id),
    )
    stats.skipped[reason] = stats.skipped.get(reason, 0) + 1
