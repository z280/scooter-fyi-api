"""src/dibs_watch.py against a real database.

A fake cursor can prove which claims the classifier picks. Only a real
Postgres can prove the thing this module has to get right, which is that one
rental produces one text however many cycles it spans — the ingest cycle runs
every couple of minutes, a rental lasts many of them, and the guard is a
column.

The other half is the phone gate. `accounts.phone_verified_at` is the only
thing that counts, and the failure mode of getting that wrong is texting a
stranger about a scooter because a rider mistyped a digit.

SKIPS unless VEO_TEST_PG_DSN names a reachable, migratable database. NEVER
point it at production: the fixture replays every migration and deletes its
own dibs rows and accounts.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import dibs_watch  # noqa: E402
from src.accounts import upsert_account  # noqa: E402
from src.comms import CommsError, OptedOut, QuotaExceeded, UnusableRecipient  # noqa: E402
from src.ingest import TaggedDevice  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_TEST_EMAIL_LIKE = "pgtest-dibswatch-%@example.com"
_VID = "dddd000000000000"
NOW = datetime(2026, 10, 8, 17, 30, tzinfo=timezone.utc)


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — dibs_watch Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()

    def _clean() -> None:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dibs WHERE vehicle_identifier = %s", (_VID,))
            cur.execute("DELETE FROM accounts WHERE email LIKE %s", (_TEST_EMAIL_LIKE,))
        conn.commit()

    _clean()

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(dibs_watch, "connection", _conn)
    yield conn
    _clean()
    conn.close()


class _Outbox(list):
    """The sends that were attempted. `fail_with` makes every later attempt
    fail the way comms would — the attempt is still recorded, because
    "we tried and it bounced" is what the skip columns are about."""

    def __init__(self) -> None:
        super().__init__()
        self.error: Exception | None = None

    def fail_with(self, error: Exception) -> None:
        self.error = error


@pytest.fixture()
def sent(monkeypatch) -> _Outbox:
    outbox = _Outbox()

    def _send(to, body, *, idempotency_key, **kw):
        outbox.append({"to": to, "body": body, "key": idempotency_key})
        if outbox.error is not None:
            raise outbox.error
        return {"id": "msg-1"}

    monkeypatch.setattr(dibs_watch, "send_sms", _send)
    return outbox


def _account(conn, tag: str, *, phone: str | None, verified: bool) -> int:
    with conn.cursor() as cur:
        account_id = upsert_account(cur, email=f"pgtest-dibswatch-{tag}@example.com")
        # `accounts.phone_number` is globally unique (sql/025), so a number
        # another suite left on another row would fail this insert rather than
        # this suite's own logic. Release it first — these are test numbers in
        # a throwaway database, and a collision here says nothing about the
        # code under test.
        if phone is not None:
            cur.execute(
                "UPDATE accounts SET phone_number = NULL "
                "WHERE phone_number = %s AND id <> %s",
                (phone, account_id),
            )
        cur.execute(
            "UPDATE accounts SET phone_number = %s, "
            "phone_verified_at = CASE WHEN %s THEN NOW() ELSE NULL END "
            "WHERE id = %s",
            (phone, verified, account_id),
        )
    conn.commit()
    return account_id


def _claim(conn, account_id: int | None, *, notify: bool = True,
           minutes_left: int = 20, dibs_id: str = "watch-1",
           mine: bool = False) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dibs (id, vehicle_identifier, vehicle_name, claimed_by,
                              claimed_at, expires_at, account_id, notify_sms,
                              mine_at)
            VALUES (%s, %s, 'Perseus 619', 'Zach', %s, %s, %s, %s,
                    CASE WHEN %s THEN NOW() ELSE NULL END)
            """,
            (dibs_id, _VID, NOW - timedelta(minutes=5),
             NOW + timedelta(minutes=minutes_left), account_id, notify, mine),
        )
    conn.commit()
    return dibs_id


def _row(conn, dibs_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT taken_at, notified_at, notify_skipped FROM dibs WHERE id = %s",
            (dibs_id,),
        )
        return cur.fetchone()


def _reserved() -> list[TaggedDevice]:
    return [TaggedDevice(
        device_id="bike-1", vehicle_type_id="1", form_factor="scooter",
        lat=39.74, lon=-104.98, spatial_status="denver_core",
        vehicle_identifier=_VID, is_reserved=True,
    )]


def _free() -> list[TaggedDevice]:
    return [TaggedDevice(
        device_id="bike-1", vehicle_type_id="1", form_factor="scooter",
        lat=39.74, lon=-104.98, spatial_status="denver_core",
        vehicle_identifier=_VID, is_reserved=False,
    )]


def test_a_verified_rider_gets_one_text_for_one_rental(pg, sent):
    """THE GUARD. Five cycles over one rental, one text."""
    acct = _account(pg, "ok", phone="+17205550101", verified=True)
    dibs_id = _claim(pg, acct)

    for i in range(5):
        dibs_watch.watch_claims_for_cycle(NOW + timedelta(minutes=2 * i), _reserved())

    assert len(sent) == 1
    assert sent[0]["to"] == "+17205550101"
    assert sent[0]["key"] == f"dibs-taken:{dibs_id}"
    taken_at, notified_at, skipped = _row(pg, dibs_id)
    assert taken_at is not None
    assert notified_at is not None
    assert skipped is None


def test_a_scooter_nobody_touched_texts_nobody(pg, sent):
    acct = _account(pg, "quiet", phone="+17205550102", verified=True)
    dibs_id = _claim(pg, acct)

    stats = dibs_watch.watch_claims_for_cycle(NOW, _free())

    assert stats.open_claims == 1
    assert stats.newly_taken == 0
    assert sent == []
    assert _row(pg, dibs_id) == (None, None, None)


def test_an_unverified_number_is_never_texted(pg, sent):
    """A number typed into a profile is a number nobody has proved they
    answer. Texting it means texting a stranger about a scooter."""
    acct = _account(pg, "unver", phone="+17205550103", verified=False)
    dibs_id = _claim(pg, acct)

    dibs_watch.watch_claims_for_cycle(NOW, _reserved())

    assert sent == []
    taken_at, notified_at, skipped = _row(pg, dibs_id)
    # The observation still stands: dibs WERE disrespected, we just could not
    # say so. Those are different facts and the row keeps both.
    assert taken_at is not None
    assert notified_at is None
    assert skipped == dibs_watch.SKIP_UNVERIFIED


def test_no_number_at_all_is_recorded_rather_than_silent(pg, sent):
    acct = _account(pg, "nophone", phone=None, verified=False)
    dibs_id = _claim(pg, acct)

    dibs_watch.watch_claims_for_cycle(NOW, _reserved())

    assert sent == []
    assert _row(pg, dibs_id)[2] == dibs_watch.SKIP_NO_PHONE


def test_a_rider_who_said_they_are_riding_it_is_not_told_they_were_robbed(pg, sent):
    """The commonest rental on a claimed scooter is the claimant's own."""
    acct = _account(pg, "mine", phone="+17205550104", verified=True)
    _claim(pg, acct, mine=True)

    stats = dibs_watch.watch_claims_for_cycle(NOW, _reserved())

    assert stats.open_claims == 0
    assert sent == []


def test_a_claim_that_did_not_ask_is_not_watched(pg, sent):
    acct = _account(pg, "optout", phone="+17205550105", verified=True)
    _claim(pg, acct, notify=False)

    stats = dibs_watch.watch_claims_for_cycle(NOW, _reserved())

    assert stats.open_claims == 0
    assert sent == []


def test_an_expired_claim_is_not_watched(pg, sent):
    """Twenty-five minutes is the whole life of a claim. A rental an hour
    later is somebody else's ordinary ride."""
    acct = _account(pg, "expired", phone="+17205550106", verified=True)
    _claim(pg, acct, minutes_left=-5)

    stats = dibs_watch.watch_claims_for_cycle(NOW, _reserved())

    assert stats.open_claims == 0
    assert sent == []


def test_a_signed_out_claim_has_nobody_to_text(pg, sent):
    _claim(pg, None)
    stats = dibs_watch.watch_claims_for_cycle(NOW, _reserved())
    assert stats.open_claims == 0
    assert sent == []


def test_an_opt_out_at_comms_is_permanent_for_this_claim(pg, sent):
    acct = _account(pg, "stop", phone="+17205550107", verified=True)
    dibs_id = _claim(pg, acct)
    sent.fail_with(OptedOut("they texted STOP"))

    dibs_watch.watch_claims_for_cycle(NOW, _reserved())
    dibs_watch.watch_claims_for_cycle(NOW + timedelta(minutes=2), _reserved())

    # Tried once, recorded, never tried again.
    assert len(sent) == 1
    assert _row(pg, dibs_id)[2] == dibs_watch.SKIP_OPTED_OUT


def test_an_unusable_number_is_permanent_too(pg, sent):
    acct = _account(pg, "bad", phone="+17205550108", verified=True)
    dibs_id = _claim(pg, acct)
    sent.fail_with(UnusableRecipient("landline"))

    dibs_watch.watch_claims_for_cycle(NOW, _reserved())

    assert _row(pg, dibs_id)[2] == dibs_watch.SKIP_UNUSABLE


@pytest.mark.parametrize("err", [QuotaExceeded("over quota"), CommsError("comms down")])
def test_a_transient_failure_is_retried_next_cycle(pg, sent, err):
    """Recording a skip here would turn a thirty-second comms outage into a
    permanently unanswered claim. The retry window is the claim's own life,
    which is the right bound: after that there is nothing useful to say."""
    acct = _account(pg, "retry", phone="+17205550109", verified=True)
    dibs_id = _claim(pg, acct)
    sent.fail_with(err)

    stats = dibs_watch.watch_claims_for_cycle(NOW, _reserved())
    assert stats.deferred == 1
    taken_at, notified_at, skipped = _row(pg, dibs_id)
    assert taken_at is not None
    assert notified_at is None
    assert skipped is None

    # Still watched on the next pass — which is the whole assertion.
    stats = dibs_watch.watch_claims_for_cycle(NOW + timedelta(minutes=2), _reserved())
    assert stats.open_claims == 1
    assert len(sent) == 2


def test_the_observation_survives_a_send_that_never_lands(pg, sent):
    """`taken_at` is stamped before any sending: "were dibs disrespected" is
    a different question from "did we manage to tell anybody", and a failed
    send must not erase the answer to the first."""
    acct = _account(pg, "obs", phone="+17205550110", verified=True)
    dibs_id = _claim(pg, acct)
    sent.fail_with(CommsError("comms down"))

    dibs_watch.watch_claims_for_cycle(NOW, _reserved())

    assert _row(pg, dibs_id)[0] is not None
