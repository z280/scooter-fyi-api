"""/admin/campaigns/<code>/qr.png|svg — QR artwork for tagged links.

Auth is dependency-overridden and the registry lookup is monkeypatched,
so these run without GitHub OAuth or Postgres. What they pin: a QR is
served only for registered codes, the bytes really are PNG/SVG, and the
encoded payload is exactly the campaign's tagged URL (decoded straight
from the QR matrix via segno's own API — a wrong-URL sticker is the one
mistake print runs can't recover from).
"""

from __future__ import annotations

import io

import segno
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import api_admin, auth, campaigns


def _client(monkeypatch, known={"sticker-2026"}):
    monkeypatch.setattr(
        api_admin.campaigns,
        "get",
        lambda code: {"code": code} if code in known else None,
    )
    monkeypatch.setattr(
        api_admin, "_site_origin", lambda: "https://denver.scooter.fyi"
    )
    app = FastAPI()
    app.include_router(api_admin.router)
    app.dependency_overrides[auth.require_admin] = lambda: {"login": "test"}
    return TestClient(app)


def test_png_serves_image_bytes(monkeypatch):
    r = _client(monkeypatch).get("/admin/campaigns/sticker-2026/qr.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n")
    assert "campaign-sticker-2026-qr.png" in r.headers["content-disposition"]


def test_svg_serves_svg_bytes(monkeypatch):
    r = _client(monkeypatch).get("/admin/campaigns/sticker-2026/qr.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in r.content


def test_qr_encodes_exactly_the_tagged_url(monkeypatch):
    _client(monkeypatch).get("/admin/campaigns/sticker-2026/qr.png")
    # Same inputs the endpoint uses; segno is deterministic, so matching
    # matrices mean matching payloads.
    expected = segno.make(
        "https://denver.scooter.fyi/?utm_campaign=sticker-2026", error="q"
    )
    got = api_admin._campaign_qr("sticker-2026")
    assert [list(row) for row in got.matrix] == [
        list(row) for row in expected.matrix
    ]


def test_unknown_code_gets_not_found_page_not_a_qr(monkeypatch):
    r = _client(monkeypatch).get("/admin/campaigns/nope/qr.png")
    assert "image/png" not in r.headers["content-type"]


def test_scale_is_bounded(monkeypatch):
    client = _client(monkeypatch)
    small = client.get("/admin/campaigns/sticker-2026/qr.png?scale=2")
    big = client.get("/admin/campaigns/sticker-2026/qr.png?scale=40")
    assert len(small.content) < len(big.content)
    assert client.get(
        "/admin/campaigns/sticker-2026/qr.png?scale=9999"
    ).status_code == 422


def test_png_scale_actually_applies(monkeypatch):
    r = _client(monkeypatch).get("/admin/campaigns/sticker-2026/qr.png?scale=3")
    qr = segno.make(
        "https://denver.scooter.fyi/?utm_campaign=sticker-2026", error="q"
    )
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=3, border=4)
    assert r.content == buf.getvalue()
