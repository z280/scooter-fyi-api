"""GitHub-OAuth-protected admin panel (spec §8)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import accounts, auth, campaigns, job_runs
from .cli import COMMANDS
from .pg import connection

router = APIRouter(prefix="/admin")

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

_DENVER_TZ = ZoneInfo("America/Denver")


def _denver_ts(v) -> str:
    """Jinja filter: render a UTC datetime (or ISO string, or epoch) as a
    Denver-local timestamp with timezone abbreviation (MDT or MST)."""
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    if isinstance(v, (int, float)):
        v = datetime.fromtimestamp(v, tz=ZoneInfo("UTC"))
    if v.tzinfo is None:
        v = v.replace(tzinfo=ZoneInfo("UTC"))
    return v.astimezone(_DENVER_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


_env.filters["denver_ts"] = _denver_ts


def _render(name: str, **ctx) -> HTMLResponse:
    tpl = _env.get_template(name)
    return HTMLResponse(tpl.render(**ctx))


@router.get("/login")
async def login(request: Request):
    return await auth.login(request)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    return await auth.callback(request)


@router.get("/logout")
def logout(request: Request):
    return auth.logout(request)


@router.get("", include_in_schema=False)
def index(request: Request):
    if "admin_user" not in request.session:
        return RedirectResponse("/admin/login")
    return RedirectResponse("/admin/cycles")


@router.get("/cycles", response_class=HTMLResponse)
def cycles(
    request: Request,
    user: dict = Depends(auth.require_admin),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, le=200, ge=1),
):
    offset = page * page_size
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cycle_id, start_ts, job_status, transmission_status,
                       LEFT(COALESCE(errors,''), 200) AS error_preview
                FROM observation_cycles
                ORDER BY start_ts DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            rows = [
                {
                    "cycle_id": str(r[0]),
                    "start_ts": r[1].isoformat() if r[1] else None,
                    "job_status": r[2],
                    "transmission_status": r[3],
                    "error_preview": r[4],
                }
                for r in cur.fetchall()
            ]
    return _render("cycles.html", user=user, rows=rows, page=page, page_size=page_size)


@router.get("/cycles/{cycle_id}", response_class=HTMLResponse)
def cycle_detail(
    cycle_id: str,
    request: Request,
    user: dict = Depends(auth.require_admin),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM observation_cycles WHERE cycle_id = %s",
                (cycle_id,),
            )
            r = cur.fetchone()
            if not r:
                return _render("not_found.html", user=user, what=f"cycle {cycle_id}")
            cols = [d.name for d in cur.description]
            cycle = dict(zip(cols, r))
            blob = cycle.get("data_json_blob")
            if isinstance(blob, str):
                try:
                    blob = json.loads(blob)
                except json.JSONDecodeError:
                    pass
            cycle["data_json_blob"] = json.dumps(blob, indent=2, default=str) if blob else ""

            cur.execute(
                "SELECT * FROM transmission_attempts WHERE cycle_id = %s ORDER BY ts_transmission",
                (cycle_id,),
            )
            tx_cols = [d.name for d in cur.description]
            tx = [dict(zip(tx_cols, row)) for row in cur.fetchall()]

            cur.execute(
                "SELECT * FROM api_failures WHERE cycle_id = %s ORDER BY attempt_time",
                (cycle_id,),
            )
            fx_cols = [d.name for d in cur.description]
            fx = [dict(zip(fx_cols, row)) for row in cur.fetchall()]
    return _render("cycle_detail.html", user=user, cycle=cycle, tx=tx, failures=fx)


@router.get("/failures", response_class=HTMLResponse)
def failures(request: Request, user: dict = Depends(auth.require_admin)):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, cycle_id, attempt_time, failure_type, http_status_code,
                       LEFT(COALESCE(error_details,''), 500)
                FROM api_failures ORDER BY attempt_time DESC LIMIT 200
                """
            )
            rows = [
                {
                    "id": r[0],
                    "cycle_id": str(r[1]) if r[1] else None,
                    "attempt_time": r[2].isoformat() if r[2] else None,
                    "failure_type": r[3],
                    "http_status_code": r[4],
                    "error_details": r[5],
                }
                for r in cur.fetchall()
            ]
    return _render("failures.html", user=user, rows=rows)


# Crontab file locations:
#   STATE_CRONTAB — the editable copy on the shared volume; what supercronic
#                   actually executes. /admin/scheduler/edit writes here.
#   DEFAULT_CRONTAB — the baked-in image default. Used as a fallback display
#                     and the source for the "Reset to default" button.
_STATE_CRONTAB = Path(os.environ.get("CRONTAB_STATE_PATH", "/app/state/crontab"))
_DEFAULT_CRONTAB = Path(os.environ.get("CRONTAB_DEFAULT_PATH", "/app/crontab"))


def _read_active_crontab() -> tuple[str, str]:
    """Return (text, source-label). Prefers the editable state file; falls
    back to the baked default; finally falls back to the repo file for
    local dev outside the container."""
    if _STATE_CRONTAB.exists():
        return _STATE_CRONTAB.read_text(), str(_STATE_CRONTAB)
    if _DEFAULT_CRONTAB.exists():
        return _DEFAULT_CRONTAB.read_text(), str(_DEFAULT_CRONTAB) + " (default; not yet seeded to state)"
    repo_fallback = Path(__file__).resolve().parents[1] / "crontab"
    if repo_fallback.exists():
        return repo_fallback.read_text(), str(repo_fallback) + " (local dev)"
    return "", "(no crontab file found)"


def _validate_crontab(text: str) -> tuple[bool, str]:
    """Use supercronic's -test flag to validate a proposed crontab.
    Returns (ok, message). The same image runs in the worker container,
    so the supercronic binary is on the path."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".crontab", delete=False, prefix="proposed-"
    ) as f:
        f.write(text)
        proposed_path = f.name
    try:
        result = subprocess.run(
            ["supercronic", "-test", proposed_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, "valid"
        return False, (result.stderr or result.stdout or "validation failed").strip()
    except FileNotFoundError:
        return False, "supercronic binary not found in worker container"
    except subprocess.TimeoutExpired:
        return False, "supercronic -test timed out"
    finally:
        try:
            os.unlink(proposed_path)
        except OSError:
            pass


def _crontab_schedules(crontab_text: str) -> dict[str, str]:
    """command name -> the cron expression that runs it, parsed out of the
    active crontab. Lets the page put "last ran" next to "supposed to run",
    which is the pair an operator actually needs: neither number means much
    without the other.

    Deliberately forgiving — this is a display aid, not a validator (that is
    supercronic's job, via _validate_crontab). A line it cannot parse is
    skipped rather than raising. When one command appears on several lines,
    their expressions are joined, since that is genuinely what is scheduled.
    """
    out: dict[str, list[str]] = {}
    for raw in crontab_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"python -m src\.cli\s+([A-Za-z0-9_]+)", line)
        if not m:
            continue
        # The 5 leading time fields, before the command.
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        out.setdefault(m.group(1), []).append(" ".join(fields[:5]))
    return {cmd: " , ".join(exprs) for cmd, exprs in out.items()}


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler_status(request: Request, user: dict = Depends(auth.require_admin)):
    """Every scheduled operation: what it is supposed to do, when it last
    did it, and what it reported.

    This page used to show the crontab plus a cadence table for the ingest
    cycle — which duplicated /admin/cycles (backed by observation_cycles,
    and far more detailed) while every OTHER job in the crontab had no
    operator-visible record at all. The cadence table is gone; the ingest
    cycle keeps its own page, and this one covers the rest.
    """
    crontab_text, crontab_source = _read_active_crontab()
    schedules = _crontab_schedules(crontab_text)

    latest = {r["command"]: r for r in job_runs.latest_per_command()}

    # Scheduled, or has ever run. Both halves earn their place: a command
    # that is scheduled but has never fired is the interesting failure, and
    # one that has run but is no longer in the crontab is a rename or a line
    # someone deleted without meaning to.
    #
    # Deliberately NOT every entry in COMMANDS. Plenty of those are one-off
    # manual tools — `migrate`, the backfills, the artifact fetchers — and
    # listing them as "not scheduled / never" on a page about the schedule
    # is noise that buries the rows above. One of them becomes visible the
    # moment somebody actually runs it, which is when it is worth seeing.
    commands = sorted(set(schedules) | set(latest))
    rows = []
    for cmd in commands:
        if not job_runs.is_recorded(cmd):
            continue
        run = latest.get(cmd)
        rows.append({
            "command": cmd,
            "schedule": schedules.get(cmd),
            "known": cmd in COMMANDS,
            "run": run,
        })
    # Scheduled-but-never-run first — that is the row an operator is looking
    # for — then errors, then by recency.
    def _sort_key(r):
        run = r["run"]
        never = r["schedule"] is not None and run is None
        failed = bool(run and run["status"] == "error")
        return (not never, not failed,
                -(run["started_at"].timestamp() if run else 0))
    rows.sort(key=_sort_key)

    return _render(
        "scheduler.html",
        user=user,
        crontab=crontab_text,
        crontab_source=crontab_source,
        rows=rows,
        recent=job_runs.recent(50),
        excluded=job_runs.EXCLUDED_COMMANDS,
    )


@router.get("/scheduler/edit", response_class=HTMLResponse)
def scheduler_edit_form(
    request: Request,
    user: dict = Depends(auth.require_admin),
    error: str | None = Query(None),
    saved: bool = Query(False),
):
    """Render the textarea editor with current crontab contents."""
    text, source = _read_active_crontab()
    default_text = _DEFAULT_CRONTAB.read_text() if _DEFAULT_CRONTAB.exists() else ""
    return _render(
        "scheduler_edit.html",
        user=user,
        crontab=text,
        source=source,
        default_crontab=default_text,
        error=error,
        saved=saved,
    )


def _csrf_ok(request: Request) -> bool:
    """Lightweight CSRF check: the Origin or Referer header on a POST must
    match this app's own host. Belt-and-suspenders since SameSite=lax
    already blocks cross-site POSTs, but cheap to add."""
    host = request.headers.get("host")
    if not host:
        return False
    for header in ("origin", "referer"):
        v = request.headers.get(header)
        if v and host in v:
            return True
    return False


@router.post("/scheduler/edit")
def scheduler_edit_save(
    request: Request,
    crontab: str = Form(...),
    action: str = Form("save"),
    user: dict = Depends(auth.require_admin),
):
    """Validate via `supercronic -test`, then write to the shared volume.
    supercronic in the scheduler container picks it up within ~15s via
    its mtime-poll wrapper."""
    if not _csrf_ok(request):
        return RedirectResponse(
            url="/admin/scheduler/edit?error=" + "cross-site+request+blocked",
            status_code=303,
        )

    if action == "reset":
        if not _DEFAULT_CRONTAB.exists():
            return RedirectResponse(
                url="/admin/scheduler/edit?error=default+crontab+not+found",
                status_code=303,
            )
        new_text = _DEFAULT_CRONTAB.read_text()
    else:
        new_text = crontab.replace("\r\n", "\n")  # normalize browser line endings
        if not new_text.endswith("\n"):
            new_text += "\n"

    ok, msg = _validate_crontab(new_text)
    if not ok:
        # URL-encode the error so it survives the redirect
        from urllib.parse import quote
        return RedirectResponse(
            url="/admin/scheduler/edit?error=" + quote(msg)[:500],
            status_code=303,
        )

    _STATE_CRONTAB.parent.mkdir(parents=True, exist_ok=True)
    _STATE_CRONTAB.write_text(new_text)
    return RedirectResponse(url="/admin/scheduler/edit?saved=1", status_code=303)


@router.get("/regions", response_class=HTMLResponse)
def regions(
    request: Request,
    user: dict = Depends(auth.require_admin),
    layer: str = Query("neighborhood"),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot_time FROM regional_metrics_narrow
                WHERE region_type = %s
                ORDER BY snapshot_time DESC LIMIT 1
                """,
                (layer,),
            )
            snap = cur.fetchone()
            rows: list[dict[str, Any]] = []
            if snap:
                cur.execute(
                    """
                    SELECT region_name, count_total, count_bikes, count_scooters
                    FROM regional_metrics_narrow
                    WHERE region_type = %s AND snapshot_time = %s
                    ORDER BY count_total DESC
                    """,
                    (layer, snap[0]),
                )
                rows = [
                    {
                        "region_name": r[0],
                        "count_total": r[1],
                        "count_bikes": r[2],
                        "count_scooters": r[3],
                    }
                    for r in cur.fetchall()
                ]
    return _render(
        "regions.html",
        user=user,
        layer=layer,
        snapshot_time=snap[0].isoformat() if snap else None,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Admin allowlist management (the ADMIN_EMAILS replacement)
# ---------------------------------------------------------------------------
# This page is gated by the GitHub-OAuth admin session (auth.require_admin),
# a SEPARATE trust boundary from the allowlist it edits. The allowlist
# (accounts.admin_emails / admin_allowlist table) authorizes the account
# session surface — /api/v1/private/* and the /api/v1/user plate fields.
# So a GitHub operator manages who counts as an account-session admin.
@router.get("/admins", response_class=HTMLResponse)
def admins_page(
    request: Request,
    user: dict = Depends(auth.require_admin),
    error: str | None = Query(None),
    saved: str | None = Query(None),
):
    return _render(
        "admins.html",
        user=user,
        admins=accounts.list_admins(),
        error=error,
        saved=saved,
    )


@router.post("/admins/add")
def admins_add(
    request: Request,
    email: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    if not _csrf_ok(request):
        return RedirectResponse("/admin/admins?error=cross-site+request+blocked", status_code=303)
    try:
        added = accounts.add_admin(email, added_by=user.get("login"))
    except ValueError:
        return RedirectResponse("/admin/admins?error=not+an+email+address", status_code=303)
    return RedirectResponse(
        f"/admin/admins?saved={'added' if added else 'already+present'}", status_code=303
    )


@router.post("/admins/remove")
def admins_remove(
    request: Request,
    email: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    if not _csrf_ok(request):
        return RedirectResponse("/admin/admins?error=cross-site+request+blocked", status_code=303)
    removed = accounts.remove_admin(email)
    return RedirectResponse(
        f"/admin/admins?saved={'removed' if removed else 'not+found'}", status_code=303
    )


# --- User analytics ---------------------------------------------------------
# Reads the RAW telemetry tables (sql/061) directly: at this deployment's
# traffic a few GROUP BYs over 30 days of events are instant, and the raw
# retention windows (90d events / 30d request metrics) comfortably cover
# every range this page offers. The *_daily rollups exist for the private
# JSON endpoints and for history beyond the raw windows.


@router.get("/analytics", response_class=HTMLResponse)
def analytics(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    user: dict = Depends(auth.require_admin),
):
    from datetime import timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=days)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT (received_at AT TIME ZONE 'America/Denver')::date AS day,
                       COUNT(DISTINCT visitor_hash) AS visitors,
                       COUNT(DISTINCT session_id) AS sessions,
                       COUNT(*) AS events
                FROM telemetry_events
                WHERE received_at >= %s
                GROUP BY 1 ORDER BY 1 DESC
                """,
                (since,),
            )
            daily = [
                {"day": d, "visitors": v, "sessions": s, "events": e}
                for d, v, s, e in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT name, COUNT(*) AS events,
                       COUNT(DISTINCT visitor_hash) AS visitors
                FROM telemetry_events
                WHERE received_at >= %s
                GROUP BY name ORDER BY events DESC
                """,
                (since,),
            )
            top_events = [
                {"name": n, "events": e, "visitors": v}
                for n, e, v in cur.fetchall()
            ]

            def _prop_counts(event: str, prop: str, limit: int = 12):
                cur.execute(
                    """
                    SELECT props->>%s AS value, COUNT(*) AS n
                    FROM telemetry_events
                    WHERE name = %s AND received_at >= %s
                          AND props ? %s
                    GROUP BY 1 ORDER BY n DESC LIMIT %s
                    """,
                    (prop, event, since, prop, limit),
                )
                return [{"value": v, "n": n} for v, n in cur.fetchall()]

            drawers = _prop_counts("drawer_open", "drawer")
            modes = _prop_counts("mode_switch", "mode")
            popup_actions = _prop_counts("popup_action", "action")
            controls = _prop_counts("control_change", "control", limit=20)

            # Ride funnel: event totals in flow order.
            cur.execute(
                """
                SELECT name, COALESCE(props->>'screen', '') AS screen,
                       COUNT(*) AS n
                FROM telemetry_events
                WHERE received_at >= %s
                      AND name IN ('ride_open', 'ride_screen',
                                   'ride_complete', 'ride_abandon')
                GROUP BY 1, 2
                """,
                (since,),
            )
            ride_raw = cur.fetchall()
            ride_open = sum(n for name, _, n in ride_raw if name == "ride_open")
            ride_complete = sum(
                n for name, _, n in ride_raw if name == "ride_complete"
            )
            ride_screens = sorted(
                (
                    {"screen": screen, "n": n}
                    for name, screen, n in ride_raw
                    if name == "ride_screen" and screen
                ),
                key=lambda r: float(r["screen"])
                if r["screen"].replace(".", "").isdigit()
                else 99,
            )
            ride_abandons = sum(
                n for name, _, n in ride_raw if name == "ride_abandon"
            )

            cur.execute(
                """
                SELECT name, props->>'method' AS method, COUNT(*) AS n
                FROM telemetry_events
                WHERE received_at >= %s
                      AND name IN ('auth_start', 'auth_success', 'auth_error')
                GROUP BY 1, 2 ORDER BY 2, 1
                """,
                (since,),
            )
            auth_funnel = [
                {"name": n, "method": m or "?", "n": c}
                for n, m, c in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT device_class, os_family, viewport,
                       COUNT(DISTINCT visitor_hash) AS visitors
                FROM telemetry_events
                WHERE received_at >= %s
                GROUP BY 1, 2, 3 ORDER BY visitors DESC
                """,
                (since,),
            )
            devices = [
                {"device": d, "os": o, "viewport": vp, "visitors": v}
                for d, o, vp, v in cur.fetchall()
            ]

            # Simple paths: first mode chosen per session, and which drawer
            # sets sessions touch. Deliberately not sequence mining.
            cur.execute(
                """
                SELECT mode, COUNT(*) AS n FROM (
                    SELECT DISTINCT ON (session_id)
                           session_id, props->>'mode' AS mode
                    FROM telemetry_events
                    WHERE name = 'mode_switch' AND received_at >= %s
                    ORDER BY session_id, received_at
                ) t GROUP BY mode ORDER BY n DESC
                """,
                (since,),
            )
            entry_modes = [{"mode": m or "?", "n": n} for m, n in cur.fetchall()]

            cur.execute(
                """
                SELECT drawers, COUNT(*) AS n FROM (
                    SELECT session_id,
                           string_agg(DISTINCT props->>'drawer', ' + ') AS drawers
                    FROM telemetry_events
                    WHERE name = 'drawer_open' AND received_at >= %s
                    GROUP BY session_id
                ) t GROUP BY drawers ORDER BY n DESC LIMIT 15
                """,
                (since,),
            )
            drawer_sets = [{"drawers": d, "n": n} for d, n in cur.fetchall()]

            cur.execute(
                """
                SELECT route, COUNT(*) AS requests,
                       COALESCE(percentile_cont(0.95)
                           WITHIN GROUP (ORDER BY duration_ms), 0)::int AS p95,
                       COUNT(*) FILTER (WHERE status >= 500) AS errors
                FROM request_metrics
                WHERE at >= %s
                GROUP BY route ORDER BY requests DESC LIMIT 25
                """,
                (since,),
            )
            api_health = [
                {"route": r, "requests": n, "p95": p, "errors": e}
                for r, n, p, e in cur.fetchall()
            ]

    return _render(
        "analytics.html",
        user=user,
        days=days,
        daily=daily,
        top_events=top_events,
        drawers=drawers,
        modes=modes,
        popup_actions=popup_actions,
        controls=controls,
        ride_open=ride_open,
        ride_screens=ride_screens,
        ride_complete=ride_complete,
        ride_abandons=ride_abandons,
        auth_funnel=auth_funnel,
        devices=devices,
        entry_modes=entry_modes,
        drawer_sets=drawer_sets,
        api_health=api_health,
    )


# --- Campaigns --------------------------------------------------------------
# Marketing-campaign registry (sql/074) + per-campaign acquisition numbers.
# Monitoring reads the RAW telemetry_events table like /admin/analytics does
# (live, covers every window this page offers); the all-time column reads the
# campaigns_daily rollup, which outlives the 90-day raw pruning.


def _site_origin() -> str:
    """The public frontend origin, for displaying shareable tagged links.
    Derived from the configured magic-link template rather than a second
    hardcoded URL."""
    from urllib.parse import urlparse

    from .config import load

    try:
        u = urlparse(load().accounts.magic_link_url_template)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return "https://denver.scooter.fyi"


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    code: str | None = Query(None),
    error: str | None = Query(None),
    saved: str | None = Query(None),
    user: dict = Depends(auth.require_admin),
):
    from datetime import timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = campaigns.list_campaigns()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT campaign,
                       COUNT(DISTINCT visitor_hash) AS visitors,
                       COUNT(DISTINCT session_id)   AS sessions,
                       COUNT(*)                     AS events,
                       COUNT(*) FILTER (WHERE name = 'page_load'),
                       COUNT(*) FILTER (WHERE name = 'ride_complete'),
                       COUNT(*) FILTER (WHERE name = 'auth_success')
                FROM telemetry_events
                WHERE received_at >= %s AND campaign <> 'none'
                GROUP BY campaign
                """,
                (since,),
            )
            window_stats = {
                r[0]: {
                    "visitors": r[1],
                    "sessions": r[2],
                    "events": r[3],
                    "page_loads": r[4],
                    "ride_completes": r[5],
                    "auth_successes": r[6],
                }
                for r in cur.fetchall()
            }

            # Sessions/events sum cleanly across days; distinct visitors do
            # not (the hash rotates daily by design), so all-time shows
            # sessions, not a bogus visitor total.
            cur.execute(
                """
                SELECT campaign, SUM(sessions), SUM(ride_completes),
                       MIN(day), MAX(day)
                FROM campaigns_daily
                GROUP BY campaign
                """
            )
            alltime = {
                r[0]: {
                    "sessions": r[1],
                    "ride_completes": r[2],
                    "first_day": r[3],
                    "last_day": r[4],
                }
                for r in cur.fetchall()
            }

            detail = None
            if code and any(c["code"] == code for c in rows):
                cur.execute(
                    """
                    SELECT (received_at AT TIME ZONE 'America/Denver')::date,
                           COUNT(DISTINCT visitor_hash),
                           COUNT(DISTINCT session_id),
                           COUNT(*),
                           COUNT(*) FILTER (WHERE name = 'ride_complete')
                    FROM telemetry_events
                    WHERE campaign = %s AND received_at >= %s
                    GROUP BY 1 ORDER BY 1 DESC
                    """,
                    (code, since),
                )
                detail = {
                    "code": code,
                    "daily": [
                        {
                            "day": d,
                            "visitors": v,
                            "sessions": s,
                            "events": e,
                            "ride_completes": rc,
                        }
                        for d, v, s, e, rc in cur.fetchall()
                    ],
                }

    known_codes = {c["code"] for c in rows}
    # Tagged traffic whose code matched no live campaign ('other'), shown so
    # typos in printed material don't vanish silently.
    other = window_stats.get(campaigns.UNKNOWN)
    for c in rows:
        c["stats"] = window_stats.get(c["code"])
        c["alltime"] = alltime.get(c["code"])
        c["link"] = f"{_site_origin()}/?utm_campaign={c['code']}"
    # Rollup rows for campaigns since deleted from the registry would be
    # invisible above; surface them too (defensive — deletion isn't offered).
    orphaned = sorted(
        k for k in alltime if k not in known_codes and k != campaigns.UNKNOWN
    )
    return _render(
        "campaigns.html",
        user=user,
        days=days,
        rows=rows,
        other=other,
        orphaned=orphaned,
        detail=detail,
        error=error,
        saved=saved,
    )


# QR codes for the tagged links, for stickers/posters. Error correction
# 'q' (25%) because printed codes get scuffed. SVG for print (crisp at any
# size), PNG for pasting into chats/docs. Served for archived campaigns
# too — an operator may still need the artwork file, and the admin session
# gate means nothing here is public.


def _campaign_qr(code: str):
    import segno

    if campaigns.get(code) is None:
        return None
    return segno.make(f"{_site_origin()}/?utm_campaign={code}", error="q")


@router.get("/campaigns/{code}/qr.png")
def campaign_qr_png(
    code: str,
    scale: int = Query(10, ge=2, le=40),
    user: dict = Depends(auth.require_admin),
):
    import io

    qr = _campaign_qr(code)
    if qr is None:
        return _render("not_found.html", user=user, what=f"campaign {code}")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=4)
    return Response(
        buf.getvalue(),
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="campaign-{code}-qr.png"'
        },
    )


@router.get("/campaigns/{code}/qr.svg")
def campaign_qr_svg(
    code: str,
    user: dict = Depends(auth.require_admin),
):
    import io

    qr = _campaign_qr(code)
    if qr is None:
        return _render("not_found.html", user=user, what=f"campaign {code}")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=10, border=4)
    return Response(
        buf.getvalue(),
        media_type="image/svg+xml",
        headers={
            "Content-Disposition": f'inline; filename="campaign-{code}-qr.svg"'
        },
    )


@router.post("/campaigns/add")
def campaigns_add(
    request: Request,
    code: str = Form(...),
    name: str = Form(""),
    channel: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    if not _csrf_ok(request):
        return RedirectResponse(
            "/admin/campaigns?error=cross-site+request+blocked", status_code=303
        )
    try:
        created = campaigns.create(
            code, name, channel, notes, created_by=user.get("login") or ""
        )
    except ValueError:
        return RedirectResponse(
            "/admin/campaigns?error=code+must+be+a+slug+(a-z+0-9+-+_,+max+40)",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/campaigns?saved={'created' if created else 'code+already+exists'}",
        status_code=303,
    )


@router.post("/campaigns/archive")
def campaigns_archive(
    request: Request,
    code: str = Form(...),
    action: str = Form("archive"),
    user: dict = Depends(auth.require_admin),
):
    if not _csrf_ok(request):
        return RedirectResponse(
            "/admin/campaigns?error=cross-site+request+blocked", status_code=303
        )
    changed = campaigns.set_archived(code, archived=(action != "unarchive"))
    verb = "unarchived" if action == "unarchive" else "archived"
    return RedirectResponse(
        f"/admin/campaigns?saved={verb if changed else 'no+change'}",
        status_code=303,
    )
