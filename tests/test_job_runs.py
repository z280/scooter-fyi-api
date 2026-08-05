"""The job-run ledger (sql/062, src/job_runs.py) and the /admin/scheduler
report it feeds.

Two properties matter more than the rest and get the most attention here:

  * **Recording must never break the job.** A scheduled command's purpose
    is its work, not its bookkeeping, so every failure mode of the ledger
    itself has to be survivable — including the database being unreachable
    at the moment the run starts or finishes.
  * **The wrapper is at the dispatch point, not per command.** That is what
    makes a job added later recorded by existing rather than by somebody
    remembering. The test for it asserts against `COMMANDS` itself, so a new
    entry is covered automatically.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src import cli, job_runs


class _FakeCursor:
    def __init__(self, store, fail_on=None):
        self._store = store
        self._fail_on = fail_on or ()
        self._sql = ""

    def execute(self, sql, params=None):
        self._sql = sql
        for needle in self._fail_on:
            if needle in sql:
                raise RuntimeError(f"database is on fire ({needle})")
        self._store.append((sql, params))

    def fetchone(self):
        return (1,)

    @property
    def rowcount(self):
        return 3

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store, fail_on=None):
        self._store, self._fail_on = store, fail_on
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self._store, self._fail_on)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, module=job_runs, fail_on=None, unreachable=False) -> list:
    store: list = []

    @contextmanager
    def _conn():
        if unreachable:
            raise RuntimeError("could not connect")
        yield _FakeConn(store, fail_on)

    monkeypatch.setattr(module, "connection", _conn)
    return store


# ---------------------------------------------------------------------------
# What gets recorded, and what deliberately does not
# ---------------------------------------------------------------------------

def test_ingest_cycle_is_excluded_with_a_stated_reason():
    assert not job_runs.is_recorded("ingest_cycle")
    # Not just absent — the exemption says why, so the next person does not
    # have to guess whether it was deliberate.
    assert "observation_cycles" in job_runs.EXCLUDED_COMMANDS["ingest_cycle"]


def test_every_other_command_is_recorded():
    recorded = [c for c in cli.COMMANDS if job_runs.is_recorded(c)]
    assert set(cli.COMMANDS) - set(recorded) == {"ingest_cycle"}, (
        "the default is to record; an exemption should be a deliberate, "
        "documented entry in EXCLUDED_COMMANDS"
    )


def test_start_returns_none_for_an_excluded_command(monkeypatch):
    store = _install(monkeypatch)
    assert job_runs.start("ingest_cycle") is None
    assert store == [], "and it must not even touch the database"


def test_start_opens_a_running_row_before_the_work(monkeypatch):
    store = _install(monkeypatch)
    assert job_runs.start("daily_sla") == 1
    sql, params = store[0]
    assert "INSERT INTO job_runs" in sql
    assert "'running'" in sql
    assert params == ("daily_sla",)


def test_finish_records_status_and_summary(monkeypatch):
    store = _install(monkeypatch)
    job_runs.finish(1, status="ok", summary={"deleted": 4})
    sql, params = store[0]
    assert "UPDATE job_runs" in sql
    assert params[0] == "ok"
    assert '"deleted": 4' in params[1]


def test_finish_is_a_noop_without_a_run_id(monkeypatch):
    store = _install(monkeypatch)
    job_runs.finish(None, status="ok", summary={"x": 1})
    assert store == [], "so callers need no special case for excluded commands"


# ---------------------------------------------------------------------------
# Bookkeeping must never break the job
# ---------------------------------------------------------------------------

def test_start_survives_an_unreachable_database(monkeypatch):
    _install(monkeypatch, unreachable=True)
    assert job_runs.start("daily_sla") is None, "degrades to 'not recorded'"


def test_finish_survives_an_unreachable_database(monkeypatch):
    _install(monkeypatch, unreachable=True)
    job_runs.finish(1, status="ok")  # must not raise


def test_a_broken_ledger_does_not_fail_a_healthy_command(monkeypatch):
    _install(monkeypatch, unreachable=True)
    monkeypatch.setitem(cli.COMMANDS, "daily_sla", lambda: {"ok": True})
    assert cli.main(["daily_sla"]) == 0, (
        "the archive still archived; the ledger being down is not its problem"
    )


def test_an_unserializable_summary_does_not_fail_the_run(monkeypatch):
    store = _install(monkeypatch)

    class Opaque:
        pass

    job_runs.finish(1, status="ok", summary={"thing": Opaque()})
    _sql, params = store[0]
    assert "thing" in params[1], "rendered via default=str rather than raising"


def test_datetimes_in_a_summary_serialize(monkeypatch):
    # Several commands return a run timestamp or a window bound.
    store = _install(monkeypatch)
    job_runs.finish(1, status="ok", summary={"computed_at": datetime(2026, 7, 30, tzinfo=timezone.utc)})
    _sql, params = store[0]
    assert "2026-07-30" in params[1]


# ---------------------------------------------------------------------------
# The dispatch wrapper
# ---------------------------------------------------------------------------

def test_a_successful_command_is_recorded_ok_with_its_summary(monkeypatch):
    calls: list = []
    monkeypatch.setattr(job_runs, "start", lambda cmd: calls.append(("start", cmd)) or 7)
    monkeypatch.setattr(
        job_runs, "finish",
        lambda run_id, **kw: calls.append(("finish", run_id, kw.get("status"), kw.get("summary"))),
    )
    monkeypatch.setitem(cli.COMMANDS, "daily_sla", lambda: {"rows": 2})

    assert cli.main(["daily_sla"]) == 0
    assert calls[0] == ("start", "daily_sla")
    assert calls[1] == ("finish", 7, "ok", {"rows": 2})


def test_a_failing_command_is_recorded_error_and_still_exits_1(monkeypatch):
    calls: list = []
    monkeypatch.setattr(job_runs, "start", lambda cmd: 7)
    monkeypatch.setattr(
        job_runs, "finish",
        lambda run_id, **kw: calls.append((kw.get("status"), kw.get("error"))),
    )

    def _boom():
        raise ValueError("nope")

    monkeypatch.setitem(cli.COMMANDS, "daily_sla", _boom)

    assert cli.main(["daily_sla"]) == 1, "the exit code is still the job's verdict"
    status, error = calls[0]
    assert status == "error"
    assert "ValueError: nope" in error


def test_the_wrapper_records_whatever_command_ran(monkeypatch):
    """Guards the actual point of wrapping the dispatch: a command added to
    COMMANDS later is recorded without anyone instrumenting it."""
    seen: list = []
    monkeypatch.setattr(job_runs, "start", lambda cmd: seen.append(cmd) or 1)
    monkeypatch.setattr(job_runs, "finish", lambda *a, **k: None)
    monkeypatch.setitem(cli.COMMANDS, "a_brand_new_job", lambda: {"fine": True})

    assert cli.main(["a_brand_new_job"]) == 0
    assert seen == ["a_brand_new_job"]


def test_an_unknown_command_is_not_recorded(monkeypatch):
    seen: list = []
    monkeypatch.setattr(job_runs, "start", lambda cmd: seen.append(cmd) or 1)
    assert cli.main(["not_a_command"]) == 2
    assert seen == [], "usage errors are not runs"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_prune_deletes_by_age_and_reports_the_count(monkeypatch):
    store = _install(monkeypatch)
    out = job_runs.prune(keep_days=30)
    sql, params = store[0]
    assert "DELETE FROM job_runs" in sql
    assert params == (30,)
    assert out == {"deleted": 3, "keep_days": 30}


def test_prune_is_wired_to_the_cli(monkeypatch):
    assert "cleanup_job_runs" in cli.COMMANDS


# ---------------------------------------------------------------------------
# The /admin/scheduler report
# ---------------------------------------------------------------------------

def test_crontab_schedules_parses_command_to_expression():
    from src.api_admin import _crontab_schedules

    text = """
# a comment mentioning python -m src.cli not_this_one
*/2 * * * * cd /app && python -m src.cli ingest_cycle
0 9 * * * cd /app && python -m src.cli daily_sla
15 9 * * 1 cd /app && python -m src.cli refresh_area_universe
"""
    out = _crontab_schedules(text)
    assert out["daily_sla"] == "0 9 * * *"
    assert out["refresh_area_universe"] == "15 9 * * 1"
    assert "not_this_one" not in out, "comments are not schedule lines"


def test_crontab_schedules_joins_a_command_scheduled_twice():
    from src.api_admin import _crontab_schedules

    # daily_sla and daily_trips genuinely share the 9am slot, and several
    # retention jobs share 3:30 — a command on two lines is scheduled twice.
    out = _crontab_schedules(
        "30 3 * * * cd /app && python -m src.cli cleanup_receipts\n"
        "0 4 * * * cd /app && python -m src.cli cleanup_receipts\n"
    )
    assert out["cleanup_receipts"] == "30 3 * * * , 0 4 * * *"


def test_crontab_schedules_skips_a_line_it_cannot_parse():
    from src.api_admin import _crontab_schedules

    out = _crontab_schedules("garbage python -m src.cli daily_sla\n")
    assert out == {}, "a display aid must not raise on a malformed crontab"


def test_the_scheduler_page_actually_renders(monkeypatch):
    """Renders the real handler and template end to end.

    The narrow parser tests above all passed while `api_admin` was missing
    its `job_runs` import — the page would have raised NameError on the
    first request. Only exercising the handler catches that, so this does.
    """
    from src import api_admin

    runs = [
        {"command": "daily_sla", "started_at": datetime.now(timezone.utc),
         "finished_at": datetime.now(timezone.utc), "status": "ok",
         "duration_ms": 1200, "summary": {"rows": 2}, "error": None},
        {"command": "archive_if_due", "started_at": datetime.now(timezone.utc) - timedelta(hours=1),
         "finished_at": None, "status": "error", "duration_ms": None,
         "summary": None, "error": "RuntimeError: boom"},
    ]
    monkeypatch.setattr(api_admin.job_runs, "latest_per_command", lambda: runs)
    monkeypatch.setattr(api_admin.job_runs, "recent", lambda limit=50: runs)
    monkeypatch.setattr(
        api_admin, "_read_active_crontab",
        lambda: ("0 9 * * * cd /app && python -m src.cli daily_sla\n", "(test)"),
    )

    html = api_admin.scheduler_status(None, user={"login": "tester"}).body.decode()

    assert "daily_sla" in html and "0 9 * * *" in html
    assert "RuntimeError: boom" in html, "a failed run must be visible, not just logged"
    assert "gap_minutes" not in html, (
        "the ingest-cadence table belongs to /admin/cycles; duplicating it here "
        "is what crowded out every other job"
    )
    assert "observation_cycles" in html, "and the page says where the cycles went"


def test_the_scheduler_page_lists_a_scheduled_job_that_has_never_run(monkeypatch):
    # The row an operator is actually hunting for.
    from src import api_admin

    monkeypatch.setattr(api_admin.job_runs, "latest_per_command", lambda: [])
    monkeypatch.setattr(api_admin.job_runs, "recent", lambda limit=50: [])
    monkeypatch.setattr(
        api_admin, "_read_active_crontab",
        lambda: ("30 3 * * * cd /app && python -m src.cli cleanup_receipts\n", "(test)"),
    )
    html = api_admin.scheduler_status(None, user={"login": "tester"}).body.decode()
    assert "cleanup_receipts" in html
    assert "never" in html


def test_the_page_lists_scheduled_or_ever_run_but_not_every_manual_tool(monkeypatch):
    """`migrate` and the backfills are real commands, but they are one-off
    manual tools. Listing them as "not scheduled / never" on a page about
    the schedule buries the rows that matter."""
    from src import api_admin

    monkeypatch.setattr(api_admin.job_runs, "latest_per_command", lambda: [])
    monkeypatch.setattr(api_admin.job_runs, "recent", lambda limit=50: [])
    monkeypatch.setattr(
        api_admin, "_read_active_crontab",
        lambda: ("0 9 * * * cd /app && python -m src.cli daily_sla\n", "(test)"),
    )
    html = api_admin.scheduler_status(None, user={"login": "tester"}).body.decode()
    ops = html.split("<h2>Operations</h2>")[1].split("<h2>Recent runs")[0]

    assert "daily_sla" in ops
    assert "<code>migrate</code>" not in ops, "a manual command nobody has run is not an operation"
    assert "backfill_battery_trips" not in ops


def test_a_manual_command_appears_once_somebody_actually_runs_it(monkeypatch):
    from src import api_admin

    run = {"command": "backfill_battery_trips", "started_at": datetime.now(timezone.utc),
           "finished_at": datetime.now(timezone.utc), "status": "ok",
           "duration_ms": 90_000, "summary": {"rides": 40}, "error": None}
    monkeypatch.setattr(api_admin.job_runs, "latest_per_command", lambda: [run])
    monkeypatch.setattr(api_admin.job_runs, "recent", lambda limit=50: [run])
    monkeypatch.setattr(api_admin, "_read_active_crontab", lambda: ("", "(test)"))

    html = api_admin.scheduler_status(None, user={"login": "tester"}).body.decode()
    ops = html.split("<h2>Operations</h2>")[1].split("<h2>Recent runs")[0]
    assert "backfill_battery_trips" in ops, "running it is what makes it worth showing"
    assert "not scheduled" in ops


def test_prune_does_not_swallow_its_own_failure(monkeypatch):
    """The docstring rule is specific: start/finish swallow because they are
    bookkeeping attached to another job. prune IS a job — if it cannot
    delete, it has failed, and cleanup_job_runs should exit non-zero."""
    _install(monkeypatch, unreachable=True)
    with pytest.raises(Exception):
        job_runs.prune(keep_days=30)
