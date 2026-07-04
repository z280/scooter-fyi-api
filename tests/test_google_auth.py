"""Google ID token verification — signed with a locally generated RSA key,
JWKS fetch monkeypatched out."""

from __future__ import annotations

import json
import time

import pytest
from authlib.jose import JsonWebKey, jwt as authlib_jwt

from src import google_auth
from src.google_auth import GoogleAuthError, verify_google_id_token

AUD = "test-client-id.apps.googleusercontent.com"

_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)
_KEY_DICT = json.loads(_KEY.as_json(is_private=False))
_KEY_DICT["kid"] = "test-kid"
_PUBLIC_SET = JsonWebKey.import_key_set({"keys": [_KEY_DICT]})


def _sign(claims: dict) -> str:
    token = authlib_jwt.encode({"alg": "RS256", "kid": "test-kid"}, claims, _KEY)
    return token.decode() if isinstance(token, bytes) else token


def _claims(**overrides) -> dict:
    now = int(time.time())
    base = {
        "iss": "https://accounts.google.com",
        "aud": AUD,
        "sub": "1234567890",
        "email": "zneill@gmail.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _fake_jwks(monkeypatch):
    monkeypatch.setattr(google_auth, "_key_set", lambda force_refresh=False: _PUBLIC_SET)


def test_valid_token_returns_claims():
    out = verify_google_id_token(_sign(_claims()), AUD)
    assert out["email"] == "zneill@gmail.com"


def test_bare_issuer_variant_accepted():
    out = verify_google_id_token(_sign(_claims(iss="accounts.google.com")), AUD)
    assert out["email"] == "zneill@gmail.com"


def test_wrong_audience_rejected():
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(_sign(_claims(aud="someone-else")), AUD)


def test_wrong_issuer_rejected():
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(_sign(_claims(iss="https://evil.example")), AUD)


def test_expired_token_rejected():
    past = int(time.time()) - 7200
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(_sign(_claims(iat=past, exp=past + 60)), AUD)


def test_unverified_email_rejected():
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(_sign(_claims(email_verified=False)), AUD)


def test_missing_email_rejected():
    c = _claims()
    del c["email"]
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(_sign(c), AUD)


def test_tampered_signature_rejected():
    tok = _sign(_claims())
    header, payload, sig = tok.split(".")
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(f"{header}.{payload}.{sig[:-4]}AAAA", AUD)


def test_no_audience_configured_rejected():
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(_sign(_claims()), "")
