"""device_photos.public_photo_url — how a stored photo becomes a URL a
browser will actually render.

The regression this pins: with r2.public_base_url unset (its shipped state),
this returned None for every photo, and a null `photo_url` is unrenderable —
our own client refuses to put a non-http(s) value in an <img src> and tells
the rider the photo "couldn't be displayed safely." Uploads had worked fine;
only the addressing was missing.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src import device_photos

_KEY = "device-photos/7/abc.jpg"


class _FakeR2:
    """Stands in for the boto3 client — presigning is the only call under
    test, and no existing test in this codebase mocks boto3 itself."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803 (boto3's spelling)
        self.calls.append((op, {"Params": Params, "ExpiresIn": ExpiresIn}))
        return f"https://acct.r2.cloudflarestorage.com/{Params['Bucket']}/{Params['Key']}?sig=x"


@pytest.fixture
def r2(monkeypatch):
    fake = _FakeR2()
    monkeypatch.setattr(device_photos, "device_photos_bucket", lambda: "devbucket")
    monkeypatch.setattr(device_photos, "_r2_client", lambda: fake)
    return fake


def _with_public_base(monkeypatch, base):
    cfg = device_photos.load()
    monkeypatch.setattr(
        device_photos, "load",
        lambda: replace(cfg, r2=replace(cfg.r2, public_base_url=base)),
    )


def test_uses_public_base_url_when_configured(monkeypatch, r2):
    _with_public_base(monkeypatch, "https://photos.scooter.fyi")
    assert device_photos.public_photo_url(_KEY) == f"https://photos.scooter.fyi/{_KEY}"
    assert r2.calls == []  # a static URL costs nothing — don't sign one


def test_public_base_url_trailing_slash_does_not_double(monkeypatch, r2):
    _with_public_base(monkeypatch, "https://photos.scooter.fyi/")
    assert device_photos.public_photo_url(_KEY) == f"https://photos.scooter.fyi/{_KEY}"


def test_falls_back_to_a_presigned_url(monkeypatch, r2):
    _with_public_base(monkeypatch, None)
    url = device_photos.public_photo_url(_KEY)
    assert url.startswith("https://")  # the only shape a client will render
    assert _KEY in url
    (op, kw), = r2.calls
    assert op == "get_object"
    assert kw["Params"] == {"Bucket": "devbucket", "Key": _KEY}
    assert kw["ExpiresIn"] == device_photos.PRESIGNED_TTL_SECONDS


def test_none_only_when_r2_is_unconfigured(monkeypatch):
    """No credentials means no bucket, no upload, and nothing to point at —
    the one case where a null photo_url is the honest answer."""
    _with_public_base(monkeypatch, None)
    monkeypatch.setattr(device_photos, "device_photos_bucket", lambda: None)
    assert device_photos.public_photo_url(_KEY) is None


def test_client_is_reused_across_photos(monkeypatch):
    """A listing signs one URL per row; each must not build its own client."""
    built: list[str] = []
    monkeypatch.setattr(device_photos.boto3, "client",
                        lambda *a, **kw: built.append(kw["endpoint_url"]) or _FakeR2())
    monkeypatch.setattr(
        device_photos, "r2_credentials",
        lambda: {"account_id": "acct", "access_key_id": "ak",
                 "secret_access_key": "sk", "bucket": "devbucket"},
    )
    device_photos._client.cache_clear()
    try:
        for _ in range(3):
            device_photos._r2_client()
        assert len(built) == 1
    finally:
        device_photos._client.cache_clear()
