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
