"""scripts/check_migration_numbers.py — the pure parts.

The git and API halves are exercised by running the script; these cover the
decision logic, which is where a wrong answer would be silent. The named case
is the real one: PR #105 added sql/088_discount_reports_equity_areas.sql while
main already carried sql/088_standardise_movement_radius.sql.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_migration_numbers",
    Path(__file__).resolve().parent.parent / "scripts" / "check_migration_numbers.py",
)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


def test_it_groups_by_number_and_ignores_unnumbered_files():
    g = mod.numbered([
        "sql/001_init.sql",
        "sql/088_standardise_movement_radius.sql",
        "sql/088_discount_reports_equity_areas.sql",
        "sql/README.md",          # not a migration
        "sql/notes.sql",          # no number
        "src/pg.py",              # not even in sql/
    ])
    assert g["088"] == [
        "sql/088_standardise_movement_radius.sql",
        "sql/088_discount_reports_equity_areas.sql",
    ]
    assert set(g) == {"001", "088"}


def test_the_105_case_is_a_collision():
    g = mod.numbered([
        "sql/088_standardise_movement_radius.sql",
        "sql/088_discount_reports_equity_areas.sql",
    ])
    assert "088" in mod.collisions(g)


def test_one_file_per_number_is_not_a_collision():
    g = mod.numbered(["sql/087_a.sql", "sql/088_b.sql", "sql/089_c.sql"])
    assert mod.collisions(g) == {}


def test_the_same_path_listed_twice_is_not_a_collision():
    # HEAD and the base branch both contain most migrations; the union would
    # otherwise report every unchanged file as colliding with itself.
    g = mod.numbered(["sql/088_x.sql", "sql/088_x.sql"])
    assert mod.collisions(g) == {}


def test_next_free_is_one_past_the_highest():
    g = mod.numbered(["sql/001_a.sql", "sql/088_b.sql"])
    assert mod.next_free(g) == "089"
    # ...and zero-padded, because the filenames are.
    assert mod.next_free(mod.numbered(["sql/008_a.sql"])) == "009"


def test_next_free_on_an_empty_tree():
    assert mod.next_free({}) == "001"


def test_three_way_collision_lists_every_claimant():
    g = mod.numbered(["sql/088_a.sql", "sql/088_b.sql", "sql/088_c.sql"])
    assert len(mod.collisions(g)["088"]) == 3


def test_numbers_are_compared_as_written_not_as_ints():
    # "088" and "88_" are different filenames; only the three-digit form is a
    # migration, so a stray two-digit file must not merge into 088's group.
    g = mod.numbered(["sql/088_a.sql", "sql/88_b.sql"])
    assert mod.collisions(g) == {}
