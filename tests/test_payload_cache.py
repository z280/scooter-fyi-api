"""src/payload_cache.py: the precomputed map-payload cache.

The gzip splice is the part that must be exactly right (a browser rejects
the whole body otherwise), so it is checked against Python's own decoder
for every shape the routes produce. The cache behaviour — build once per
(cycle, stamp), serve the previous entry while a rebuild runs, never serve
an entry past MAX_STALE_SECONDS — is checked with the table stubbed out.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import zlib

import pytest

from src import payload_cache as pc


@pytest.fixture
def cache(monkeypatch):
    """The cache ON, with an in-memory stand-in for the UNLOGGED table."""
    table: dict[str, pc.Entry] = {}
    monkeypatch.setattr(pc, "ENABLED", True)
    monkeypatch.setattr(pc, "_load", lambda key: table.get(key))
    monkeypatch.setattr(pc, "_store", lambda e: table.__setitem__(e.key, e))
    pc.clear()
    yield table
    pc.clear()


# ---------- the gzip splice ---------------------------------------------------
@pytest.mark.parametrize("prefix, suffix", [
    (b'{"a":1', b"}"),
    (b"", b""),
    (b"x" * 200_000, b',"metadata":{"viewed_by":"r@example.com"}}'),
    ('{"name":"Lunar 🐸"'.encode(), b"}"),
])
def test_assembled_stream_is_valid_gzip(prefix, suffix):
    entry = pc.make_entry("k", "c", "s", prefix)
    gz = pc.assemble(entry, suffix)
    assert gzip.decompress(gz) == prefix + suffix
    # zlib in gzip mode is stricter about the trailer (CRC and length).
    assert zlib.decompress(gz, 31) == prefix + suffix


def test_one_entry_serves_different_suffixes():
    """The point of the open prefix: one stored body, per-caller metadata."""
    entry = pc.make_entry("k", "c", "s", b'{"features":[1,2,3]')
    a = pc.assemble(entry, b',"metadata":{"viewed_by":"a"}}')
    b = pc.assemble(entry, b',"metadata":{"viewed_by":"b"}}')
    assert json.loads(gzip.decompress(a))["metadata"]["viewed_by"] == "a"
    assert json.loads(gzip.decompress(b))["metadata"]["viewed_by"] == "b"


def test_client_without_gzip_gets_plain_json():
    gz = pc.assemble(pc.make_entry("k", "c", "s", b'{"x":1'), b"}")
    body, headers = pc.gzip_response_body(gz, None)
    assert body == b'{"x":1}' and "Content-Encoding" not in headers
    body, headers = pc.gzip_response_body(gz, "br, gzip")
    assert body == gz and headers["Content-Encoding"] == "gzip"


def test_dumps_matches_starlette_json():
    from starlette.responses import JSONResponse

    obj = {"a": [1, 2.5, None, True], "é": "🐸", "n": {"x": "y"}}
    assert pc.dumps(obj) == JSONResponse(obj).body


# ---------- build once, per cycle and stamp -----------------------------------
def _builder(calls, cycle="c1", stamp="s1", body=b"{"):
    def build():
        calls.append(cycle)
        return pc.make_entry("devices|x", cycle, stamp, body)
    return build


def test_built_once_per_cycle_and_stamp(cache):
    calls = []
    for _ in range(3):
        pc.get_or_build("devices|x", "c1", "s1", _builder(calls))
    assert calls == ["c1"]
    pc.get_or_build("devices|x", "c1", "s2", _builder(calls, stamp="s2"))  # a report landed
    pc.get_or_build("devices|x", "c2", "s2", _builder(calls, "c2", "s2"))  # a cycle landed
    assert calls == ["c1", "c1", "c2"]


def test_restarted_worker_reads_the_table(cache):
    calls = []
    pc.get_or_build("devices|x", "c1", "s1", _builder(calls))
    pc.clear()                                  # a fresh process: memory empty
    pc.get_or_build("devices|x", "c1", "s1", _builder(calls))
    assert calls == ["c1"], "the stored entry must be reused, not rebuilt"


def test_previous_entry_served_while_rebuilding(cache):
    pc.get_or_build("devices|x", "c1", "s1", _builder([]))
    started, release = threading.Event(), threading.Event()

    def slow_build():
        started.set()
        release.wait(5)
        return pc.make_entry("devices|x", "c2", "s1", b"{")

    t = threading.Thread(target=pc.get_or_build,
                         args=("devices|x", "c2", "s1", slow_build))
    t.start()
    assert started.wait(5)
    # Another request for the new cycle does not wait and does not build: it
    # gets the c1 entry, whose own cycle the route then puts in the ETag.
    got = pc.get_or_build("devices|x", "c2", "s1", _builder([], "c2"))
    assert got.cycle_id == "c1"
    release.set()
    t.join(5)
    assert pc.get_or_build("devices|x", "c2", "s1", _builder([])).cycle_id == "c2"


def test_too_old_entry_is_not_served_stale(cache, monkeypatch):
    old = pc.make_entry("devices|x", "c1", "s1", b"{")
    old.built_at = time.time() - pc.MAX_STALE_SECONDS - 1
    pc._memo["devices|x"] = old
    lock = pc._lock_for("devices|x")
    lock.acquire()                              # someone else is building
    out: list[pc.Entry] = []
    t = threading.Thread(target=lambda: out.append(
        pc.get_or_build("devices|x", "c2", "s1", _builder([], "c2"))))
    t.start()
    time.sleep(0.2)
    assert not out, "an over-age entry must make the caller wait, not be served"
    lock.release()
    t.join(5)
    assert out[0].cycle_id == "c2"


def test_disabled_cache_always_builds(monkeypatch):
    monkeypatch.setattr(pc, "ENABLED", False)
    calls = []
    pc.get_or_build("devices|x", "c1", "s1", _builder(calls))
    pc.get_or_build("devices|x", "c1", "s1", _builder(calls))
    assert calls == ["c1", "c1"]


def test_warmer_failure_is_contained(cache, monkeypatch):
    ran = []
    monkeypatch.setattr(pc, "_warmers", [lambda: 1 / 0, lambda: ran.append(1)])
    pc.warm_once()
    assert ran == [1]


# ---------- the devices route uses it -----------------------------------------
def test_public_and_signed_in_non_admin_share_one_entry(cache, monkeypatch):
    """A non-admin's signed-in feed differs from the public one only in
    metadata, so both are served from ONE build; an admin's (plate-bearing)
    feed is a separate entry and the public one never carries a plate."""
    from tests import test_api_devices_payload as t
    from src import api_public

    builds = []
    real = api_public._build_device_features

    def counting(*a, **k):
        builds.append(1)
        return real(*a, **k)

    @__import__("contextlib").contextmanager
    def _conn():
        yield t._FakeConn()

    monkeypatch.setattr(api_public, "connection", _conn)
    monkeypatch.setattr(api_public, "stats_for_cycle", lambda c, s: {})
    monkeypatch.setattr(api_public, "_negative_states", lambda c: {})
    monkeypatch.setattr(api_public, "_build_device_features", counting)
    from fastapi import Response
    from tests.payload_json import decoded

    kw = dict(form_factor=None, spatial_status=None, include_outliers=False,
              bbox=None, include=None)
    public = decoded(api_public._devices_current_impl(t._request(), Response(), **kw))
    rider = decoded(api_public._devices_current_impl(
        t._request(), Response(), **kw, resource="user-devices",
        viewed_by="rider@example.com"))
    assert builds == [1]
    assert rider["metadata"]["viewed_by"] == "rider@example.com"
    assert "viewed_by" not in public["metadata"]
    assert rider["features"] == public["features"]

    admin = decoded(api_public._devices_current_impl(
        t._request(), Response(), **kw, include_plate=True,
        resource="user-devices", viewed_by="z@neill.io"))
    assert builds == [1, 1]
    assert "vehicle_plate" in admin["features"][0]["properties"]
    assert "vehicle_plate" not in public["features"][0]["properties"]
