"""Every sql/ migration gets a number of its own.

src/pg.py:run_migrations applies `sorted(SQL_DIR.glob("*.sql"))` and keys
schema_migrations on the FILENAME, not the number, so two files sharing a
prefix do not collide at runtime: both run, ordered by the rest of the
name. The damage is subtler. "sql/061" stops identifying one change; the
order between the two is decided by the alphabet, not by which depends on
which; and it happens precisely when two branches are written in parallel
-- which is when nobody is watching the number.

No database is needed; this is a directory listing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

# The two pairs that shipped before this guard existed. They stay, and they
# stay exactly as named: schema_migrations is keyed on filename, so renaming
# either half would make it a NEW migration to every database that already
# recorded the old name -- production included -- and it would run a second
# time there. Every file here is already applied in production.
#
# Nothing new belongs on this list. A new duplicate means renumbering the
# file that has NOT merged yet, while no database has recorded its name.
_HISTORICAL_DUPLICATES: dict[int, frozenset[str]] = {
    61: frozenset({"061_area_leaders_live.sql", "061_telemetry.sql"}),
    69: frozenset({"069_device_status_snapshots.sql", "069_rental_aware_trip_detection.sql"}),
}

# Three digits, zero-padded, so the lexicographic sort run_migrations does
# is also numeric order.
_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def _migrations() -> list[str]:
    return sorted(p.name for p in SQL_DIR.glob("*.sql"))


def test_every_migration_is_named_nnn_description():
    bad = [n for n in _migrations() if not _NAME.match(n)]
    assert not bad, (
        f"{bad}: name migrations NNN_lower_snake.sql -- a three-digit, "
        "zero-padded prefix keeps string order equal to numeric order"
    )


def test_no_two_migrations_share_a_number():
    by_number: dict[int, set[str]] = defaultdict(set)
    for name in _migrations():
        m = _NAME.match(name)
        if m:
            by_number[int(m.group(1))].add(name)

    unexpected = {
        n: sorted(names)
        for n, names in by_number.items()
        if len(names) > 1 and names != _HISTORICAL_DUPLICATES.get(n)
    }
    next_free = max(by_number) + 1
    assert not unexpected, (
        f"migration number(s) reused: {unexpected}. Renumber the file that "
        f"has not merged yet -- the next free number is {next_free:03d}. Do "
        "NOT rename one that has shipped: schema_migrations is keyed on "
        "filename, so a rename re-runs it everywhere it already ran."
    )


def test_the_historical_duplicates_are_still_exactly_as_shipped():
    """The allowlist is for two specific, already-applied pairs. If one of
    them has been renamed, that rename is the bug (it would re-run in
    production); if the list has gone stale some other way, it should be
    trimmed rather than left to excuse a future collision."""
    names = set(_migrations())
    for n, pair in _HISTORICAL_DUPLICATES.items():
        assert pair <= names, f"sql/{n:03d}: expected {sorted(pair)} to be untouched"
