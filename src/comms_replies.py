"""Collect inbound SMS replies from z280-comms and act on the consent ones.

Run from cron (`python -m src.cli poll_comms_replies`). Replies are polled,
not pushed, and **polling claims what it returns**: a reply handed to this
process is never handed to anyone — including a later run of this same
process — again. Two consequences shape everything below.

1. **Write first, interpret second.** The row goes into comms_replies
   before we decide what it means, because if this process dies between
   collecting and understanding, that row is the only remaining evidence
   the message ever existed.

2. **An un-acked row is a to-do item, not a retry.** Acking does not
   control redelivery (there is none); it records that we finished. So a
   row with handled_at IS NULL and a collection timestamp from an hour ago
   means a human needs to look — the message reached us and we dropped it.

Why we bother reading replies at all, when comms enforces consent for us:
being blocked is not a substitute for knowing. A rider who texts STOP
still has an account here with a phone number on it, and continuing to
show them "we'll text you a code" — or, worse, treating their number as a
working sign-in door — is the application being wrong about a person who
told us plainly. The 409 at send time protects the recipient; this
protects the honesty of what we display.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .comms import ack_reply, comms_credentials, poll_replies
from .pg import connection

log = logging.getLogger(__name__)

def classify(body: str | None) -> str:
    """'stop' | 'unstop' | 'other' for one reply body.

    This is a MIRROR of z280-comms' own rule, not an independent judgement,
    and that distinction is the whole point. Comms owns consent for every
    application on the shared sender; this function exists only so our UI
    can be honest in between polls. If the two disagree, every disagreement
    is a bug, in one of two directions:

      * **Wider here than comms** — we mark someone opted out, comms keeps
        accepting sends, and the OTHER applications on that number keep
        texting a rider who used a carrier-standard keyword. That is the
        complaint that gets a shared number filtered, and we'd have caused
        it while believing we'd handled it.
      * **Narrower here than comms** — comms blocks, we don't know, and our
        UI goes on offering a sign-in door that answers 409 forever. That
        is exactly the dishonesty this module's docstring claims to prevent.

    So being wrong in the same direction as the authority is the only safe
    way to be wrong, and "improving" on comms locally is the one change
    that must not be made here.

    As of this PR comms blocks on a STOP **prefix** and clears on exactly
    UNSTOP — narrower than the carrier-standard set in both directions.
    (An earlier draft of this file matched a wider set on the whole
    message, which silently produced nine divergences.) The table in
    tests/test_comms_replies.py is the tripwire; when comms widens its
    set, that test fails first and this follows.

    Note this classifier is never load-bearing for a send: consent is
    enforced upstream, and a 409 at send time records the opt-out
    regardless of what this function thought (api_auth._note_opt_out).
    """
    if not body:
        return "other"
    text = body.strip().lower()
    # Checked first, though it cannot collide: "unstop" does not start with
    # "stop", and stating the precedence keeps it that way if either rule
    # is ever edited.
    if text == "unstop":
        return "unstop"
    if text.startswith("stop"):
        return "stop"
    return "other"


def _record(cur, reply: dict[str, Any], classified: str) -> bool:
    """Insert the collected reply. False if we'd already stored this id.

    The id is comms' own, and the primary key, so a redelivery we somehow
    see twice is a no-op rather than a second row — and, more importantly,
    a second round of consent side effects.
    """
    cur.execute(
        """
        INSERT INTO comms_replies
            (id, channel, from_number, body, in_reply_to, received_at,
             metadata, classified_as)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            str(reply.get("id")),
            reply.get("channel"),
            reply.get("from"),
            reply.get("body"),
            reply.get("in_reply_to"),
            reply.get("received_at"),
            json.dumps(reply.get("metadata") or {}),
            classified,
        ),
    )
    return cur.rowcount == 1


def _apply_consent(cur, phone: str | None, classified: str) -> int:
    """Mirror a STOP/UNSTOP onto any account holding that number.

    Returns the number of accounts touched — normally 0 or 1, and 0 is
    perfectly ordinary: consent is global across every application sharing
    the sender, so we hear about people who have never had an account here.
    That is exactly why this is best-effort bookkeeping and never an
    authorization decision.
    """
    if not phone or classified == "other":
        return 0
    if classified == "stop":
        cur.execute(
            "UPDATE accounts SET sms_opted_out_at = NOW() "
            "WHERE phone_number = %s AND sms_opted_out_at IS NULL",
            (phone,),
        )
    else:
        cur.execute(
            "UPDATE accounts SET sms_opted_out_at = NULL "
            "WHERE phone_number = %s AND sms_opted_out_at IS NOT NULL",
            (phone,),
        )
    return cur.rowcount


def poll_once(limit: int = 50) -> dict[str, Any]:
    """One collection pass. Returns a summary for the CLI log line."""
    if not comms_credentials():
        log.info("comms not configured — skipping reply poll")
        return {"skipped": "unconfigured"}

    replies = poll_replies(limit=limit)
    summary = {"collected": 0, "duplicates": 0, "stop": 0, "unstop": 0,
               "other": 0, "accounts_updated": 0, "unhandled": 0}

    for reply in replies:
        reply_id = str(reply.get("id") or "")
        if not reply_id:
            # Nothing to key on, and nothing to ack. Log the whole thing:
            # it is already gone from comms' queue, so this line is the
            # only record that will ever exist of it.
            log.error("comms returned a reply with no id: %r", reply)
            continue

        classified = classify(reply.get("body"))
        try:
            with connection() as conn:
                with conn.cursor() as cur:
                    fresh = _record(cur, reply, classified)
                    if fresh:
                        summary["accounts_updated"] += _apply_consent(
                            cur, reply.get("from"), classified
                        )
                conn.commit()
        except Exception:  # noqa: BLE001
            # Leaves no row at all, which is the worst case here — hence
            # the loud log with the full payload. One bad reply must not
            # abandon the rest of the batch, though: they are all already
            # claimed, so returning early strands them too.
            log.exception("failed to store comms reply %s: %r", reply_id, reply)
            summary["unhandled"] += 1
            continue

        if not fresh:
            summary["duplicates"] += 1
            continue
        summary["collected"] += 1
        summary[classified] += 1

        # Ack last: it means "we finished", so it must follow the write and
        # the consent update, not race them. A failure here costs only the
        # processed/collected distinction, so it must not fail the run.
        try:
            ack_reply(reply_id)
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE comms_replies SET handled_at = NOW() WHERE id = %s",
                        (reply_id,),
                    )
                conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("failed to ack comms reply %s", reply_id)
            summary["unhandled"] += 1

    log.info("comms reply poll: %r", summary)
    return summary
