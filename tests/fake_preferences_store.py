"""An in-memory `user_preferences` for the fake-cursor preference tests.

Lifted out of tests/test_ride_usuals.py when ride specs (sql/080) became the
THIRD named kind sharing that table. Shared rather than copied for the reason
the fake exists at all: the property worth testing is WHICH ROWS EACH ENDPOINT
CAN SEE, and three families of near-identical handlers are kept apart only by
`kind` — in every WHERE clause, in the ON CONFLICT arbiter predicate, and in
each kind's partial unique index. A second copy of this model could drift from
the first, and a drifted fake is one that stops being able to fail the
copy-paste mistake it was written to catch.

Not a test module: it defines no tests and pytest will not collect it. The
fixtures that wire it to `src.api_preferences.connection` stay in the test
files, because which router and which user they mount is their business.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

#: Fixed clock the stores hang their monotonic timestamps off.
EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# An in-memory user_preferences, keyed the way the real table is
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self) -> None:
        # (account_id, kind, name) -> {settings, created_at, updated_at}
        self.rows: dict[tuple[int, str, str | None], dict] = {}
        self.account_exists = True
        self.executed: list[tuple[str, tuple]] = []
        self._tick = 0

    def now(self) -> datetime:
        # Monotonic, so ORDER BY updated_at DESC is deterministic instead of
        # depending on whether two writes landed in the same microsecond.
        self._tick += 1
        return EPOCH + timedelta(seconds=self._tick)

    def seed(self, kind: str, name: str, settings: dict, *, account_id: int = 1) -> None:
        stamp = self.now()
        self.rows[(account_id, kind, name)] = {
            "settings": settings, "created_at": stamp, "updated_at": stamp,
        }

    def names(self, kind: str, *, account_id: int = 1) -> set[str | None]:
        return {n for (a, k, n) in self.rows if a == account_id and k == kind}


class FakeCursor:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self._result: list[tuple] = []
        self.rowcount = 0

    # -- psycopg surface ----------------------------------------------------
    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        params = tuple(params)
        self._store.executed.append((s, params))
        self._result = []
        self.rowcount = 0

        if s.startswith("SELECT 1 FROM accounts"):
            self._result = [(1,)] if self._store.account_exists else []
            return

        if s.startswith("SELECT COUNT(*) FROM user_preferences"):
            account_id, kind, name = params
            self._result = [(sum(
                1 for (a, k, n) in self._store.rows
                if a == account_id and k == kind and n != name
            ),)]
            return

        if s.startswith("SELECT name, settings, created_at, updated_at"):
            if "ORDER BY updated_at DESC" in s:          # a list read
                account_id, kind = params
                hits = [
                    (n, row) for (a, k, n), row in self._store.rows.items()
                    if a == account_id and k == kind
                ]
                hits.sort(key=lambda pair: pair[1]["updated_at"], reverse=True)
                self._result = [
                    (n, row["settings"], row["created_at"], row["updated_at"])
                    for n, row in hits
                ]
                return
            if len(params) == 3:                          # one, by name
                account_id, kind, name = params
                row = self._store.rows.get((account_id, kind, name))
                if row is not None:
                    self._result = [
                        (name, row["settings"], row["created_at"], row["updated_at"])
                    ]
                return
            account_id, kind = params                     # the unnamed kind
            row = self._store.rows.get((account_id, kind, None))
            if row is not None:
                self._result = [(None, row["settings"], row["created_at"], row["updated_at"])]
            return

        if s.startswith("INSERT INTO user_preferences"):
            account_id, kind, name, blob = params
            # THE ON CONFLICT ARBITER MUST NAME THIS KIND'S INDEX. A handler
            # that bound kind='ride_mode_usual' while inferring the
            # saved_map_settings index would upsert against the wrong
            # uniqueness rule — the exact copy-paste failure, and invisible
            # to a fake that only looked at the bound parameters.
            arbiter = re.search(r"ON CONFLICT \(account_id, name\) WHERE kind = '(\w+)'", s)
            assert arbiter, f"upsert has no partial-index arbiter: {s}"
            assert arbiter.group(1) == kind, (
                f"upsert binds kind={kind!r} but arbitrates on "
                f"{arbiter.group(1)!r}'s unique index"
            )
            stamp = self._store.now()
            row = self._store.rows.get((account_id, kind, name))
            if row is None:
                row = {"settings": None, "created_at": stamp, "updated_at": stamp}
                self._store.rows[(account_id, kind, name)] = row
            row["settings"] = json.loads(blob)
            row["updated_at"] = stamp
            self._result = [(name, row["settings"], row["created_at"], row["updated_at"])]
            return

        if s.startswith("DELETE FROM user_preferences"):
            account_id, kind, name = params
            self.rowcount = 1 if self._store.rows.pop((account_id, kind, name), None) else 0
            return

        raise AssertionError(f"unexpected SQL reached the fake cursor: {s}")


class FakeConn:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.commits = 0

    def cursor(self):
        return FakeCursor(self._store)

    def commit(self):
        self.commits += 1

