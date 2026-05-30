"""Lock in CLI command dispatch + the Denver localizer Jinja filter."""

from datetime import datetime, timezone

from src import api_admin, cli


def test_cli_unknown_command_returns_2():
    assert cli.main(["nope"]) == 2


def test_cli_no_args_returns_2():
    assert cli.main([]) == 2


def test_cli_known_commands_registered():
    expected = {"ingest_cycle", "archive_if_due", "daily_sla", "migrate"}
    assert expected <= set(cli.COMMANDS)


def test_denver_ts_dst_in_summer():
    # May 30: MDT (UTC-6)
    assert (
        api_admin._denver_ts(datetime(2026, 5, 30, 17, 0, tzinfo=timezone.utc))
        == "2026-05-30 11:00:00 MDT"
    )


def test_denver_ts_dst_in_winter():
    # Jan 15: MST (UTC-7)
    assert (
        api_admin._denver_ts(datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc))
        == "2026-01-15 10:00:00 MST"
    )


def test_denver_ts_accepts_iso_string():
    assert (
        api_admin._denver_ts("2026-05-30T17:00:00+00:00")
        == "2026-05-30 11:00:00 MDT"
    )


def test_denver_ts_handles_z_suffix():
    assert (
        api_admin._denver_ts("2026-05-30T17:00:00Z")
        == "2026-05-30 11:00:00 MDT"
    )


def test_denver_ts_handles_naive_datetime_as_utc():
    # No tzinfo → treat as UTC (matches Postgres TIMESTAMP behavior when
    # column type is TIMESTAMPTZ but driver returns naive)
    assert (
        api_admin._denver_ts(datetime(2026, 5, 30, 17, 0))
        == "2026-05-30 11:00:00 MDT"
    )


def test_denver_ts_handles_none_and_empty():
    assert api_admin._denver_ts(None) == ""
    assert api_admin._denver_ts("") == ""


def test_read_active_crontab_falls_back_to_repo_when_container_paths_missing(monkeypatch, tmp_path):
    # Force the container paths to point at non-existent files so the
    # repo-fallback branch is exercised.
    monkeypatch.setattr(api_admin, "_STATE_CRONTAB", tmp_path / "nope" / "state-crontab")
    monkeypatch.setattr(api_admin, "_DEFAULT_CRONTAB", tmp_path / "nope" / "default-crontab")
    text, source = api_admin._read_active_crontab()
    # The repo-fallback resolves to the actual repo's crontab file
    assert "ingest_cycle" in text
    assert "local dev" in source


def test_validate_crontab_gracefully_handles_missing_supercronic(monkeypatch):
    # On platforms without supercronic on PATH (developer macOS), the
    # validator must return False with a clear error rather than crashing.
    import subprocess as sp

    def _raise(*args, **kwargs):
        raise FileNotFoundError("supercronic")

    monkeypatch.setattr(sp, "run", _raise)
    ok, msg = api_admin._validate_crontab("*/10 * * * * /bin/true\n")
    assert ok is False
    assert "supercronic" in msg.lower()
