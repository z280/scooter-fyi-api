"""Inbound reply handling: keyword classification and the collect loop.

The loop's invariants matter more than its output, because polling CLAIMS
what it returns: whatever this process does with a reply, nobody will ever
get a second chance at it. So: store before interpreting, don't let one bad
reply strand the rest of the batch, and never apply the same STOP twice.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from src import comms_replies


# ---------- classification ----------------------------------------------------
@pytest.mark.parametrize("body", [
    "STOP", "stop", " Stop ", "STOP.", "stopall", "unsubscribe", "CANCEL", "quit", "end",
])
def test_opt_out_keywords(body):
    assert comms_replies.classify(body) == "stop"


@pytest.mark.parametrize("body", ["START", "unstop", "UNSTOP!", "yes", "subscribe"])
def test_opt_in_keywords(body):
    assert comms_replies.classify(body) == "unstop"


@pytest.mark.parametrize("body", [
    None,
    "",
    "what time again?",
    # Contains STOP, means the opposite. A substring match here would
    # unsubscribe someone who asked to keep hearing from us, and they'd
    # have no way to tell it happened.
    "please don't stop texting me the good ones",
    "stop by the shop later",
])
def test_everything_else_is_other(body):
    assert comms_replies.classify(body) == "other"


# ---------- fake DB -----------------------------------------------------------
class _FakeCur:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.state.setdefault("sql", []).append((s, params))
        if s.startswith("INSERT INTO comms_replies"):
            reply_id = params[0]
            seen = self.state.setdefault("stored", {})
            if reply_id in seen:
                self.rowcount = 0          # ON CONFLICT DO NOTHING
            else:
                seen[reply_id] = params
                self.rowcount = 1
        elif "UPDATE accounts SET sms_opted_out_at" in s:
            self.state.setdefault("consent", []).append((s, params))
            self.rowcount = self.state.get("accounts_matched", 1)
        elif "UPDATE comms_replies SET handled_at" in s:
            self.state.setdefault("handled", []).append(params[0])
            self.rowcount = 1
        else:
            self.rowcount = 0

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
        self.state["commits"] = self.state.get("commits", 0) + 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(monkeypatch):
    state: dict = {}

    @contextmanager
    def fake_connection():
        yield _FakeConn(state)

    monkeypatch.setattr(comms_replies, "connection", fake_connection)
    monkeypatch.setenv("COMMS_TOKEN", "tok")
    return state


def _serve(monkeypatch, replies, *, acks=None):
    monkeypatch.setattr(comms_replies, "poll_replies", lambda limit=50: replies)
    monkeypatch.setattr(comms_replies, "ack_reply",
                        lambda rid: (acks if acks is not None else []).append(rid))


def _reply(rid, body, frm="+13035551212"):
    return {"id": rid, "channel": "sms", "from": frm, "body": body,
            "in_reply_to": "m1", "received_at": "2026-07-29T11:24:49Z", "metadata": {}}


# ---------- the loop ----------------------------------------------------------
def test_unconfigured_is_a_no_op_not_a_failure(env, monkeypatch):
    monkeypatch.delenv("COMMS_TOKEN", raising=False)
    monkeypatch.setattr(comms_replies, "poll_replies",
                        lambda limit=50: pytest.fail("must not poll"))
    assert comms_replies.poll_once() == {"skipped": "unconfigured"}


def test_a_stop_is_stored_and_mirrored_onto_the_account(env, monkeypatch):
    acks = []
    _serve(monkeypatch, [_reply("r1", "STOP")], acks=acks)
    out = comms_replies.poll_once()
    assert out["collected"] == 1 and out["stop"] == 1
    assert env["stored"]["r1"][7] == "stop"       # classified_as column
    assert env["consent"] and "sms_opted_out_at = NOW()" in env["consent"][0][0]
    assert acks == ["r1"]
    assert env["handled"] == ["r1"]


def test_an_unstop_clears_the_local_opt_out(env, monkeypatch):
    _serve(monkeypatch, [_reply("r2", "UNSTOP")])
    out = comms_replies.poll_once()
    assert out["unstop"] == 1
    assert "sms_opted_out_at = NULL" in env["consent"][0][0]


def test_an_ordinary_reply_is_stored_but_touches_no_consent(env, monkeypatch):
    _serve(monkeypatch, [_reply("r3", "what time again?")])
    out = comms_replies.poll_once()
    assert out["other"] == 1
    assert "consent" not in env


def test_a_stop_from_a_stranger_is_stored_without_a_matching_account(env, monkeypatch):
    # Consent is global across every application on the shared sender, so
    # we routinely hear about people who have no account here. That is
    # ordinary, not an error.
    env["accounts_matched"] = 0
    _serve(monkeypatch, [_reply("r4", "STOP", frm="+13039995555")])
    out = comms_replies.poll_once()
    assert out["collected"] == 1 and out["accounts_updated"] == 0


def test_a_replayed_id_applies_its_consent_only_once(env, monkeypatch):
    _serve(monkeypatch, [_reply("r5", "STOP")])
    comms_replies.poll_once()
    first = len(env["consent"])
    _serve(monkeypatch, [_reply("r5", "STOP")])
    out = comms_replies.poll_once()
    assert out["duplicates"] == 1 and out["collected"] == 0
    assert len(env["consent"]) == first      # no second application


def test_the_row_is_written_before_the_ack(env, monkeypatch):
    order = []
    monkeypatch.setattr(comms_replies, "poll_replies", lambda limit=50: [_reply("r6", "STOP")])
    monkeypatch.setattr(comms_replies, "ack_reply", lambda rid: order.append("ack"))
    real_execute = _FakeCur.execute

    def tracking(self, sql, params=None):
        if " ".join(sql.split()).startswith("INSERT INTO comms_replies"):
            order.append("store")
        real_execute(self, sql, params)

    monkeypatch.setattr(_FakeCur, "execute", tracking)
    comms_replies.poll_once()
    assert order == ["store", "ack"]


def test_a_failed_ack_leaves_the_row_unhandled_for_a_human(env, monkeypatch):
    def boom(rid):
        raise RuntimeError("comms down")

    monkeypatch.setattr(comms_replies, "poll_replies", lambda limit=50: [_reply("r7", "STOP")])
    monkeypatch.setattr(comms_replies, "ack_reply", boom)
    out = comms_replies.poll_once()
    # Collected and acted on, but never marked handled — which is exactly
    # the "we have it and didn't finish" state the index looks for.
    assert out["collected"] == 1 and out["unhandled"] == 1
    assert "handled" not in env


def test_one_unstorable_reply_does_not_strand_the_rest_of_the_batch(env, monkeypatch):
    real_execute = _FakeCur.execute

    def selective(self, sql, params=None):
        if " ".join(sql.split()).startswith("INSERT INTO comms_replies") and params[0] == "bad":
            raise RuntimeError("constraint blew up")
        real_execute(self, sql, params)

    monkeypatch.setattr(_FakeCur, "execute", selective)
    _serve(monkeypatch, [_reply("bad", "STOP"), _reply("good", "STOP")])
    out = comms_replies.poll_once()
    # They were ALL claimed by the poll — returning early would lose the
    # good one too, permanently.
    assert out["collected"] == 1 and out["unhandled"] == 1
    assert "good" in env["stored"]


def test_a_reply_with_no_id_is_skipped_loudly(env, monkeypatch):
    _serve(monkeypatch, [{"body": "STOP", "from": "+13035551212"}])
    out = comms_replies.poll_once()
    assert out["collected"] == 0
    assert "stored" not in env
