"""SMS sign-in: US number handling, the request/verify flow against a fake
login_codes table, and the message that actually goes out.

Code generation/normalization/hashing is not re-tested here — the SMS door
calls literally the same functions as the email door, which
test_login_code.py already covers. What is specific to SMS is the phone
normalization, the send shape, and the failure modes a text has and an
email doesn't (opted out, quota, unusable number).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src import api_auth, comms
from src.accounts import normalize_us_phone


def _request() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/x",
        "headers": [(b"user-agent", b"pytest")], "query_string": b"",
    })


# ---------- US number normalization -------------------------------------------
@pytest.mark.parametrize("raw", [
    "+13035551212",
    "13035551212",
    "3035551212",
    "(303) 555-1212",
    "303-555-1212",
    "303.555.1212",
    " 1 303 555 1212 ",
])
def test_the_forms_a_rider_actually_types_all_resolve(raw):
    assert normalize_us_phone(raw) == "+13035551212"


@pytest.mark.parametrize("raw", [
    "",
    None,
    "555",                 # too short
    "+447700900123",       # not US — this door is US-only
    "+11035551212",        # area code starts with 1
    "+13031551212",        # exchange starts with 1
    "+12115551212",        # N11 area code (211)
    "+13039111212",        # N11 exchange (911)
    "not a number",
])
def test_unusable_input_is_rejected(raw):
    assert normalize_us_phone(raw) is None


def test_area_codes_with_a_middle_nine_are_accepted():
    # 929 (New York), 934, 959, 984 are real. The widely copy-pasted
    # [2-9][0-8]\d area-code pattern predates them and would reject a
    # rider's actual phone number.
    for area in ("929", "934", "959", "984"):
        assert normalize_us_phone(f"{area}5551212") == f"+1{area}5551212"


# ---------- fake DB -----------------------------------------------------------
class _FakeCur:
    def __init__(self, state):
        self.state = state
        self._fetch = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.state.setdefault("sql", []).append((s, params))
        if s.startswith("INSERT INTO login_codes"):
            self.state["inserted"] = params
            self._fetch = (self.state.get("new_code_id", 42),)
        elif s.startswith("SELECT id, code_hash FROM login_codes"):
            self._fetch = self.state.get("row")
        elif "attempts = attempts + 1" in s:
            self._fetch = (self.state.get("claimed", 1),)
        elif "RETURNING id" in s:
            self._fetch = (1,)
        elif "SELECT COUNT(*), MIN(at)" in s:      # ratelimit.enforce
            self._fetch = (self.state.get("rate_count", 0), None)
        else:
            self._fetch = None

    def fetchone(self):
        return self._fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCur(self.state)

    def commit(self):
        self.state["committed"] = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def db(monkeypatch):
    state: dict = {}

    @contextmanager
    def fake_connection():
        yield _FakeConn(state)

    monkeypatch.setattr(api_auth, "connection", fake_connection)
    monkeypatch.setenv("COMMS_TOKEN", "tok")
    return state


@pytest.fixture
def sent(monkeypatch):
    """Capture the send instead of performing it."""
    calls = []

    def fake_send(to, body, **kw):
        calls.append({"to": to, "body": body, **kw})
        return {"id": "m1", "fell_back": False}

    monkeypatch.setattr(api_auth, "send_sms", fake_send)
    return calls


# ---------- request -----------------------------------------------------------
def test_request_503s_when_comms_is_unconfigured(db, monkeypatch):
    monkeypatch.delenv("COMMS_TOKEN", raising=False)
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_request(
            _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
        )
    assert e.value.status_code == 503


def test_request_400s_on_a_non_us_number(db, sent):
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_request(
            _request(), api_auth.SmsCodeRequestIn(phone_number="+447700900123")
        )
    assert e.value.status_code == 400
    assert not sent  # never spend a message on a number we know is wrong


def test_request_texts_the_specified_message(db, sent):
    out = api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="(303) 555-1212")
    )
    assert out == {"sent": True}
    assert len(sent) == 1
    call = sent[0]
    assert call["to"] == "+13035551212"          # normalized before sending
    assert call["body"].startswith("Use code ")
    assert call["body"].endswith(" to login.")
    # The site name is comms' job, not ours — it prefixes "scooter.fyi: "
    # server-side, so naming the site here would say it twice.
    assert "scooter.fyi" not in call["body"]
    # 26 here, 39 delivered — one GSM-7 segment either way.
    assert len(call["body"]) == 26
    # A code that lands late is worse than one that never lands.
    assert call["ttl_seconds"] == api_auth.SMS_SEND_TTL_SECONDS
    assert call["urgent"] is True


def test_the_body_carries_the_code_that_was_stored(db, sent):
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    destination, code_hash, _expires, _ip = db["inserted"]
    assert destination == "+13035551212"
    code = sent[0]["body"].removeprefix("Use code ").split(" ")[0]
    assert api_auth._hash_code("+13035551212", code) == code_hash


def test_the_idempotency_key_names_the_issuance_not_the_attempt(db, sent):
    db["new_code_id"] = 4242
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    # The login_codes row id — stable across a retry of THIS send, and
    # different for a genuinely new code. A per-call UUID would dedupe
    # nothing.
    assert sent[0]["idempotency_key"] == "login-code-4242"


def test_the_code_is_stored_against_the_phone_column(db, sent):
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    insert = [s for s, _ in db["sql"] if s.startswith("INSERT INTO login_codes")][0]
    assert "phone_number" in insert and "(email," not in insert


def test_prior_live_codes_are_superseded_only_once_the_text_is_away(db, sent, monkeypatch):
    """Only the newest code stays live — but for SMS that rule is applied
    AFTER the send, not before it. Burning at issue time destroys a working
    code whenever comms refuses (review #32.3); the settle step restores the
    single-guess-target invariant once we know the text actually went."""
    calls = _settle_calls(monkeypatch)
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    burned_at_issue = [s for s, _ in db["sql"]
                       if s.startswith("UPDATE login_codes SET used_at")
                       and "phone_number = %s" in s]
    assert not burned_at_issue
    assert calls[0]["delivered"] is True and calls[0]["destination"] == "+13035551212"


def test_the_send_happens_after_the_commit(db, sent, monkeypatch):
    """A comms outage must not roll back the rate-limit events — otherwise
    a broken sender is an unmetered retry loop."""
    order = []
    real_commit = _FakeConn.commit

    def tracking_commit(self):
        order.append("commit")
        real_commit(self)

    monkeypatch.setattr(_FakeConn, "commit", tracking_commit)
    monkeypatch.setattr(api_auth, "send_sms",
                        lambda *a, **k: order.append("send") or {"id": "m"})
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    # The trailing commit is _settle_issued_code deciding which code is
    # live now that the send has resolved — it necessarily comes last.
    assert order[:2] == ["commit", "send"]


# ---------- send failures a text has and an email doesn't ---------------------
def test_opted_out_becomes_409_carrying_the_unblock_instructions(db, monkeypatch):
    sentence = "Recipient blocked communications, text UNSTOP to +17202803332 to unblock."

    def opted_out(*a, **k):
        raise comms.OptedOut(sentence)

    monkeypatch.setattr(api_auth, "send_sms", opted_out)
    monkeypatch.setattr(api_auth, "_note_opt_out", lambda phone: None)
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_request(
            _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
        )
    assert e.value.status_code == 409
    # Passed through untouched — a paraphrase names a keyword that doesn't work.
    assert e.value.detail == sentence


@pytest.mark.parametrize("exc,status", [
    (comms.UnusableRecipient("x"), 400),
    (comms.QuotaExceeded("x"), 429),
    (comms.CommsError("x"), 502),
])
def test_send_failures_map_to_useful_statuses(db, monkeypatch, exc, status):
    def raiser(*a, **k):
        raise exc

    monkeypatch.setattr(api_auth, "send_sms", raiser)
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_request(
            _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
        )
    assert e.value.status_code == status


# ---------- verify ------------------------------------------------------------
def test_verify_rejects_a_wrong_code(db, monkeypatch):
    db["row"] = (1, api_auth._hash_code("+13035551212", "AB123XY"))
    monkeypatch.setattr(api_auth, "upsert_account_by_phone",
                        lambda cur, phone: pytest.fail("must not reach the account"))
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_verify(
            _request(),
            api_auth.SmsCodeVerifyIn(phone_number="3035551212", code="ZZ999ZZ"),
        )
    assert e.value.status_code == 401


def test_verify_rejects_when_no_live_code_exists(db):
    db["row"] = None
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_verify(
            _request(),
            api_auth.SmsCodeVerifyIn(phone_number="3035551212", code="AB123XY"),
        )
    assert e.value.status_code == 401


def test_too_many_attempts_burns_the_code(db):
    db["row"] = (1, api_auth._hash_code("+13035551212", "AB123XY"))
    db["claimed"] = api_auth.MAX_CODE_ATTEMPTS + 1
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_verify(
            _request(),
            api_auth.SmsCodeVerifyIn(phone_number="3035551212", code="AB123XY"),
        )
    assert e.value.status_code == 401
    assert "too many attempts" in e.value.detail


def test_verify_accepts_a_lowercase_hyphenated_code_off_a_phone_screen(db, monkeypatch):
    db["row"] = (1, api_auth._hash_code("+13035551212", "AB123XY"))
    monkeypatch.setattr(api_auth, "upsert_account_by_phone", lambda cur, phone: 7)
    minted = {}

    def fake_mint(cur, **kw):
        minted.update(kw)
        from datetime import datetime, timezone
        return "tok", datetime(2026, 8, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(api_auth, "mint_session", fake_mint)
    out = api_auth.auth_sms_code_verify(
        _request(),
        api_auth.SmsCodeVerifyIn(phone_number="(303) 555-1212", code="ab-123-xy"),
    )
    assert out["token"] == "tok"
    assert minted["method"] == "sms_code"
    assert minted["account_id"] == 7


def test_a_contested_number_is_a_409_not_a_takeover(db, monkeypatch):
    from src.accounts import PhoneNumberContested

    db["row"] = (1, api_auth._hash_code("+13035551212", "AB123XY"))

    def contested(cur, phone):
        raise PhoneNumberContested("held unverified by an email-less account")

    monkeypatch.setattr(api_auth, "upsert_account_by_phone", contested)
    monkeypatch.setattr(api_auth, "mint_session",
                        lambda *a, **k: pytest.fail("must not mint a session"))
    with pytest.raises(HTTPException) as e:
        api_auth.auth_sms_code_verify(
            _request(),
            api_auth.SmsCodeVerifyIn(phone_number="3035551212", code="AB123XY"),
        )
    assert e.value.status_code == 409


# ---------- capability advertisement ------------------------------------------
def test_auth_config_reports_sms_off_without_a_token(monkeypatch):
    from starlette.responses import Response

    monkeypatch.delenv("COMMS_TOKEN", raising=False)
    assert api_auth.auth_config(Response())["sms_enabled"] is False


def test_auth_config_reports_sms_on_with_a_token(monkeypatch):
    from starlette.responses import Response

    monkeypatch.setenv("COMMS_TOKEN", "tok")
    assert api_auth.auth_config(Response())["sms_enabled"] is True


# ---------- review #32.3: a failed send must not destroy a working code ------
def _settle_calls(monkeypatch):
    """Capture _settle_issued_code instead of touching a database."""
    calls = []
    monkeypatch.setattr(
        api_auth, "_settle_issued_code",
        lambda column, destination, code_id, *, delivered: calls.append(
            {"column": column, "destination": destination,
             "code_id": code_id, "delivered": delivered}
        ),
    )
    return calls


def test_issuing_does_not_burn_prior_codes_before_the_send(db, sent):
    """The burn is deferred, so a rider holding a working code keeps it
    until we know the new one actually went out."""
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    burns = [s for s, _ in db["sql"]
             if s.startswith("UPDATE login_codes SET used_at")
             and "phone_number = %s" in s and "id <> %s" not in s]
    assert not burns, "prior codes were burned before the send resolved"


def test_a_delivered_code_supersedes_the_older_ones(db, sent, monkeypatch):
    calls = _settle_calls(monkeypatch)
    db["new_code_id"] = 99
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    assert calls == [{"column": "phone_number", "destination": "+13035551212",
                      "code_id": 99, "delivered": True}]


@pytest.mark.parametrize("exc", [
    comms.CommsError("transport down"),
    comms.QuotaExceeded("over quota"),
])
def test_an_undelivered_code_is_burned_instead_of_the_old_one(db, monkeypatch, exc):
    """The rider tapped resend during an outage. Their previous code has to
    survive: burning it would leave them with nothing, having also spent one
    of three hourly slots to get there."""
    calls = _settle_calls(monkeypatch)
    db["new_code_id"] = 77

    def raiser(*a, **k):
        raise exc

    monkeypatch.setattr(api_auth, "send_sms", raiser)
    with pytest.raises(HTTPException):
        api_auth.auth_sms_code_request(
            _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
        )
    assert calls == [{"column": "phone_number", "destination": "+13035551212",
                      "code_id": 77, "delivered": False}]


def test_an_opted_out_send_also_leaves_the_previous_code_alone(db, monkeypatch):
    calls = _settle_calls(monkeypatch)

    def opted_out(*a, **k):
        raise comms.OptedOut("text UNSTOP to +17202803332 to unblock.")

    monkeypatch.setattr(api_auth, "send_sms", opted_out)
    monkeypatch.setattr(api_auth, "_note_opt_out", lambda phone: None)
    with pytest.raises(HTTPException):
        api_auth.auth_sms_code_request(
            _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
        )
    assert calls and calls[0]["delivered"] is False


# ---------- review #32.2: the global cap must not lock out proven owners -----
def test_an_unproven_number_is_subject_to_the_global_daily_cap(db, sent, monkeypatch):
    monkeypatch.setattr(api_auth, "phone_is_verified", lambda cur, phone: False)
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    keys = [p[0] for s, p in db["sql"]
            if s.startswith("SELECT pg_advisory_xact_lock") and p]
    assert any("sms_code_global:all" in k for k in keys)


def test_a_proven_owner_skips_the_global_cap_but_not_the_per_number_one(db, sent, monkeypatch):
    """13 IPs can drain the daily bucket in an hour. A rider whose only door
    is SMS must not be locked out by that — but stays capped per number, so
    the exemption is bounded."""
    monkeypatch.setattr(api_auth, "phone_is_verified", lambda cur, phone: True)
    api_auth.auth_sms_code_request(
        _request(), api_auth.SmsCodeRequestIn(phone_number="3035551212")
    )
    keys = [p[0] for s, p in db["sql"]
            if s.startswith("SELECT pg_advisory_xact_lock") and p]
    assert not any("sms_code_global" in k for k in keys)
    assert any("sms_code_phone:+13035551212" in k for k in keys)
    assert any("sms_code_ip:" in k for k in keys)
