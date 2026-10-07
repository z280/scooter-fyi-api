"""sql/091: missed-discount reports against the official Equity Area map.

The frontend's equity card submits zone_version 'equity' plus the area it
ended in. Reuses the fake DB of test_discount_report_upload_order.py.
"""

from __future__ import annotations

from tests.test_discount_report_upload_order import _NOW, ctx  # noqa: F401  (fixture)


def _insert(state) -> tuple:
    rows = [p for sql, p in state["sql"] if sql.startswith("INSERT INTO discount_reports")]
    assert len(rows) == 1, state["sql"]
    return rows[0]


def test_equity_report_with_area_and_receipt_is_stored(ctx):
    client, state = ctx
    r = client.post(
        "/api/v1/reports/discount",
        data={"ride_ended_at": _NOW.isoformat(), "zone_version": "equity",
              "region_name": "EQ_014", "end_lat": "39.785", "end_lng": "-104.826",
              "amount_charged_cents": "612"},
        files={"receipt": ("r.jpg", b"\xff\xd8\xff-x", "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    params = _insert(state)
    assert params[2] == "equity" and params[3] == "EQ_014"
    assert params[6] == 612 and params[7] == "receipt.jpg"


def test_region_name_is_optional(ctx):
    client, state = ctx
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "equity"})
    assert r.status_code == 200, r.text
    assert _insert(state)[3] is None


def test_region_name_must_look_like_an_equity_area(ctx):
    client, _ = ctx
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "equity",
        "region_name": "Five Points'; drop table x"})
    assert r.status_code == 422


def test_unknown_zone_versions_are_still_rejected(ctx):
    client, _ = ctx
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "v3"})
    assert r.status_code == 422


def test_a_non_ascii_region_name_is_refused_by_the_API_not_the_database(ctx):
    """Regression: `\\d` is Unicode-aware, `[0-9]` is not.

    Both Python's `re` and the Rust engine Pydantic uses read `\\d` as any
    Unicode decimal digit, so `EQ_٠١٤` (Arabic-Indic) satisfied the field while
    sql/091's `~ '^EQ_[0-9]{3}$'` refused it. The insert then raised a
    CheckViolation, and this endpoint's `except Exception: ... raise` turned a
    malformed field into a 500 rather than a validation response. #117 made the
    receipt-claim path ASCII-only and missed this legacy field.

    The assertion that matters is the SECOND one: refused BEFORE the database,
    so nothing reaches an insert that was always going to fail.
    """
    client, state = ctx
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "equity",
        "region_name": "EQ_٠١٤"})
    assert r.status_code == 422, r.text
    assert not [sql for sql, _ in state["sql"] if sql.startswith("INSERT INTO discount_reports")]


def test_an_ascii_region_name_still_works(ctx):
    """The guard above must not have narrowed the field to nothing."""
    client, state = ctx
    r = client.post("/api/v1/reports/discount", json={
        "ride_ended_at": _NOW.isoformat(), "zone_version": "equity",
        "region_name": "EQ_014"})
    assert r.status_code == 200, r.text
    assert _insert(state)[3] == "EQ_014"
