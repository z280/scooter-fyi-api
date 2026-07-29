"""Postgres-backed coverage for phone ownership — the security half of SMS
sign-in.

A fake cursor cannot catch what matters here. The rules being tested are
enforced partly by real constraints (accounts_phone_number_key,
accounts_email_or_phone_required, login_codes_one_destination) and partly
by code that only makes sense in their presence, so the test has to talk to
a database that has them.

The property under test, in one sentence: **a phone number typed into a
profile is an assertion, and only typing back a texted code is proof** —
so an unverified claim can never intercept somebody else's sign-in.

SKIPS unless VEO_TEST_PG_DSN names a reachable, migratable database (same
contract as tests/test_auth_method_constraint_pg.py). NEVER point that at
production: the fixture executes every migration.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from src import accounts as acct  # noqa: E402
from src import api_auth  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

_PHONE = "+13035550101"
_OTHER_PHONE = "+13035550102"
_EMAIL_LIKE = "pgtest-phone-%@example.com"


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture()
def pg_conn(monkeypatch):
    dsn = os.environ.get("VEO_TEST_PG_DSN")
    if not dsn:
        pytest.skip("VEO_TEST_PG_DSN not set — phone verification Postgres test skipped")
    if not _reachable(dsn):
        pytest.skip(f"VEO_TEST_PG_DSN unreachable ({dsn})")

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        for path in sorted(SQL_DIR.glob("*.sql")):
            cur.execute(path.read_text())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE email LIKE %s OR phone_number IN (%s, %s)",
                    (_EMAIL_LIKE, _PHONE, _OTHER_PHONE))
        cur.execute("DELETE FROM login_codes WHERE phone_number IN (%s, %s)",
                    (_PHONE, _OTHER_PHONE))
    conn.commit()

    @contextmanager
    def _fake_connection():
        yield conn

    monkeypatch.setattr(api_auth, "connection", _fake_connection)
    monkeypatch.setattr(api_auth, "enforce", lambda cur, **kw: None)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _make_account(cur, *, email=None, phone=None, verified=False) -> int:
    cur.execute(
        """
        INSERT INTO accounts (email, phone_number, phone_verified_at, last_login_at)
        VALUES (%s, %s, CASE WHEN %s THEN NOW() END, NOW())
        RETURNING id
        """,
        (email, phone, verified),
    )
    account_id = int(cur.fetchone()[0])
    acct.assign_public_username(cur, account_id)
    return account_id


def _row(cur, account_id):
    cur.execute(
        "SELECT email, phone_number, phone_verified_at FROM accounts WHERE id = %s",
        (account_id,),
    )
    return cur.fetchone()


# ---------- the takeover this whole design exists to prevent -------------------
def test_an_unverified_claim_does_not_intercept_someone_elses_sign_in(pg_conn):
    """The attack: put a stranger's number in your profile, wait for them to
    sign in by SMS, receive their account. The claim must lose."""
    with pg_conn.cursor() as cur:
        attacker = _make_account(cur, email="pgtest-phone-attacker@example.com",
                                 phone=_PHONE, verified=False)

        # The real owner proves the number.
        victim = acct.upsert_account_by_phone(cur, _PHONE)

        assert victim != attacker, "SMS sign-in resolved into the claimant's account"
        assert _row(cur, victim)[2] is not None      # the prover is verified
        assert _row(cur, attacker)[1] is None        # the claimant lost the number
        # ...but keeps their own door. Losing an unproven claim must not
        # lock somebody out of an account they can still authenticate to.
        assert _row(cur, attacker)[0] == "pgtest-phone-attacker@example.com"
    pg_conn.rollback()


def test_the_proven_owner_signs_back_into_the_same_account(pg_conn):
    with pg_conn.cursor() as cur:
        first = acct.upsert_account_by_phone(cur, _PHONE)
        again = acct.upsert_account_by_phone(cur, _PHONE)
        assert first == again
    pg_conn.rollback()


def test_an_unverified_holder_with_no_other_door_is_refused_not_stranded(pg_conn):
    """Releasing the number would leave a row with neither email nor phone —
    forbidden by accounts_email_or_phone_required, and an account with no
    way back in even if it weren't. Adopting it would be the takeover. So
    we refuse and let a human decide."""
    with pg_conn.cursor() as cur:
        stranded = _make_account(cur, email=None, phone=_PHONE, verified=False)
        with pytest.raises(acct.PhoneNumberContested):
            acct.upsert_account_by_phone(cur, _PHONE)
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        # And nothing was half-done to it.
        cur.execute("SELECT COUNT(*) FROM accounts WHERE id = %s", (stranded,))
    pg_conn.rollback()


# ---------- the honest path: proving the number you already listed ------------
def test_verifying_from_your_own_session_attaches_to_that_account(pg_conn):
    """Without this bridge, a rider who lists their number and then uses SMS
    sign-in silently ends up with a second, empty account."""
    with pg_conn.cursor() as cur:
        mine = _make_account(cur, email="pgtest-phone-mine@example.com",
                             phone=_PHONE, verified=False)
        acct.claim_verified_phone(cur, mine, _PHONE)
        email, phone, verified_at = _row(cur, mine)
        assert phone == _PHONE and verified_at is not None

        # And now SMS sign-in lands in that same account rather than forking.
        assert acct.upsert_account_by_phone(cur, _PHONE) == mine
    pg_conn.rollback()


def test_claiming_a_number_someone_else_proved_is_refused(pg_conn):
    with pg_conn.cursor() as cur:
        owner = _make_account(cur, email="pgtest-phone-owner@example.com",
                              phone=_PHONE, verified=True)
        interloper = _make_account(cur, email="pgtest-phone-other@example.com")
        with pytest.raises(acct.PhoneNumberTaken):
            acct.claim_verified_phone(cur, interloper, _PHONE)
    pg_conn.rollback()


def test_reverifying_your_own_number_is_idempotent(pg_conn):
    with pg_conn.cursor() as cur:
        mine = _make_account(cur, email="pgtest-phone-mine2@example.com",
                             phone=_PHONE, verified=True)
        acct.claim_verified_phone(cur, mine, _PHONE)   # must not raise Taken
        assert _row(cur, mine)[2] is not None
    pg_conn.rollback()


# ---------- schema-level guarantees -------------------------------------------
def test_a_login_code_must_have_exactly_one_destination(pg_conn):
    with pg_conn.cursor() as cur:
        for email, phone in ((None, None), ("x@example.com", _PHONE)):
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO login_codes (email, phone_number, code_hash, expires_at) "
                    "VALUES (%s, %s, 'h', NOW() + INTERVAL '10 minutes')",
                    (email, phone),
                )
            pg_conn.rollback()


def test_a_phone_only_account_is_allowed(pg_conn):
    # sql/025 made email nullable precisely so a proved phone can be an
    # account's whole identity.
    with pg_conn.cursor() as cur:
        account_id = acct.upsert_account_by_phone(cur, _OTHER_PHONE)
        email, phone, verified_at = _row(cur, account_id)
        assert email is None and phone == _OTHER_PHONE and verified_at is not None
    pg_conn.rollback()


def test_two_accounts_cannot_hold_the_same_number(pg_conn):
    with pg_conn.cursor() as cur:
        _make_account(cur, email="pgtest-phone-a@example.com", phone=_PHONE)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _make_account(cur, email="pgtest-phone-b@example.com", phone=_PHONE)
    pg_conn.rollback()
