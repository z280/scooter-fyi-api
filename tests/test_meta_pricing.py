"""GET /api/v1/meta/pricing — the config-driven sales-tax rate (Ride Mode
Screen 8's cost breakdown).

What is worth pinning here is not the number (an operator retunes it when a
ballot measure passes) but the CONTRACT around it: the payload shape the
frontend's `PricingResponse` codes against, that the value really comes from
config.json rather than being frozen in code, that it is cached like the
other published-policy payload, and that a percentage typo cannot ship a
hundredfold tax to a rider.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_meta
from src import config as config_module


@pytest.fixture(autouse=True)
def _clear_pricing_cache(monkeypatch):
    """`_raw_pricing_block` is lru_cached (config.json is read at boot in
    production). Clear it around every test or the first CONFIG_PATH wins.

    `config.load()` is warmed here against the REAL repo config.json on
    purpose: it is lru_cached too, and a test that repoints CONFIG_PATH at a
    minimal fixture file before load()'s first call would blow up on the
    missing `gbfs` block — a test-order-dependent failure.

    config.py DOES now carry a typed `pricing` block, and it wins over the raw
    JSON by design — which would make every CONFIG_PATH fixture below a no-op.
    So the typed path is blanked out here (an object with no `pricing`
    attribute is exactly what `_configured_pricing` treats as "not typed yet",
    the state of a deployment whose config.py lags its config.json).
    `test_a_typed_config_block_wins_over_the_raw_json` puts it back.
    """
    config_module.load()
    monkeypatch.setattr(api_meta, "load", lambda: object())
    api_meta._raw_pricing_block.cache_clear()
    yield
    api_meta._raw_pricing_block.cache_clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api_meta.router)
    return TestClient(app)


def _config_with(monkeypatch, tmp_path, block):
    """Point the raw-config loader at a throwaway config.json whose
    `"pricing"` block is `block` (or which has no such block, for None)."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"pricing": block} if block is not None else {}))
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
    api_meta._raw_pricing_block.cache_clear()


# --- payload shape -----------------------------------------------------------

def test_payload_has_exactly_the_three_contract_keys(client):
    r = client.get("/api/v1/meta/pricing")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"tax_rate", "currency", "as_of"}
    assert isinstance(body["tax_rate"], float)
    assert isinstance(body["currency"], str)
    assert isinstance(body["as_of"], str)


def test_needs_no_bearer_token(client):
    """Public: the wizard fetches this before anyone signs in."""
    assert client.get("/api/v1/meta/pricing").status_code == 200


def test_default_rate_is_a_fraction_not_a_percentage(client):
    """0.0915, never 9.15 — the frontend multiplies by this directly."""
    rate = client.get("/api/v1/meta/pricing").json()["tax_rate"]
    assert 0.0 < rate < 1.0
    # Denver combined: 2.90 state + 1.00 RTD + 0.10 SCFD + 5.15 city.
    assert rate == pytest.approx(0.0915)


def test_default_currency_and_as_of(client):
    body = client.get("/api/v1/meta/pricing").json()
    assert body["currency"] == "USD"
    assert body["as_of"] == "2025-01-01"


def test_cache_header_matches_the_privacy_endpoint(client):
    """Same idiom as /api/v1/meta/privacy — one hour, publicly cacheable."""
    r = client.get("/api/v1/meta/pricing")
    assert r.headers["Cache-Control"] == "public, max-age=3600"
    privacy = client.get("/api/v1/meta/privacy")
    assert r.headers["Cache-Control"] == privacy.headers["Cache-Control"]


# --- config sourcing ---------------------------------------------------------

def test_values_come_from_the_config_block(client, monkeypatch, tmp_path):
    _config_with(monkeypatch, tmp_path,
                 {"tax_rate": 0.0881, "currency": "usd-test",
                  "as_of": "2019-01-01"})
    body = client.get("/api/v1/meta/pricing").json()
    assert body == {"tax_rate": 0.0881, "currency": "usd-test",
                    "as_of": "2019-01-01"}


def test_missing_block_falls_back_to_the_baked_defaults(client, monkeypatch,
                                                        tmp_path):
    """This is also the live repo's state until the integrator lands the
    `pricing` block, so an unconfigured deployment must still serve a
    defensible rate rather than 0 or a 500."""
    _config_with(monkeypatch, tmp_path, None)
    body = client.get("/api/v1/meta/pricing").json()
    assert body == {"tax_rate": api_meta._DEFAULT_TAX_RATE,
                    "currency": "USD", "as_of": "2025-01-01"}


def test_partial_block_fills_only_the_keys_it_carries(client, monkeypatch,
                                                      tmp_path):
    _config_with(monkeypatch, tmp_path, {"tax_rate": 0.05})
    body = client.get("/api/v1/meta/pricing").json()
    assert body == {"tax_rate": 0.05, "currency": "USD",
                    "as_of": "2025-01-01"}


def test_zero_tax_is_a_legitimate_configuration(client, monkeypatch, tmp_path):
    """A jurisdiction with no ride tax must be configurable — 0.0 is a real
    rate, so it cannot be treated as "unset" and replaced by the default."""
    _config_with(monkeypatch, tmp_path, {"tax_rate": 0})
    assert client.get("/api/v1/meta/pricing").json()["tax_rate"] == 0.0


def test_a_percentage_in_config_is_rejected_not_served(client, monkeypatch,
                                                       tmp_path):
    """9.15 where 0.0915 belongs would show a rider ~$46 of tax on a $5 ride.
    It is a config typo with no error of its own, so the loader has to catch
    it."""
    _config_with(monkeypatch, tmp_path, {"tax_rate": 9.15})
    assert client.get("/api/v1/meta/pricing").json()["tax_rate"] == \
        api_meta._DEFAULT_TAX_RATE


def test_a_nonsense_tax_rate_is_rejected_not_served(client, monkeypatch,
                                                    tmp_path):
    for bad in ("not-a-number", -0.01, 1.0, None, [0.09]):
        _config_with(monkeypatch, tmp_path, {"tax_rate": bad})
        assert client.get("/api/v1/meta/pricing").json()["tax_rate"] == \
            api_meta._DEFAULT_TAX_RATE, bad


def test_an_unreadable_config_file_still_serves_defaults(client, monkeypatch,
                                                         tmp_path):
    """Scoped to the raw-JSON read: a missing or malformed config.json logs
    and falls back rather than 500ing this endpoint. (A config.json missing
    at BOOT is a different failure — `config.load()` raises and the app never
    starts, which is every endpoint's problem, not this one's.)"""
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json at all")
    for path in (tmp_path / "does-not-exist.json", malformed):
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
        api_meta._raw_pricing_block.cache_clear()
        body = client.get("/api/v1/meta/pricing").json()
        assert body["tax_rate"] == api_meta._DEFAULT_TAX_RATE, path


def test_a_typed_config_block_wins_over_the_raw_json(client, monkeypatch,
                                                     tmp_path):
    """Forward compatibility: when config.py grows a typed `pricing` block
    (the integrator's call), the endpoint reads it and does not re-parse
    config.json. Mirrors src/api_geocode.py:geocode_settings."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"pricing": {"tax_rate": 0.01}}))
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(path))
    api_meta._raw_pricing_block.cache_clear()

    class _Typed:
        pricing = type("P", (), {"tax_rate": 0.0777, "currency": "CAD",
                                 "as_of": "2030-06-01"})()

    monkeypatch.setattr(api_meta, "load", lambda: _Typed())
    assert client.get("/api/v1/meta/pricing").json() == {
        "tax_rate": 0.0777, "currency": "CAD", "as_of": "2030-06-01"}
