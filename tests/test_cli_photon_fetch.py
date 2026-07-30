"""`python -m src.cli fetch_photon_index` / `refresh_photon_index` — the
ETag-gated Photon index sync in src/r2_map.py.

The R2 layer is faked at `r2_map._client` (a boto3 client object is a duck, and
nothing in this codebase mocks boto3 itself), and the unpack step is stubbed by
default so these run without `zstandard` installed. The two tests that exercise
the real `.tar.zst` unpack are gated on the package being present, so they run
in CI/prod and skip in a bare container.

What matters here is the no-op path: this runs as a one-shot sidecar on every
`docker compose up` AND from cron at 05:00, so an unchanged index must cost one
LIST and nothing else — and a FAILED install must never be remembered as done.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from src import r2_map

_BUCKET = "denver-street-optimized-data"
_KEY_OLD = "photon/photon-index-20260401.tar.zst"
_KEY_NEW = "photon/photon-index-20260701.tar.zst"


class _FakePaginator:
    def __init__(self, objects):
        self._objects = objects
        self.prefixes: list[str] = []

    def paginate(self, Bucket=None, Prefix=None):  # noqa: N803 — boto3 kwargs
        self.prefixes.append(Prefix)
        # Two pages, to prove the newest key is chosen across pages rather than
        # from the first one that matches.
        contents = [{"Key": k, "ETag": f'"{e}"'} for k, e in self._objects]
        yield {"Contents": contents[:1]}
        yield {"Contents": contents[1:]}


class _FakeClient:
    def __init__(self, objects, download_error=None):
        self.objects = objects
        self.download_error = download_error
        self.downloads: list[tuple[str, str]] = []
        self.paginator = _FakePaginator(objects)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self.paginator

    def download_file(self, bucket, key, path):
        self.downloads.append((key, path))
        if self.download_error is not None:
            raise self.download_error
        Path(path).write_bytes(b"\x28\xb5\x2f\xfd-not-really-zstd")


def _install(monkeypatch, tmp_path, objects, *, download_error=None,
             unpack_error=None, creds=True, unpack=True):
    """Point r2_map at `tmp_path` with a faked R2 client. Returns the client."""
    client = _FakeClient(objects, download_error=download_error)
    monkeypatch.setattr(r2_map, "PHOTON_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(
        r2_map, "r2_map_credentials",
        (lambda: {"account_id": "acct", "access_key_id": "k",
                  "secret_access_key": "s", "bucket": _BUCKET})
        if creds else (lambda: None))
    monkeypatch.setattr(r2_map, "_client", lambda c: client)

    if unpack:
        def fake_unpack(archive, dest_dir):
            assert Path(archive).exists(), "unpack ran before the download landed"
            if unpack_error is not None:
                raise unpack_error
            (Path(dest_dir) / r2_map.PHOTON_DATA_DIRNAME).mkdir(exist_ok=True)
            (Path(dest_dir) / r2_map.PHOTON_DATA_DIRNAME / "node.lock").touch()

        monkeypatch.setattr(r2_map, "_unpack_photon_index", fake_unpack)
    return client


def _marker(tmp_path: Path) -> str:
    return (tmp_path / r2_map.PHOTON_MARKER_NAME).read_text()


# --- first install -----------------------------------------------------------

def test_first_run_downloads_unpacks_and_records_the_etag(monkeypatch, tmp_path):
    client = _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    result = r2_map.sync_photon_index()

    assert result["changed"] is True
    assert result["key"] == _KEY_NEW
    assert result["index_present"] is True
    assert result["errors"] == []
    assert [k for k, _ in client.downloads] == [_KEY_NEW]
    assert (tmp_path / r2_map.PHOTON_DATA_DIRNAME).is_dir()
    assert _marker(tmp_path) == f"{_KEY_NEW}\nabc123\n"
    assert client.paginator.prefixes == ["photon/"]


def test_the_downloaded_tarball_is_not_left_on_the_volume(monkeypatch, tmp_path):
    """It is a multi-GB transient next to a multi-GB index."""
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    r2_map.sync_photon_index()
    assert list(tmp_path.glob("*.tar.zst*")) == []


# --- the no-op path ----------------------------------------------------------

def test_unchanged_etag_is_a_no_op(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    r2_map.sync_photon_index()

    client = _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    result = r2_map.sync_photon_index()
    assert result["changed"] is False
    assert result["key"] == _KEY_NEW
    assert result["index_present"] is True
    assert client.downloads == []


def test_a_new_etag_on_the_same_key_refreshes(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    r2_map.sync_photon_index()

    client = _install(monkeypatch, tmp_path, [(_KEY_NEW, "def456")])
    result = r2_map.sync_photon_index()
    assert result["changed"] is True
    assert [k for k, _ in client.downloads] == [_KEY_NEW]
    assert _marker(tmp_path) == f"{_KEY_NEW}\ndef456\n"


def test_a_newer_dated_key_refreshes(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, [(_KEY_OLD, "old")])
    r2_map.sync_photon_index()

    client = _install(monkeypatch, tmp_path, [(_KEY_OLD, "old"), (_KEY_NEW, "new")])
    result = r2_map.sync_photon_index()
    assert result["changed"] is True
    assert result["key"] == _KEY_NEW
    assert [k for k, _ in client.downloads] == [_KEY_NEW]


def test_newest_is_chosen_by_date_in_the_name_not_by_listing_order(monkeypatch, tmp_path):
    """A re-upload of an old index bumps its LastModified but must not win."""
    client = _install(monkeypatch, tmp_path,
                      [(_KEY_NEW, "new"), (_KEY_OLD, "old")])
    assert r2_map.sync_photon_index()["key"] == _KEY_NEW
    assert [k for k, _ in client.downloads] == [_KEY_NEW]


def test_a_wiped_volume_refetches_even_with_a_matching_marker(monkeypatch, tmp_path):
    """The marker alone is not evidence the index is still there — a recreated
    volume (or a hand-deleted photon_data) has to be repopulated."""
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    r2_map.sync_photon_index()
    (tmp_path / r2_map.PHOTON_DATA_DIRNAME / "node.lock").unlink()
    (tmp_path / r2_map.PHOTON_DATA_DIRNAME).rmdir()

    client = _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    assert r2_map.sync_photon_index()["changed"] is True
    assert [k for k, _ in client.downloads] == [_KEY_NEW]


def test_non_index_objects_under_the_prefix_are_ignored(monkeypatch, tmp_path):
    client = _install(monkeypatch, tmp_path, [
        ("photon/README.txt", "e1"),
        ("photon/photon-index-2026.tar.zst", "e2"),      # wrong stamp width
        ("photon/photon-index-20260701.tar.gz", "e3"),   # wrong compression
        (_KEY_NEW, "good"),
    ])
    assert r2_map.sync_photon_index()["key"] == _KEY_NEW
    assert [k for k, _ in client.downloads] == [_KEY_NEW]


# --- failure paths (must not be remembered as success) -----------------------

def test_no_index_object_at_all_is_a_clean_no_op(monkeypatch, tmp_path):
    client = _install(monkeypatch, tmp_path, [("photon/README.txt", "e1")])
    result = r2_map.sync_photon_index()
    assert result == {"changed": False, "key": None, "dir": str(tmp_path),
                      "index_present": False, "errors": []}
    assert client.downloads == []
    assert not (tmp_path / r2_map.PHOTON_MARKER_NAME).exists()


def test_missing_credentials_skip_without_touching_r2(monkeypatch, tmp_path):
    """The sidecar must still exit 0 — `photon` gates on
    service_completed_successfully and the deploy runs under `set -e`."""
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")], creds=False)
    result = r2_map.sync_photon_index()
    assert result["changed"] is False
    assert result["errors"] == []


def test_a_failed_download_leaves_no_marker_and_no_partial(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")],
             download_error=OSError("connection reset"))
    result = r2_map.sync_photon_index()
    assert result["changed"] is False
    assert result["errors"] and "connection reset" in result["errors"][0]
    assert not (tmp_path / r2_map.PHOTON_MARKER_NAME).exists()
    assert list(tmp_path.glob("*.part")) == []


def test_a_failed_unpack_is_retried_next_run(monkeypatch, tmp_path):
    """The marker is written AFTER the swap for exactly this reason: an ETag
    recorded on download would make a corrupt archive permanent."""
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")],
             unpack_error=RuntimeError("zstd: unexpected end of input"))
    result = r2_map.sync_photon_index()
    assert result["changed"] is False
    assert result["errors"] and "unexpected end of input" in result["errors"][0]
    assert not (tmp_path / r2_map.PHOTON_MARKER_NAME).exists()
    assert list(tmp_path.glob("*.part")) == []

    client = _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])
    assert r2_map.sync_photon_index()["changed"] is True
    assert [k for k, _ in client.downloads] == [_KEY_NEW]


def test_a_listing_failure_is_reported_not_raised(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, [(_KEY_NEW, "abc123")])

    def boom(client, bucket):
        raise OSError("403 forbidden")

    monkeypatch.setattr(r2_map, "_newest_photon_index", boom)
    result = r2_map.sync_photon_index()
    assert result["changed"] is False
    assert result["errors"] == ["403 forbidden"]


# --- the real unpack (needs zstandard) ---------------------------------------

def _write_archive(path: Path, top_level: str) -> None:
    zstandard = pytest.importorskip("zstandard")
    payload = path.parent / "payload"
    (payload / top_level).mkdir(parents=True)
    (payload / top_level / "node.lock").write_text("x")

    tar_path = path.parent / "payload.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(payload / top_level, arcname=top_level)
    cctx = zstandard.ZstdCompressor()
    path.write_bytes(cctx.compress(tar_path.read_bytes()))


def test_real_unpack_swaps_photon_data_into_place(tmp_path):
    pytest.importorskip("zstandard")
    dest = tmp_path / "volume"
    dest.mkdir()
    stale = dest / r2_map.PHOTON_DATA_DIRNAME
    stale.mkdir()
    (stale / "stale.marker").write_text("old index")

    archive = tmp_path / "index.tar.zst"
    _write_archive(archive, r2_map.PHOTON_DATA_DIRNAME)
    r2_map._unpack_photon_index(archive, dest)

    assert (dest / r2_map.PHOTON_DATA_DIRNAME / "node.lock").exists()
    # The previous index is gone, not merged with the new one.
    assert not (dest / r2_map.PHOTON_DATA_DIRNAME / "stale.marker").exists()
    assert not (dest / ".photon_index_staging").exists()
    assert not (dest / f"{r2_map.PHOTON_DATA_DIRNAME}.old").exists()


def test_real_unpack_rejects_an_archive_with_the_wrong_top_level_dir(tmp_path):
    """Silently installing an index photon cannot find would produce a healthy
    container that answers nothing."""
    pytest.importorskip("zstandard")
    dest = tmp_path / "volume"
    dest.mkdir()
    archive = tmp_path / "index.tar.zst"
    _write_archive(archive, "photon_data_oops")

    with pytest.raises(RuntimeError, match="photon_data"):
        r2_map._unpack_photon_index(archive, dest)
    assert not (dest / r2_map.PHOTON_DATA_DIRNAME).exists()
    assert not (dest / ".photon_index_staging").exists()


# --- cli wiring (integrator-owned; skips until applied) ----------------------

def test_cli_exposes_fetch_and_refresh_commands():
    """`src/cli.py` is a shared file this lane may not edit — the exact command
    bodies are handed to the integrator in the lane report. This asserts the
    wiring once it lands and skips until then."""
    from src import cli

    missing = [c for c in ("fetch_photon_index", "refresh_photon_index")
               if c not in cli.COMMANDS]
    if missing:
        pytest.skip(f"src/cli.py wiring not applied yet: {missing}")

    assert getattr(cli, "sync_photon_index", None) is not None, \
        "cli must import sync_photon_index from r2_map"

    calls: list[str] = []

    def fake_sync():
        calls.append("sync")
        return {"changed": True, "key": _KEY_NEW}

    for name in ("fetch_photon_index", "refresh_photon_index"):
        calls.clear()
        import pytest as _pytest  # local: monkeypatch fixture isn't available here
        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(cli, "sync_photon_index", fake_sync)
            result = cli.COMMANDS[name]()
        # Both commands are thin wrappers around the one sync — the difference
        # is only how loudly `changed` is logged.
        assert calls == ["sync"]
        assert result["changed"] is True
