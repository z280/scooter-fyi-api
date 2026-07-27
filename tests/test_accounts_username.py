"""Unit tests for the public_username generator/assigner/chooser and
phone-number helpers (src/accounts.py). Postgres-specific behavior (the
upsert_account xmax=0 branch, the real UNIQUE/FK constraints, the
generated public_username column) needs a real database and isn't
exercised here — these tests only cover the pure Python control flow
against a fake cursor with a scripted fetchone() queue."""

from __future__ import annotations

import pytest

from src.accounts import (
    InvalidUsernameChoice,
    assign_public_username,
    choose_public_username,
    generate_public_username,
    is_valid_phone_number,
    normalize_phone_number,
)


class _FakeCursor:
    def __init__(self, fetch):
        self._fetch = list(fetch)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetch.pop(0)


def test_generate_public_username_returns_adjective_and_emoji():
    cur = _FakeCursor([("brave",), ("🦉",)])
    assert generate_public_username(cur) == ("brave", "🦉")


# ---------- assign_public_username (random) --------------------------------

def test_assign_public_username_persists_first_free_candidate():
    # word draw, emoji draw, lock (no fetch), "is it taken?" -> None (free).
    cur = _FakeCursor([("brave",), ("🦉",), None])
    result = assign_public_username(cur, account_id=7)
    assert result == "brave🦉"
    last_sql, last_params = cur.executed[-1]
    assert "UPDATE accounts SET username_adjective" in last_sql
    assert last_params == ("brave", "🦉", 7)


def test_assign_public_username_takes_an_advisory_lock_per_candidate():
    cur = _FakeCursor([("brave",), ("🦉",), None])
    assign_public_username(cur, account_id=7)
    lock_calls = [sql for sql, _ in cur.executed if "pg_advisory_xact_lock" in sql]
    assert len(lock_calls) == 1


def test_assign_public_username_retries_on_taken_candidate():
    # 1st candidate "brave"+owl: SELECT finds a row -> "taken".
    # 2nd candidate "bold"+fox: SELECT finds nothing -> free.
    cur = _FakeCursor([
        ("brave",), ("🦉",), (1,),
        ("bold",), ("🦊",), None,
    ])
    assert assign_public_username(cur, account_id=7) == "bold🦊"


def test_assign_public_username_gives_up_after_max_attempts():
    cur = _FakeCursor([
        ("brave",), ("🦉",), (1,),
        ("bold",), ("🦊",), (1,),
    ])
    with pytest.raises(RuntimeError):
        assign_public_username(cur, account_id=7, max_attempts=2)


# ---------- choose_public_username (explicit rider choice) -----------------

def test_choose_both_halves_explicitly():
    # current (adjective, emoji), adjective-valid check, emoji-valid check.
    cur = _FakeCursor([("brave", "🦉"), (1,), (1,)])
    result = choose_public_username(cur, 7, adjective="bold", emoji="🦊")
    assert result == "bold🦊"
    last_sql, last_params = cur.executed[-1]
    assert "UPDATE accounts SET username_adjective" in last_sql
    assert last_params == ("bold", "🦊", 7)


def test_choose_only_adjective_keeps_current_emoji():
    cur = _FakeCursor([("brave", "🦉"), (1,)])  # current row, adjective-valid check
    result = choose_public_username(cur, 7, adjective="bold", emoji=None)
    assert result == "bold🦉"


def test_choose_only_emoji_keeps_current_adjective():
    cur = _FakeCursor([("brave", "🦉"), (1,)])  # current row, emoji-valid check
    result = choose_public_username(cur, 7, adjective=None, emoji="🦊")
    assert result == "brave🦊"


def test_choose_rejects_adjective_not_in_list():
    cur = _FakeCursor([("brave", "🦉"), None])  # current row, adjective NOT found
    with pytest.raises(InvalidUsernameChoice, match="adjective"):
        choose_public_username(cur, 7, adjective="wobbly", emoji=None)


def test_choose_rejects_emoji_not_in_list():
    cur = _FakeCursor([("brave", "🦉"), None])  # current row, emoji NOT found
    with pytest.raises(InvalidUsernameChoice, match="emoji"):
        choose_public_username(cur, 7, adjective=None, emoji="🍆")


def test_choose_raises_value_error_for_unknown_account():
    cur = _FakeCursor([None])
    with pytest.raises(ValueError):
        choose_public_username(cur, 999, adjective="bold", emoji=None)


def test_choose_with_no_prior_username_requires_both_halves():
    cur = _FakeCursor([(None, None)])
    with pytest.raises(InvalidUsernameChoice, match="provide both"):
        choose_public_username(cur, 7, adjective="bold", emoji=None)


# ---------- phone helpers ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("+13035551234", "+13035551234"),
    (" +1 303-555-1234 ", "+13035551234"),
    ("+1 (303) 555.1234", "+13035551234"),
])
def test_normalize_phone_number_strips_formatting(raw, expected):
    assert normalize_phone_number(raw) == expected


@pytest.mark.parametrize("phone,valid", [
    ("+13035551234", True),
    ("+442071838750", True),
    ("3035551234", False),   # missing +country code
    ("+0123456789", False),  # leading 0 after + is not valid E.164
    ("+1", False),           # too short
    ("not-a-number", False),
])
def test_is_valid_phone_number(phone, valid):
    assert is_valid_phone_number(phone) is valid
