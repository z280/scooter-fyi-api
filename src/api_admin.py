"""GitHub-OAuth-protected admin panel (spec §8)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import accounts, auth
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


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler_status(request: Request, user: dict = Depends(auth.require_admin)):
    """Show the active crontab + recent cycle cadence for diagnosing drift."""
    crontab_text, crontab_source = _read_active_crontab()

    # Recent cycles + observed gap (minutes between consecutive start_ts)
    recent = []
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    start_ts,
                    job_status,
                    EXTRACT(EPOCH FROM start_ts -
                        LAG(start_ts) OVER (ORDER BY start_ts ASC)) / 60.0
                        AS gap_minutes
                FROM observation_cycles
                ORDER BY start_ts DESC
                LIMIT 30
                """,
            )
            for r in cur.fetchall():
                recent.append({
                    "start_ts": r[0],
                    "job_status": r[1],
                    "gap_minutes": round(float(r[2]), 2) if r[2] is not None else None,
                })

    return _render(
        "scheduler.html",
        user=user,
        crontab=crontab_text,
        crontab_source=crontab_source,
        recent=recent,
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
