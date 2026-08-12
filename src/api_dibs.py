"""Dibs: recording a rider's claim on a scooter, and proving it later.

WHAT THIS IS FOR. Veo has no reservation system, so a rider who spots a
scooter four blocks away has no way to say "that one's mine" and no way to
settle it if somebody else arrives at the same moment. Dibs is the honest
substitute — a timestamped claim with exactly the standing of calling dibs on
the front seat, and no more.

The claim itself lives on the phone. This module exists for the CERTIFICATE,
which is a different problem: it gets shown to somebody who has no reason to
trust the person holding it, and a timestamp stored only in that person's
localStorage is one they can edit. So the claim is registered here, the
timestamp is the DATABASE's, and the certificate carries a QR back to a page
the other person can open on their own phone without installing anything.

That page is also the app's front door, which is the other half of why the
certificate exists — a rider showing one is, at that moment, introducing
somebody to both the app and the idea.

STILL NOT ENFORCEMENT. Nothing here prevents anyone from riding anything, and
no endpoint here will ever tell a rider that a vehicle is unavailable because
somebody else called dibs. The moment it did, dibs would be a promise the app
has no standing to make.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .client_ip import real_client_ip
from .pg import connection
from .ratelimit import enforce

log = logging.getLogger(__name__)

router = APIRouter()

#: Mirrors src/dibs.ts. Ten minutes to set off plus the fifteen-minute walk a
#: claim is allowed to be. Copied onto the row at claim time rather than
#: computed on read, so changing the rules cannot retroactively extend or void
#: certificates already handed out.
DIBS_MAX_TOTAL_MINUTES = 25

#: A rider hedging across a few scooters is normal; a script registering
#: hundreds is not. Generous enough that nobody legitimate meets it.
DIBS_RATE = (20, 60)

#: The certificate's QR points HERE — the API's own host — because the
#: verification page is served by this module. The app lives elsewhere.
API_BASE = "https://data.scooter.fyi"
APP_BASE = "https://denver.scooter.fyi"

#: The two registered campaign codes (sql/076_dibs.sql seeds them). They must
#: exist in the campaigns registry or src/campaigns.py resolves them to
#: 'other' — the QR would scan, the traffic would arrive, and the report would
#: read zero.
#:
#: The scan itself is `dibbs`; a click THROUGH the verification page is
#: `dibbs-validation`, and they are separated because they mean different
#: things. One is a stranger being curious about a certificate they were just
#: shown. The other is that stranger going and getting the app.
CAMPAIGN_SCAN = "dibbs"
CAMPAIGN_VALIDATION = "dibbs-validation"

VALIDATION_UTM = (
    f"?utm_source=dibs-validation&utm_medium=referral&utm_campaign={CAMPAIGN_VALIDATION}"
)


def _new_id() -> str:
    """Short, URL-safe and unguessable.

    It goes into a QR and then into a stranger's address bar, so it has to be
    short enough to be robust at certificate size. It must NOT be sequential:
    a browsable list of who called dibs on what, keyed by an integer anybody
    can increment, is a privacy leak dressed as a primary key.
    """
    return secrets.token_urlsafe(9)


class DibsIn(BaseModel):
    vehicle_identifier: str = Field(min_length=1, max_length=64)
    vehicle_name: str = Field(min_length=1, max_length=120)
    plate: str | None = Field(default=None, max_length=32)
    claimed_by: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="Veo", max_length=40)
    device_type: str = Field(default="", max_length=40)
    #: Where the rider was standing. Carried onto any referral made from this
    #: certificate — a signup won at a light-rail stop is a different fact
    #: from one won in a suburb.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


def _limit(request: Request) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            enforce(cur, bucket="dibs_ip", key=real_client_ip(request),
                    limit=DIBS_RATE[0], window_seconds=DIBS_RATE[1])
        conn.commit()


@router.post("/api/v1/dibs", dependencies=[Depends(_limit)])
def create_dibs(body: DibsIn) -> dict[str, Any]:
    """Register a claim and hand back its certificate links.

    THE TIMESTAMP IS OURS. `claimed_at` is the database's NOW(), never
    anything the client sent — that is the entire reason this endpoint exists.
    A rider with a wrong clock, or one who set theirs back deliberately,
    cannot win an argument they should lose.
    """
    dibs_id = _new_id()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dibs (id, vehicle_identifier, vehicle_name, "
                "plate, claimed_by, provider, device_type, lat, lon, "
                "expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() + %s) "
                "RETURNING claimed_at, expires_at",
                (dibs_id, body.vehicle_identifier, body.vehicle_name,
                 body.plate, body.claimed_by, body.provider, body.device_type,
                 body.lat, body.lon,
                 timedelta(minutes=DIBS_MAX_TOTAL_MINUTES)),
            )
            claimed_at, expires_at = cur.fetchone()
        conn.commit()
    return {
        "id": dibs_id,
        "claimed_at": claimed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "verify_url": f"{API_BASE}/dibs/{dibs_id}",
        "qr_url": f"/api/v1/dibs/{dibs_id}/qr.svg",
    }


def _fetch(dibs_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, vehicle_identifier, vehicle_name, plate, "
                "       claimed_by, claimed_at, expires_at, NOW(), "
                "       provider, device_type, lat, lon "
                "FROM dibs WHERE id = %s",
                (dibs_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "vehicle_identifier": row[1],
        "vehicle_name": row[2],
        "plate": row[3],
        "claimed_by": row[4],
        "claimed_at": row[5],
        "expires_at": row[6],
        "now": row[7],
        "provider": row[8],
        "device_type": row[9],
        "lat": row[10],
        "lon": row[11],
    }


@router.get("/api/v1/dibs/{dibs_id}")
def get_dibs(dibs_id: str) -> dict[str, Any]:
    """The claim, as data. Public on purpose — verification that needs an
    account is verification the other person cannot do."""
    d = _fetch(dibs_id)
    if d is None:
        raise HTTPException(404, {"error": "no_such_dibs"})
    return {
        "id": d["id"],
        "vehicle_name": d["vehicle_name"],
        "plate": d["plate"],
        "claimed_by": d["claimed_by"],
        "claimed_at": d["claimed_at"].isoformat(),
        "expires_at": d["expires_at"].isoformat(),
        # An EXPIRED certificate is still a true one, and saying so is half
        # the point: "I had dibs, you took it anyway" is a real thing to be
        # able to show. So this reports the fact rather than 404ing on it.
        "active": d["expires_at"] > d["now"],
        "denver_time": _denver(d["claimed_at"]),
    }


def _denver(at: datetime) -> str:
    """Denver time, in the form a person reads aloud.

    Pinned to the city the scooter is in rather than to the reader's phone:
    the certificate is settled by comparing two claims at one intersection,
    and a traveller's device would print an hour off every other one there.
    """
    try:
        from zoneinfo import ZoneInfo
        local = at.astimezone(ZoneInfo("America/Denver"))
    except Exception:  # noqa: BLE001 — tzdata missing is not worth a 500
        local = at.astimezone(timezone.utc)
    return local.strftime("%a, %b %-d, %Y at %-I:%M:%S %p %Z")


@router.get("/api/v1/dibs/{dibs_id}/qr.svg")
def dibs_qr(dibs_id: str) -> Response:
    """The QR for a certificate, as SVG.

    Vector because a certificate is shown at whatever size the holder's phone
    happens to be, and a raster QR scaled up is a QR that stops scanning.

    Error-correction level Q rather than the usual M: this code gets scanned
    off a screen held at arm's length, at an angle, possibly in Denver sun,
    by somebody who is mildly annoyed. The extra redundancy costs a few
    modules and buys tolerance exactly where it is needed.
    """
    import segno

    d = _fetch(dibs_id)
    if d is None:
        raise HTTPException(404, {"error": "no_such_dibs"})
    target = (
        f"{API_BASE}/dibs/{dibs_id}"
        f"?utm_source=dibs-certificate&utm_medium=qr&utm_campaign={CAMPAIGN_SCAN}"
        # The claim's own id rides along as the referrer, so a signup that
        # arrives this way can be attributed to the RIDER who showed the
        # certificate and not merely to the channel.
        f"&ref={dibs_id}"
    )
    # `currentColor` is not a colour segno will accept, so the ink is set on
    # the wrapper instead: the SVG is emitted without a fill and the page's
    # colour cascades into it. (Kept explicit rather than hardcoding black —
    # the certificate is printed on parchment in light mode and on a lighter
    # card in dark, and the code has to stay legible on both.)
    buf = segno.make(target, error="q").svg_inline(scale=1, border=2)
    return Response(
        content=buf,
        media_type="image/svg+xml",
        headers={
            # A certificate's QR never changes once issued.
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


# --- the page a stranger lands on -------------------------------------------

def _esc(v: str) -> str:
    return (
        v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace('"', "&quot;")
    )


@router.get("/dibs/{dibs_id}", response_class=HTMLResponse)
def dibs_page(dibs_id: str) -> HTMLResponse:
    """The page behind the QR.

    WHO IS READING THIS. Not the rider — somebody standing next to them, who
    has just been shown a phone and told "I called dibs". They have no account,
    no app, and no particular goodwill. So the page answers their question in
    the first line, in words, before anything else: was this claim real, and
    when was it made.

    It is deliberately a full page and not JSON: the point of a QR on a
    certificate is that the other person can check it themselves without
    installing anything, and a wall of braces does not settle an argument.

    Everything that leaves here goes to the app tagged `dibbs-validation` —
    somebody who was shown a certificate by a stranger and then went looking
    for the app is a different, more interesting event than the scan itself.
    """
    d = _fetch(dibs_id)
    if d is None:
        return HTMLResponse(_page_shell(
            "Not a dibs claim",
            "<p class=\"lede\">This certificate could not be found. It may have "
            "been mistyped, or it was never issued.</p>",
        ), status_code=404)

    active = d["expires_at"] > d["now"]
    status = (
        '<p class="status status--live">✅ Still live right now.</p>'
        if active else
        '<p class="status status--past">🕓 This one has since expired — but it '
        'was real, and this is when it was called.</p>'
    )
    # "(provider) (device_type) (vanity_name)" — "Veo scooter Lunar 🐸 928".
    # Assembled from the parts that are actually present rather than joined
    # blindly: an older certificate has no device_type, and "Veo  Lunar" with
    # a hole in it reads as a bug.
    what = " ".join(
        _esc(x) for x in (d.get("provider"), d.get("device_type"), d["vehicle_name"]) if x
    )
    plate = f'<p class="plate">Plate {_esc(d["plate"])}</p>' if d["plate"] else ""
    who = _esc(d["claimed_by"])

    body = f'''
      <p class="callout">
        <span class="who">{who}</span>
        <span class="verb">called dibbs on</span>
        <span class="what">{what}</span>
      </p>
      {plate}
      <p class="lede">at</p>
      <p class="when">{_esc(_denver(d["claimed_at"]))}</p>
      {status}
      <p class="fine">
        That time came from Scooter.fyi's servers, not from anyone's phone —
        which is the only reason it's worth anything in an argument. Dibbs
        isn't a reservation; Veo doesn't offer one. It's a timestamp, and
        whatever standing it earns you in person.
      </p>

      <div class="signup">
        <h2>Want to call dibbs yourself?</h2>
        <p class="signup__sub">
          Sign up with your phone and email and your friend
          <strong>{who}</strong> gets <strong>100 pts</strong> for referring you.
        </p>
        <form method="post" action="/dibs/{_esc(d["id"])}/refer" class="signup__form">
          <label>
            <span>Email</span>
            <input type="email" name="email" autocomplete="email"
                   placeholder="you@example.com" inputmode="email">
          </label>
          <label>
            <span>Phone</span>
            <input type="tel" name="phone" autocomplete="tel"
                   placeholder="(303) 555-0142" inputmode="tel">
          </label>
          <button type="submit">Sign me up &rarr;</button>
          <p class="signup__fine">
            Either one is enough. We'll only use it to set up your account.
          </p>
        </form>
        <a class="plainlink" href="{APP_BASE}/{VALIDATION_UTM}&amp;ref={_esc(d["id"])}">
          or just have a look at the map first &rarr;
        </a>
      </div>
    '''
    return HTMLResponse(_page_shell("Certificate of Dibbs", body))


class ReferIn(BaseModel):
    email: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)


@router.post("/dibs/{dibs_id}/refer", response_class=HTMLResponse)
def dibs_refer(
    dibs_id: str,
    email: str = Form(default=""),
    phone: str = Form(default=""),
) -> HTMLResponse:
    """The referral form on a certificate page.

    A PLAIN HTML FORM, posted and re-rendered. The person filling it in is
    standing on a pavement on somebody else's phone, on whatever connection
    they have — this has to work before any JavaScript does, and a form that
    needs a bundle to submit is a form that fails exactly there.

    The referral is created ON BEHALF OF the certificate's owner: they are the
    one who did the introducing, and they are the one the points are for. It
    inherits the claim's position too, so "where did this signup come from" has
    an answer.

    Nothing is awarded here. This is a lead until the newcomer actually turns
    up and rides — see sql/076's note on why paying at signup rewards the
    wrong thing.
    """
    email = (email or "").strip()
    phone = (phone or "").strip()
    d = _fetch(dibs_id)
    if d is None:
        return HTMLResponse(
            _page_shell("Not a dibbs claim",
                        '<p class="lede">That certificate could not be found.</p>'),
            status_code=404,
        )
    if not email and not phone:
        return HTMLResponse(
            _page_shell("One more thing",
                        '<p class="lede">We need an email or a phone number to '
                        'set up your account — either one is enough.</p>'
                        f'<a class="cta" href="/dibs/{_esc(dibs_id)}">Back</a>'),
            status_code=400,
        )

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO referrals (dibs_id, referrer_username, email, "
                "phone, lat, lon) VALUES (%s, %s, %s, %s, %s, %s)",
                (dibs_id, d["claimed_by"], email or None, phone or None,
                 d.get("lat"), d.get("lon")),
            )
        conn.commit()

    who = _esc(d["claimed_by"])
    return HTMLResponse(_page_shell("You're on the list", f'''
      <p class="callout"><span class="what">Nice one 🛴</span></p>
      <p class="lede">
        We'll be in touch to finish setting up your account. Once you're
        riding, <strong>{who}</strong> gets their 100 points.
      </p>
      <a class="cta" href="{APP_BASE}/{VALIDATION_UTM}&amp;ref={_esc(dibs_id)}">
        Have a look at the map &rarr;
      </a>
      <p class="fine">
        Free, no app store. Live scooter map for Denver, with routes that
        avoid the roads you shouldn't ride down.
      </p>
    '''))


def _page_shell(title: str, body: str) -> str:
    """One self-contained document: no CDN, no webfont, no analytics script.

    Somebody scanning this is on a street, on someone else's phone plan, with
    about ten seconds of patience. It has to render on the first packet — which
    is also why the form below it is a plain HTML form and not a component.

    THE LOGO. `_logo()` is the one place artwork goes. It is a placeholder mark
    right now because the real asset has not landed here yet; dropping the file
    into src/static and pointing that function at it is the whole change.
    """
    return f'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · Scooter.fyi</title>
<meta name="robots" content="noindex">
<meta property="og:title" content="{_esc(title)}">
<meta property="og:description" content="Somebody called dibbs. Here's the receipt.">
<style>
  :root {{ color-scheme: light; --ink:#2b2418; --muted:#7a6a4c; --gold:#c9a94f;
          --paper:#fffdf7; --accent:#0066ff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:20px 14px 40px;
         background:
           radial-gradient(1200px 400px at 50% -160px, #fff6d8 0%, transparent 70%),
           #f2ead8;
         font-family: ui-rounded, "SF Pro Rounded", system-ui, -apple-system,
                      "Segoe UI", Roboto, sans-serif;
         color:var(--ink); -webkit-text-size-adjust:100%; }}
  .card {{ max-width:430px; margin:0 auto; padding:20px 20px 22px; text-align:center;
          background:var(--paper); border-radius:22px;
          border:1px solid #e7dcc0;
          box-shadow:0 1px 0 #fff inset, 0 14px 40px rgba(80,60,20,.16); }}
  /* A ticket-stub notch, because this is a stub. */
  .card::before, .card::after {{ content:""; display:block; height:14px;
          background:radial-gradient(circle at 8px 50%, #f2ead8 7px, transparent 7px) left/22px 14px repeat-x; }}
  .card::before {{ margin:-20px -20px 12px; }}
  .card::after  {{ margin:16px -20px -22px; transform:rotate(180deg); }}
  .logo {{ color:var(--accent); }}
  .logo__mark {{ display:block; width:104px; height:auto; margin:0 auto; }}
  .wordmark {{ margin:2px 0 0; font-size:12px; font-weight:800; letter-spacing:.14em;
              text-transform:uppercase; color:var(--muted); }}
  h1 {{ margin:12px 0 14px; font-size:15px; font-weight:800; letter-spacing:.12em;
       text-transform:uppercase; color:var(--muted); }}
  h1::before, h1::after {{ content:"✦"; color:var(--gold); margin:0 8px; }}
  .callout {{ margin:0; display:flex; flex-direction:column; gap:2px; }}
  .who {{ font-size:26px; font-weight:800; line-height:1.15; overflow-wrap:anywhere; }}
  .verb {{ font-size:13px; color:var(--muted); }}
  .what {{ font-size:22px; font-weight:800; line-height:1.2; overflow-wrap:anywhere; }}
  .plate {{ margin:6px 0 0; font-size:12px; color:var(--muted); }}
  .lede {{ margin:10px 0 0; font-size:13px; color:var(--muted); }}
  .when {{ margin:2px 0 0; font-size:17px; font-weight:800;
          font-variant-numeric:tabular-nums; }}
  .status {{ margin:14px 0 0; font-size:14px; font-weight:800; }}
  .status--live {{ color:#12833c; }}
  .status--past {{ color:var(--muted); }}
  .fine {{ margin:14px 0 0; font-size:11.5px; line-height:1.55; color:var(--muted); }}

  .signup {{ margin:20px -20px -22px; padding:20px; text-align:left;
            background:#1d2733; color:#eef3f8;
            border-radius:0 0 22px 22px; }}
  .signup h2 {{ margin:0; font-size:18px; font-weight:800; }}
  .signup__sub {{ margin:6px 0 14px; font-size:13.5px; line-height:1.5; color:#c4d0dd; }}
  .signup__form {{ display:flex; flex-direction:column; gap:10px; }}
  .signup label {{ display:flex; flex-direction:column; gap:4px; font-size:12px;
                  font-weight:700; letter-spacing:.04em; text-transform:uppercase;
                  color:#9fb0c2; }}
  .signup input {{ font:inherit; font-size:16px; padding:12px 13px; border-radius:12px;
                  border:1px solid #33465c; background:#111a24; color:#fff; }}
  .signup input::placeholder {{ color:#6b8098; }}
  .signup button {{ margin-top:4px; padding:14px 16px; border:0; border-radius:12px;
                   background:var(--accent); color:#fff; font:inherit; font-size:16px;
                   font-weight:800; cursor:pointer; }}
  .signup__fine {{ margin:2px 0 0; font-size:11.5px; color:#8fa1b4; }}
  .plainlink {{ display:block; margin:14px 0 0; font-size:13px; color:#9fc3ff; }}
  .cta {{ display:block; margin:16px 0 0; padding:14px 16px; border-radius:14px;
         background:var(--accent); color:#fff; text-decoration:none;
         font-size:15px; font-weight:800; }}
</style>
</head><body>
  <div class="card">
    {_logo()}
    <h1>{_esc(title)}</h1>
    {body}
  </div>
</body></html>'''


def _logo() -> str:
    """The FYI speech-bubble mark, inlined.

    INLINE, NOT AN <img>. The whole page is one request on purpose — somebody
    scanning this is on a street, on someone else's phone plan — and a second
    round trip for a logo is the one that shows up as a blank box while it
    loads.

    Inlining is only affordable because the artwork was reduced first. The
    supplied EPS traced to 69 KB of near-collinear points; simplified to 0.12
    units it is 2.2 KB and renders identically at every size this page uses.
    It draws in `currentColor`, so the mark themes with the card rather than
    carrying a second hardcoded blue.
    """
    return f'<div class="logo">{LOGO_SVG}</div><p class="wordmark">Scooter.fyi</p>'


#: See _logo(). Kept as a constant so the artwork is one obvious thing to
#: replace, and so the function above stays readable.
LOGO_SVG = '<svg class="logo__mark" role="img" aria-label="Scooter.fyi" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 109 80"><path fill-rule="evenodd" fill="currentColor" d="M 58.2 1.5 L 52.2 1.8 L 25.1 1.6 L 16.8 2 L 11.4 2.8 L 9.1 3.6 L 7.6 4.4 L 6.9 4.9 L 5 6.9 L 3.6 9.2 L 2.9 10.8 L 1.6 15.3 L 1.1 18.6 L 0.9 22.4 L 1.2 35.3 L 0.8 42.7 L 0.8 49.2 L 1.1 52.3 L 1.9 55.9 L 2.8 58.3 L 3.7 60 L 5.4 62.2 L 7.2 63.7 L 9.7 64.8 L 12.1 65.3 L 15.6 65.6 L 34.2 65.4 L 37.9 65.6 L 39.8 66 L 40.8 66.9 L 41.4 69.2 L 41.5 73.5 L 41.2 75.9 L 41.3 77.3 L 41.8 78.3 L 42.7 78.9 L 43.5 79.1 L 45.9 79.1 L 47 78.9 L 49.7 77.9 L 51.2 77 L 53.9 74.9 L 60.8 68.2 L 64.2 66 L 66.2 65.3 L 68.2 64.9 L 71 64.7 L 79.6 64.6 L 84.8 64.6 L 90 65 L 93.8 65 L 96.9 64.5 L 99.1 63.8 L 102.5 62 L 105.1 59.6 L 106.8 56.8 L 107.5 54.9 L 107.9 53.1 L 108.4 48.6 L 108.4 43.4 L 108.2 40.2 L 108.3 15.2 L 107.8 11.5 L 107.3 9.8 L 106.1 7.4 L 104.7 5.6 L 102.8 3.9 L 100.5 2.5 L 98.1 1.6 L 94.8 1 L 88.9 0.9 L 85 1.1 L 77.8 1.1 L 68 0.8 L 58.3 1.5 Z M 63.1 7.4 L 76.9 6.9 L 87.2 6.9 L 92.9 7.1 L 95.3 7.4 L 97.4 8 L 99.2 9.1 L 100.1 10.1 L 101.2 12.1 L 101.8 14 L 102.3 16.8 L 102.5 19.7 L 102.1 31.5 L 102.1 38.9 L 102.5 45.3 L 102.5 49.3 L 102.2 51.3 L 101.3 53.6 L 99.3 56 L 97 57.6 L 93.8 58.7 L 90.6 59.1 L 87.9 59.1 L 78 58.5 L 74.3 58.8 L 69.4 59.5 L 64.2 59.7 L 62 60.1 L 60.9 60.5 L 59.5 61.4 L 55.8 65.2 L 54 66.7 L 49.4 69.8 L 47.9 70.3 L 47.4 70.2 L 47 69.8 L 46.6 68.2 L 46.6 66.6 L 46.9 64.3 L 46.8 61.6 L 45.9 60.5 L 44.9 59.9 L 43.5 59.5 L 41.5 59.3 L 36 59.6 L 30.9 59.6 L 27.3 59.3 L 24.8 59.3 L 19.7 59.8 L 15.9 59.7 L 14 59.3 L 11.6 58.3 L 10.6 57.7 L 8.5 55.6 L 7.5 53.4 L 7 50.4 L 7 24.5 L 7.3 19.8 L 8.1 15.2 L 9.3 12.3 L 10 11.3 L 11.4 9.9 L 13 9 L 14.7 8.4 L 17.8 7.8 L 20.4 7.6 L 31.1 7.8 L 50.1 7.8 L 62.8 7.4 Z"/><path fill-rule="nonzero" fill="currentColor" d="M 30.9 21.7 L 29.4 33.5 L 42.6 33.5 L 41.8 39 L 28.8 39 L 26.8 54.8 L 19.8 54.8 L 24.6 16.2 L 47.1 16.2 L 46.4 21.7 Z M 65.7 39.8 L 63.9 54.8 L 56.9 54.8 L 58.8 39.8 L 48.7 16.2 L 54.9 16.2 L 55.7 16.3 L 56.8 17.1 L 62.4 32.1 L 63.1 34.8 L 65.1 31.1 L 73.8 17.2 L 74.4 16.6 L 75 16.3 L 81.6 16.2 Z M 87.7 54.8 L 80.7 54.8 L 85.4 16.2 L 92.4 16.2 Z"/></svg>'
