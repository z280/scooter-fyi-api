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

WHAT DIBS DOES AND DOES NOT DO. It does not reserve anything: Veo has no
reservation system, this app cannot stop a vehicle unlocking, and no claim
here changes what the operator will rent to whom.

It DOES now gate our own buttons. `GET /api/v1/dibs/vehicle/{id}` reports a
live claim so the app can grey out "I'll ride this one" and say who called it
— a deliberate change of stance from the first version of this module, which
refused to tell one rider about another's claim on the grounds that doing so
would make dibs a promise.

The reason for the change is that the alternative was worse. Dibs that only
its own holder can see is not a social object at all, just a private note; the
whole premise — two people at one scooter settling it — needs the second
person to be told. A greyed button with a name and a timestamp beside it is an
argument the app is making on somebody's behalf, and riders can override it by
opening Veo directly, which they always could.

So: visible to everyone, binding on nobody, and never presented as
unavailability. The copy says a person called dibs, not that the scooter is
taken.
"""

from __future__ import annotations

import io
import logging
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .accounts import normalize_us_phone
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
#: The scan itself is `dibs`; a click THROUGH the verification page is
#: `dibs-validation`, and they are separated because they mean different
#: things. One is a stranger being curious about a certificate they were just
#: shown. The other is that stranger going and getting the app.
CAMPAIGN_SCAN = "dibs"
CAMPAIGN_VALIDATION = "dibs-validation"

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
    #: The catalogue's device name, maker included — "Veo Cosmo". There is no
    #: separate provider field; see the certificate's `what` line.
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
                "plate, claimed_by, device_type, lat, lon, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW() + %s) "
                "RETURNING claimed_at, expires_at",
                (dibs_id, body.vehicle_identifier, body.vehicle_name,
                 body.plate, body.claimed_by, body.device_type,
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
        # ABSOLUTE, like verify_url. The app is served from a different host
        # (denver.scooter.fyi) than this API, so a relative path resolved
        # against the app's origin and 404'd — the certificate rendered with a
        # broken image where its QR should be.
        "qr_url": f"{API_BASE}/api/v1/dibs/{dibs_id}/qr.svg",
    }


@router.post("/api/v1/dibs/{dibs_id}/release")
def dibs_release(dibs_id: str) -> dict[str, Any]:
    """Give a claim back before it expires.

    A RELEASE HAS TO REACH THE SERVER. The claim lives in two places: the
    holder's own device (`dibs.ts`) and this table, which is what every OTHER
    rider's map reads. Dropping only the local copy would leave the row live
    for up to twenty-five minutes — still dimming that scooter on everybody
    else's map, and, worse, now reading as a STRANGER'S claim to the person
    who just released it, because "is this mine?" is answered by the local
    record that no longer exists.

    Expiring rather than deleting: the row is the evidence behind a
    certificate somebody may already have been shown, and a claim that was
    real and then given back is a different thing from one that never
    happened. `expires_at = NOW()` makes every live-claim query skip it while
    the history stays honest.

    Idempotent, and deliberately not authenticated. There is no session on a
    dibs claim — it is identified by an unguessable id the holder's device
    generated — so possession of that id IS the credential, exactly as it is
    for the certificate URL. The worst a guessed id could do is hand a
    scooter back to everyone.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dibs SET expires_at = NOW() "
                "WHERE id = %s AND expires_at > NOW() RETURNING id",
                (dibs_id,),
            )
            row = cur.fetchone()
        conn.commit()
    # 200 either way: a claim that was already gone is the state the caller
    # wanted, and a retry after a dropped connection must not read as failure.
    return {"released": row is not None}


@router.get("/api/v1/dibs/live")
def live_dibs() -> dict[str, Any]:
    """Every live claim in the city, keyed by vehicle.

    Fetched once per device refresh rather than per popup. Dibs are RARE —
    a handful across the fleet at any moment against thousands of vehicles —
    so one small response every refresh is cheaper than a request each time
    somebody taps a scooter, and it means the popup already knows the answer
    when it opens instead of gaining it a moment later.

    Only the oldest live claim per vehicle is returned, for the reason given
    on the per-vehicle endpoint: when two people call dibs, the earlier claim
    wins by the rules, and returning the newest would have the app quietly
    siding with whoever tapped last.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (vehicle_identifier) "
                "       vehicle_identifier, id, claimed_by, claimed_at, expires_at "
                "FROM dibs WHERE expires_at > NOW() "
                "ORDER BY vehicle_identifier, claimed_at ASC"
            )
            rows = cur.fetchall()
    return {
        "dibs": {
            r[0]: {
                "id": r[1],
                "claimed_by": r[2],
                "claimed_at": r[3].isoformat(),
                "expires_at": r[4].isoformat(),
                "denver_time": _denver(r[3]),
                "certificate_url": f"{API_BASE}/dibs/{r[1]}",
            }
            for r in rows
        }
    }


@router.get("/api/v1/dibs/vehicle/{vehicle_identifier}")
def dibs_for_vehicle(vehicle_identifier: str) -> dict[str, Any]:
    """Does anybody have a live claim on this scooter?

    Read by the device popup so it can say "Resourceful 🌈 has dibs!" and
    grey its own ride button. Public and unauthenticated: the second person in
    the argument is exactly who needs to see this, and they may well not have
    an account.

    Returns the OLDEST live claim rather than the newest. Two people can both
    call dibs — nothing prevents it — and when they do, the earlier claim is
    the one that wins by the rules of dibs, so it is the one shown. Showing
    the newest would have the app quietly siding with whoever tapped last.

    `claimed_by` is a public display name the rider chose. No contact details,
    no account id, nothing that identifies them beyond the handle they picked
    to be known by.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, claimed_by, claimed_at, expires_at "
                "FROM dibs "
                "WHERE vehicle_identifier = %s AND expires_at > NOW() "
                "ORDER BY claimed_at ASC LIMIT 1",
                (vehicle_identifier,),
            )
            row = cur.fetchone()
    if row is None:
        return {"dibs": None}
    return {
        "dibs": {
            "id": row[0],
            "claimed_by": row[1],
            "claimed_at": row[2].isoformat(),
            "expires_at": row[3].isoformat(),
            "denver_time": _denver(row[2]),
            "certificate_url": f"{API_BASE}/dibs/{row[0]}",
        }
    }


def _fetch(dibs_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, vehicle_identifier, vehicle_name, plate, "
                "       claimed_by, claimed_at, expires_at, NOW(), "
                "       device_type, lat, lon "
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
        "device_type": row[8],
        "lat": row[9],
        "lon": row[10],
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
    # A STANDALONE SVG DOCUMENT, not an inline fragment.
    #
    # This used to call `svg_inline()`, which is segno's embed-in-HTML form:
    # it deliberately omits the XML declaration AND the `xmlns`, because
    # inline SVG inherits the namespace from the HTML parser. But this is a
    # URL, and the certificate modal loads it through `<img src=…>` — which
    # parses it as a standalone XML document, finds no namespace, and refuses
    # to render it. The response was a valid 200 image/svg+xml the whole
    # time; browsers simply would not draw it.
    #
    # The ink is explicit for the same reason. `svg_inline` emitted no fill so
    # the page's `currentColor` could cascade in, which works for a fragment
    # and cannot work here: an <img> is an isolated document and inherits
    # nothing from the page around it. #2b2418 is the parchment ink the
    # certificate already uses, dark enough to scan on both card colours.
    buf = io.BytesIO()
    segno.make(target, error="q").save(
        buf, kind="svg", scale=1, border=2, dark="#2b2418", xmldecl=False,
    )
    return Response(
        content=buf.getvalue(),
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

    Everything that leaves here goes to the app tagged `dibs-validation` —
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
    # "(device_type) (vanity_name)" — "Veo Cosmo Lunar 🐸 928". NO separate
    # provider: the catalogue's device name already carries the maker, and
    # printing both rendered "Veo Veo Cosmo Veo Cosmo". Assembled from the
    # parts that are present rather than joined blindly, since an older
    # certificate has no device_type and a hole in the middle reads as a bug.
    what = " ".join(
        _esc(x) for x in (d.get("device_type"), d["vehicle_name"]) if x
    )
    plate = f' <span class="plate">(plate {_esc(d["plate"])})</span>' if d["plate"] else ""
    who = _esc(d["claimed_by"])
    # "this Cosmo" reads better than "this Veo Cosmo Lunar 🐸 928" in a
    # question, and falls back to the generic noun rather than to an empty
    # gap when an older claim carries no device_type.
    model_q = _esc(d.get("device_type") or "") or "ride"

    # THE SENTENCE IS THE PAGE. Present tense while it stands, past tense with
    # the verdict attached once it does not — because the two readers are
    # different people with different questions. Somebody checking a live claim
    # wants to know it is real; somebody checking a dead one is, almost always,
    # about to take the scooter.
    if active:
        claim_line = (
            f'<strong>{who}</strong> has dibs on <strong>{what}</strong>{plate}'
        )
        verdict = '<p class="verdict verdict--live">Still good.</p>'
    else:
        claim_line = (
            f'<strong>{who}</strong> had dibs on <strong>{what}</strong>{plate}, '
            f'but they expired at <strong>{_esc(_denver(d["expires_at"]))}</strong> '
            f'and are now'
        )
        verdict = '<p class="verdict verdict--void">null and void.</p>'

    body = f'''
      <div class="fyi">{FYI_SVG}</div>
      <p class="claim">{claim_line}</p>
      {verdict}
      <p class="lede">called at</p>
      <p class="when">{_esc(_denver(d["claimed_at"]))}</p>
      <p class="fine">
        That time came from Scooter.fyi's servers, not from anyone's phone —
        which is the only reason it's worth anything in an argument.
      </p>

      <!-- THIS PAGE'S OWN ANTI-SCREENSHOT. The in-app certificate proves it
           is live by moving; a server-rendered page cannot move without
           JavaScript, and this page deliberately ships none — the person
           reading it is on a street, on somebody else's phone, and it has to
           work on the first packet.

           So it proves liveness the way only a server can: by stating the
           time it was GENERATED. A screenshot of this page carries a stamp
           that is minutes or days stale, and the reader can see that at a
           glance without being told how to check anything. It is a stronger
           claim than an animation, not a weaker one — an animation proves a
           page is running, this proves WHEN it was fetched.

           The shimmer beside it is there so the rule stated on the in-app
           certificate ("only counts while it's moving") is not contradicted
           by the page it points at. It is CSS, so it costs no script. -->
      <p class="asof"><span class="asof__sweep"></span>
        <span class="asof__text">Loaded live at {_esc(_denver(d["now"]))}</span>
      </p>
      <p class="fine">
        If that time isn&rsquo;t about now, you&rsquo;re looking at a
        screenshot &mdash; open the link yourself.
      </p>

      <details class="rules">
        <summary>The rules of dibs</summary>
        <ol>
          <li><strong>Dibs isn't a reservation.</strong> Veo doesn't offer
              one. This is a timestamp and whatever standing it earns you in
              person — nothing stops anyone riding anything.</li>
          <li><strong>Ten minutes to set off.</strong> Call dibs and don't
              start walking towards it, and your claim is void. Not ten
              minutes to arrive — ten minutes to move.</li>
          <li><strong>Fifteen minutes' walk, maximum.</strong> You can't call
              dibs on something you couldn't plausibly reach.</li>
          <li><strong>Twenty-five minutes and it's over</strong>, however well
              you walked. Ten to set off plus the fifteen you were allowed.</li>
          <li><strong>A certificate only counts while it's moving.</strong>
              The real one animates. A screenshot doesn't.</li>
          <li><strong>Scooter.fyi will try to notify you</strong> if the
              device you have dibs on is no longer available.</li>
        </ol>
      </details>

      <div class="signup signup--standdown">
        <h2>Did you have your heart set on riding this {model_q}?</h2>
        <p class="signup__sub">
          We will give you <strong>300 pts</strong> for being a good guy and
          letting <strong>{who}</strong> use {what}. Just fill out your phone
          number and you&rsquo;ll get 300 pts when you start a ride on
          scooter.fyi today. That&rsquo;s enough to take over a whole
          neighbourhood! <span class="signup__fine-inline">(see 🏆 in app)</span>
        </p>
        <p class="signup__sub signup__sub--alt">
          Already ride with us? Then it&rsquo;s <strong>50 pts</strong> —
          still yours, just for walking away.
        </p>
        <form method="post" action="/dibs/{_esc(d["id"])}/stand-down" class="signup__form">
          <label>
            <span>Phone</span>
            <input type="tel" name="phone" autocomplete="tel"
                   placeholder="(303) 555-0142" inputmode="tel" required>
          </label>
          <button type="submit">I&rsquo;ll be a good guy/gal, help me find a new ride &rarr;</button>
          <p class="signup__fine">
            We&rsquo;ll only use it to set up your account and pay you.
          </p>
        </form>
      </div>

      <div class="signup">
        <h2>Want to call dibs yourself?</h2>
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
    return HTMLResponse(_page_shell("Certificate of Dibs", body))


#: What standing down is worth, and how long the offer lasts. Both are
#: printed on a page people screenshot, so both are constants rather than
#: expressions evaluated at payout time.
#:
#: TWO TIERS, because both kinds of reader deserve the offer and they are not
#: worth the same. 300 buys a NEW rider — an account that would not otherwise
#: exist, which is the whole reason this page is worth having. 50 thanks an
#: EXISTING one for the same courtesy, which is generous for walking away from
#: a scooter and small enough not to be a wage.
#:
#: This is a cheatable shape and it is worth naming: two accounts, one claim,
#: one stand-down, repeat. The deadline and the ride requirement blunt it (you
#: must actually ride to be paid), the amount caps the take, and the honest
#: answer is that per-account point accumulation needs watching in the admin
#: panel. That monitoring does not exist yet.
STAND_DOWN_POINTS_NEW = 300
STAND_DOWN_POINTS_EXISTING = 50
#: "today" — end of the calendar day in Denver, not 24 hours. The copy says
#: today and a rider reads that as "before I go to bed", not "before this time
#: tomorrow".
STAND_DOWN_TZ = ZoneInfo("America/Denver")


def _end_of_denver_day(now: datetime) -> datetime:
    local = now.astimezone(STAND_DOWN_TZ)
    return (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


@router.post("/dibs/{dibs_id}/stand-down", response_class=HTMLResponse)
def dibs_stand_down(
    dibs_id: str,
    phone: str = Form(default=""),
) -> HTMLResponse:
    """"I wanted this scooter, I am letting them have it."

    The one person guaranteed to read a dibs certificate is somebody standing
    at the same scooter who wanted it, and the outcome this page exists to
    avoid is two people arguing on a pavement. So it buys the argument out.

    Recorded on `referrals` as `kind = 'stand_down'` (sql/077): the dibs
    holder still gets their referral 100 for the introduction, and the person
    who walked away is owed 300 in `newcomer_points`. Two debts to two people
    on one row, both gated on the newcomer actually turning up and riding.

    NOTHING IS AWARDED HERE, and that is not a shortcut — see sql/076's note.
    Somebody who types a phone number into a box and never rides has not
    stood down from anything.

    A PLAIN HTML FORM, like the referral above it: this is somebody on a
    pavement on a strange connection, and a form that needs a bundle to submit
    is a form that fails exactly there.
    """
    phone = (phone or "").strip()
    d = _fetch(dibs_id)
    if d is None:
        return HTMLResponse(
            _page_shell("Not a dibs claim",
                        '<p class="lede">That certificate could not be found.</p>'),
            status_code=404,
        )
    if not phone:
        return HTMLResponse(
            _page_shell("One more thing",
                        '<p class="lede">We need a phone number to set up your '
                        'account and pay you.</p>'
                        f'<a class="cta" href="/dibs/{_esc(dibs_id)}">Back</a>'),
            status_code=400,
        )

    deadline = _end_of_denver_day(d["now"])
    # NEW or EXISTING decides the amount. Matched on the normalised number so
    # "(303) 555-0142" and "+13035550142" are the same rider — an existing
    # account that typed its number differently must not be paid the
    # new-rider rate.
    e164 = normalize_us_phone(phone) or phone
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM accounts WHERE phone_number = %s LIMIT 1",
                (e164,),
            )
            existing = cur.fetchone() is not None
            award = (
                STAND_DOWN_POINTS_EXISTING if existing else STAND_DOWN_POINTS_NEW
            )
            cur.execute(
                "INSERT INTO referrals (dibs_id, referrer_username, phone, "
                "lat, lon, kind, newcomer_points, newcomer_deadline) "
                "VALUES (%s, %s, %s, %s, %s, 'stand_down', %s, %s)",
                (dibs_id, d["claimed_by"], e164, d.get("lat"), d.get("lon"),
                 award, deadline),
            )
        conn.commit()

    who = _esc(d["claimed_by"])
    return HTMLResponse(_page_shell("Good guy move 🛴", f'''
      <p class="callout"><span class="what">Respect.</span></p>
      <p class="lede">
        <strong>{who}</strong> gets their scooter, and you get
        <strong>{award} points</strong> the moment you start a ride on
        scooter.fyi today. Let&rsquo;s find you a better one.
      </p>
      <a class="cta" href="{APP_BASE}/{VALIDATION_UTM}&amp;ref={_esc(dibs_id)}">
        Find me a ride &rarr;
      </a>
      <p class="fine">
        We&rsquo;ll text you to finish setting up your account. Points land
        once you&rsquo;ve actually ridden — before midnight, Denver time.
      </p>
    '''))


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
            _page_shell("Not a dibs claim",
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
<meta property="og:description" content="Somebody called dibs. Here's the receipt.">
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
  .mark {{ display:flex; align-items:center; justify-content:center; gap:9px;
          color:var(--accent); }}
  .mark__art {{ width:38px; height:auto; flex:0 0 auto; }}
  .mark__word {{ font-size:19px; font-weight:800; letter-spacing:-.02em;
                color:var(--ink); }}
  .mark__tld {{ color:var(--accent); }}
  h1 {{ margin:14px 0 16px; font-size:13px; font-weight:800; letter-spacing:.14em;
       text-transform:uppercase; color:var(--muted); }}
  h1::before, h1::after {{ content:"✦"; color:var(--gold); margin:0 8px; }}

  /* The FYI, lifted straight from the mark it came from. */
  /* The certificate's opening word IS the mark. */
  .fyi {{ margin:0 0 10px; color:var(--accent); }}
  .fyi__art {{ display:block; width:96px; height:auto; margin:0 auto; }}
  .claim {{ margin:0; font-size:19px; line-height:1.4; overflow-wrap:anywhere; }}
  .plate {{ color:var(--muted); font-size:14px; white-space:nowrap; }}
  .verdict {{ margin:8px 0 0; font-size:22px; font-weight:900; letter-spacing:.02em; }}
  .verdict--live {{ color:#12833c; }}
  .verdict--void {{ color:#c02626; text-transform:uppercase; }}
  .lede {{ margin:14px 0 0; font-size:13px; color:var(--muted); }}
  .when {{ margin:2px 0 0; font-size:16px; font-weight:800;
          font-variant-numeric:tabular-nums; }}
  .fine {{ margin:14px 0 0; font-size:11.5px; line-height:1.55; color:var(--muted); }}

  /* Liveness. See the comment on .asof in the body. */
  .asof {{ position:relative; overflow:hidden; margin:14px 0 0; padding:8px 10px;
          border-radius:999px; background:rgba(0,102,255,.08);
          border:1px solid rgba(0,102,255,.25); }}
  .asof__sweep {{ position:absolute; inset:0;
    background:linear-gradient(100deg, transparent 0%, rgba(0,102,255,.26) 45%,
                               rgba(0,102,255,.05) 60%, transparent 100%);
    transform:translateX(-100%); animation:asof-sweep 2.1s linear infinite; }}
  @keyframes asof-sweep {{ to {{ transform:translateX(100%); }} }}
  @media (prefers-reduced-motion: reduce) {{
    .asof__sweep {{ animation:asof-pulse 2.4s ease-in-out infinite; transform:none; }}
    @keyframes asof-pulse {{ 0%,100% {{ opacity:.25 }} 50% {{ opacity:.75 }} }}
  }}
  .asof__text {{ position:relative; font-size:12.5px; font-weight:800; color:#0b4fbf;
                font-variant-numeric:tabular-nums; }}

  .rules {{ margin:16px 0 0; text-align:left; border-top:1px solid #eadfc4;
           padding-top:12px; }}
  .rules summary {{ font-size:12.5px; font-weight:800; letter-spacing:.06em;
                   text-transform:uppercase; color:var(--muted); cursor:pointer; }}
  .rules ol {{ margin:10px 0 0; padding-left:18px; }}
  .rules li {{ margin:0 0 8px; font-size:12.5px; line-height:1.5; color:var(--ink); }}

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
    """The scooter.fyi mark, inlined.

    INLINE, NOT AN <img>. The page is one request on purpose — somebody
    scanning this is on a street, on someone else's phone plan — and a second
    round trip for artwork is the one that shows as a blank box while it
    loads.

    Extracted from the supplied artwork as the ART ONLY: the wordmark beside
    it is set below in real text, which stays legible at any size and to a
    screen reader. Curves are preserved verbatim from the source; only
    coordinate precision was trimmed. It draws in `currentColor` (streets at
    22% of it), so one mark serves the blue card, the dark panel and anything
    else without a second copy.
    """
    return (
        f'<div class="mark">{MARK_SVG}'
        f'<span class="mark__word">scooter<span class="mark__tld">.fyi</span></span>'
        f'</div>'
    )


#: The FYI speech bubble. The certificate's opening word IS the mark — it is a
#: notice, delivered to somebody who did not ask for one, and the bubble says
#: that in one glance where the letters only say it in three.
FYI_SVG = '<svg class="fyi__art" role="img" aria-label="FYI" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 109 80"><path fill-rule="evenodd" fill="currentColor" d="M 58.2 1.5 L 52.2 1.8 L 25.1 1.6 L 16.8 2 L 11.4 2.8 L 9.1 3.6 L 7.6 4.4 L 6.9 4.9 L 5 6.9 L 3.6 9.2 L 2.9 10.8 L 1.6 15.3 L 1.1 18.6 L 0.9 22.4 L 1.2 35.3 L 0.8 42.7 L 0.8 49.2 L 1.1 52.3 L 1.9 55.9 L 2.8 58.3 L 3.7 60 L 5.4 62.2 L 7.2 63.7 L 9.7 64.8 L 12.1 65.3 L 15.6 65.6 L 34.2 65.4 L 37.9 65.6 L 39.8 66 L 40.8 66.9 L 41.4 69.2 L 41.5 73.5 L 41.2 75.9 L 41.3 77.3 L 41.8 78.3 L 42.7 78.9 L 43.5 79.1 L 45.9 79.1 L 47 78.9 L 49.7 77.9 L 51.2 77 L 53.9 74.9 L 60.8 68.2 L 64.2 66 L 66.2 65.3 L 68.2 64.9 L 71 64.7 L 79.6 64.6 L 84.8 64.6 L 90 65 L 93.8 65 L 96.9 64.5 L 99.1 63.8 L 102.5 62 L 105.1 59.6 L 106.8 56.8 L 107.5 54.9 L 107.9 53.1 L 108.4 48.6 L 108.4 43.4 L 108.2 40.2 L 108.3 15.2 L 107.8 11.5 L 107.3 9.8 L 106.1 7.4 L 104.7 5.6 L 102.8 3.9 L 100.5 2.5 L 98.1 1.6 L 94.8 1 L 88.9 0.9 L 85 1.1 L 77.8 1.1 L 68 0.8 L 58.3 1.5 Z M 63.1 7.4 L 76.9 6.9 L 87.2 6.9 L 92.9 7.1 L 95.3 7.4 L 97.4 8 L 99.2 9.1 L 100.1 10.1 L 101.2 12.1 L 101.8 14 L 102.3 16.8 L 102.5 19.7 L 102.1 31.5 L 102.1 38.9 L 102.5 45.3 L 102.5 49.3 L 102.2 51.3 L 101.3 53.6 L 99.3 56 L 97 57.6 L 93.8 58.7 L 90.6 59.1 L 87.9 59.1 L 78 58.5 L 74.3 58.8 L 69.4 59.5 L 64.2 59.7 L 62 60.1 L 60.9 60.5 L 59.5 61.4 L 55.8 65.2 L 54 66.7 L 49.4 69.8 L 47.9 70.3 L 47.4 70.2 L 47 69.8 L 46.6 68.2 L 46.6 66.6 L 46.9 64.3 L 46.8 61.6 L 45.9 60.5 L 44.9 59.9 L 43.5 59.5 L 41.5 59.3 L 36 59.6 L 30.9 59.6 L 27.3 59.3 L 24.8 59.3 L 19.7 59.8 L 15.9 59.7 L 14 59.3 L 11.6 58.3 L 10.6 57.7 L 8.5 55.6 L 7.5 53.4 L 7 50.4 L 7 24.5 L 7.3 19.8 L 8.1 15.2 L 9.3 12.3 L 10 11.3 L 11.4 9.9 L 13 9 L 14.7 8.4 L 17.8 7.8 L 20.4 7.6 L 31.1 7.8 L 50.1 7.8 L 62.8 7.4 Z"/><path fill-rule="nonzero" fill="currentColor" d="M 30.9 21.7 L 29.4 33.5 L 42.6 33.5 L 41.8 39 L 28.8 39 L 26.8 54.8 L 19.8 54.8 L 24.6 16.2 L 47.1 16.2 L 46.4 21.7 Z M 65.7 39.8 L 63.9 54.8 L 56.9 54.8 L 58.8 39.8 L 48.7 16.2 L 54.9 16.2 L 55.7 16.3 L 56.8 17.1 L 62.4 32.1 L 63.1 34.8 L 65.1 31.1 L 73.8 17.2 L 74.4 16.6 L 75 16.3 L 81.6 16.2 Z M 87.7 54.8 L 80.7 54.8 L 85.4 16.2 L 92.4 16.2 Z"/></svg>'

#: See _logo(). One obvious thing to replace when the artwork changes.
MARK_SVG = '<svg class="mark__art" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="-3.8 973.2 517.7 457.8"><path fill-rule="nonzero" fill="currentColor" d="M 35.17 1000.42 C 49.78 983.01 72.76 973.22 95.41 974.06 C 212.76 974.08 330.11 974.02 447.46 974.09 C 463.52 973.75 478.95 980.85 490.77 991.43 C 504.43 1004.32 512.71 1022.83 512.71 1041.67 C 512.64 1148.35 512.75 1255.03 512.67 1361.71 C 513.85 1385.07 502.52 1409.30 482.16 1421.42 C 472.74 1427.37 461.58 1430.60 450.43 1430.37 C 454.23 1425.92 456.21 1420.37 456.65 1414.58 C 460.84 1413.23 465.27 1412.53 469.26 1410.60 C 482.86 1404.21 492.78 1391 495.77 1376.35 C 497.58 1368.92 496.77 1361.26 496.97 1353.71 C 496.81 1288.53 497.14 1223.37 496.85 1158.19 C 496.94 1155.89 496.95 1153.60 496.95 1151.30 C 496.85 1119.12 497.06 1086.94 496.93 1054.76 C 496.80 1046.73 497.61 1038.57 495.73 1030.69 C 492.53 1016.35 483.34 1003.17 470.33 996.15 C 465.34 993.75 460.11 991.79 454.69 990.64 C 449.97 989.99 445.20 990.20 440.45 990.15 C 325.43 990.20 210.42 990.21 95.40 990.15 C 79.15 989.56 62.48 995.39 50.80 1006.83 C 47.01 1010.85 43.42 1015.12 40.91 1020.07 C 37.54 1026.44 35.36 1033.53 35.15 1040.76 C 34.99 1126.76 35.32 1212.78 34.98 1298.76 C 29.41 1299.5 24.08 1301.62 19.35 1304.60 C 19.22 1217.32 19.34 1130.03 19.29 1042.73 C 19.53 1027.37 25.17 1012.12 35.17 1000.42"/><path fill-rule="nonzero" fill="currentColor" d="M 386.48 1064.78 C 394.55 1063.42 403.19 1064.60 410.25 1068.89 C 420.40 1074.25 427.72 1084.53 429.87 1095.73 C 430.52 1102.17 430.78 1108.91 428.52 1115.08 C 424.55 1127.44 413.68 1137.32 401.00 1140.12 C 382.53 1144.96 362.00 1132.57 356.61 1114.46 C 350.17 1097.23 359.38 1076.62 375.60 1068.58 C 378.99 1066.75 382.70 1065.55 386.48 1064.78"/><path fill-rule="nonzero" fill="currentColor" d="M 38.77 1361.33 C 31.08 1363.71 24.79 1370.17 22.97 1378.07 C 18.96 1391.37 29.41 1406.78 43.40 1407.42 C 51.36 1408.44 59.57 1404.89 64.22 1398.35 C 69.19 1392.37 70.07 1383.85 67.90 1376.55 C 64.35 1364.82 50.52 1357.19 38.77 1361.33 Z M 87.77 1120.07 C 89.67 1117.98 92.46 1116.67 95.33 1116.85 C 116.39 1116.87 137.45 1116.80 158.51 1116.87 C 166.39 1117.03 170.49 1126.91 166.99 1133.35 C 165.24 1136.89 161.29 1138.98 157.42 1139 C 149.32 1139.01 141.21 1138.96 133.11 1139.12 C 127.25 1160.12 120.84 1180.94 114.51 1201.80 C 107.95 1223.30 101.50 1244.82 94.72 1266.26 C 92.11 1275.62 88.74 1284.78 86.49 1294.23 C 104.14 1305.23 117.23 1322.26 126.25 1340.80 C 130.93 1348.85 133.88 1357.75 138.12 1366.03 C 190.89 1366.19 243.67 1366.03 296.44 1366.12 C 302.81 1366.12 309.17 1366.10 315.54 1365.96 C 317.94 1351.12 324.79 1337.17 334.47 1325.73 C 347.66 1310.03 367.00 1299.69 387.34 1297.25 C 402.10 1296.07 417.53 1298.21 430.55 1305.57 C 434.20 1307.58 437.20 1311.32 437.09 1315.67 C 437.56 1321.25 432.55 1325.96 427.27 1326.62 C 422.21 1327.80 418.06 1323.92 413.49 1322.58 C 397.60 1316.57 379.35 1319.51 365.03 1328.23 C 349.26 1338.08 338.31 1355.23 336.11 1373.69 C 335.75 1376.82 336.50 1380.21 334.93 1383.10 C 333.36 1386.21 330.06 1388.62 326.47 1388.48 C 261.45 1388.58 196.45 1388.48 131.42 1388.51 C 126.48 1388.96 121.91 1385.58 120.11 1381.10 C 117.05 1373.89 114.67 1366.39 110.79 1359.55 C 103.22 1343.35 93.73 1327.32 79.15 1316.48 C 76.72 1326.23 73.06 1335.60 70.50 1345.28 C 80.10 1350.91 86.47 1360.73 89.74 1371.16 C 93.12 1384.41 91.20 1399.16 83.24 1410.46 C 74.63 1423.44 58.90 1431.03 43.41 1430.33 C 31.48 1429.62 19.85 1424.17 11.86 1415.23 C -1.34 1400.89 -3.80 1378.03 5.91 1361.16 C 12.56 1349.14 24.88 1340.39 38.49 1338.33 C 42.45 1337.83 46.42 1337.46 50.42 1337.51 C 56.19 1316.35 62.92 1295.48 69.23 1274.48 C 82.92 1229.42 96.80 1184.37 110.04 1139.19 C 103.58 1138.32 96.60 1140.42 90.51 1137.58 C 84.00 1134.60 82.78 1124.94 87.77 1120.07"/><path fill-rule="nonzero" fill="currentColor" d="M 387.94 1360.5 C 372.14 1364.78 365.73 1387.94 377.79 1399.35 C 385.35 1408.01 399.77 1409 408.74 1402 C 416.21 1396.83 419.29 1386.89 417.75 1378.19 C 416.12 1372.10 412.54 1366.32 407.05 1363.01 C 401.44 1359.41 394.30 1358.78 387.94 1360.5 Z M 388.92 1337.41 C 400.52 1335.94 412.74 1338.87 421.88 1346.33 C 431.04 1353.12 437.55 1363.39 439.57 1374.64 C 441.23 1384.94 440.16 1396.03 434.74 1405.14 C 428.54 1417.03 416.70 1425.76 403.54 1428.32 C 391.22 1430.83 377.91 1427.83 367.79 1420.39 C 358.70 1413.91 352.64 1403.73 350.13 1392.96 C 348.13 1382.26 349.26 1370.66 354.76 1361.12 C 361.42 1348.30 374.47 1338.94 388.92 1337.41"/><path fill-rule="nonzero" fill="currentColor" opacity=".22" d="M 206.66 1232.12 C 202.07 1233 198.01 1236.17 195.75 1240.21 C 194.40 1243.5 194.62 1247.26 195.23 1250.69 C 196.50 1254.91 199.86 1258.35 203.69 1260.42 C 211.82 1264.80 223.11 1258.82 224.66 1249.87 C 225.89 1244.39 223.21 1238.62 219.18 1234.98 C 215.82 1231.89 210.96 1231.60 206.66 1232.12 Z M 378.48 1203.64 C 381.86 1202.16 385.21 1200.60 388.43 1198.78 C 385.09 1195.69 381.58 1192.78 378.03 1189.92 C 377.69 1194.5 378.05 1199.07 378.48 1203.64 Z M 261.72 1122.73 C 259.69 1127.03 258.23 1131.55 256.70 1136.03 C 252.63 1146.67 248.67 1157.33 244.82 1168.05 C 238.47 1185.94 231.57 1203.66 225.56 1221.67 C 228.93 1224.94 233.06 1227.66 235.14 1232.01 C 237.69 1236.42 238.40 1241.5 239.59 1246.37 C 248.10 1248.46 255.95 1252.53 264.40 1254.83 C 268.81 1256.03 272.98 1258.05 277.47 1258.98 C 281.40 1258.01 284.72 1255.35 288.39 1253.67 C 308.62 1242.14 329.07 1231.01 349.41 1219.71 C 353.48 1217.41 358.02 1215.89 361.77 1213.03 C 362.38 1210.66 362.14 1208.17 362.20 1205.73 C 362.02 1196.03 361.90 1186.30 362.75 1176.62 C 356.13 1170.85 349.84 1164.48 342.38 1159.78 C 324.58 1151.07 306.39 1143.17 288.53 1134.57 C 279.49 1130.85 270.87 1126.17 261.72 1122.73 Z M 293.32 991.38 C 298.91 991.27 304.52 990.93 310.11 991.36 C 295.79 1030.03 281.13 1068.60 267.26 1107.44 C 280.73 1113.82 294.34 1119.87 307.83 1126.21 C 321.51 1132.35 334.87 1139.26 348.87 1144.69 C 351.53 1146.51 353.52 1149.16 356.23 1150.94 C 366.41 1157.94 375.01 1166.87 384.44 1174.78 C 390.74 1180.21 396.49 1186.37 403.49 1190.94 C 427.22 1181.16 450.53 1170.32 474.30 1160.60 C 481.96 1157.76 488.99 1153.37 496.95 1151.30 C 496.54 1157.12 496.13 1162.92 495.72 1168.73 C 492.95 1170.66 489.54 1171.32 486.48 1172.67 C 466.59 1181.17 446.75 1189.80 426.83 1198.26 C 418.19 1202.39 408.96 1205.21 400.63 1209.98 C 393.25 1214.44 385.54 1218.33 377.97 1222.44 C 377.70 1234.37 377.44 1246.28 377.18 1258.21 C 372.91 1257.91 368.64 1257.60 364.37 1257.30 C 363.51 1248.44 362.65 1239.57 361.79 1230.71 C 357.51 1232.71 353.75 1235.62 349.62 1237.89 C 331.99 1247.05 314.83 1257.07 297.39 1266.60 C 295.38 1267.94 292.75 1268.69 291.32 1270.69 C 281.11 1300.28 269.97 1329.55 259.64 1359.10 C 254.28 1358.85 248.92 1358.60 243.57 1358.35 C 253.45 1330.37 263.26 1302.37 273.16 1274.39 C 260.25 1270.57 247.91 1264.92 234.81 1261.73 C 231.48 1267.85 225.39 1271.82 219.08 1274.35 C 214.76 1276.10 210.02 1275.57 205.48 1275.57 C 195.17 1303.37 184.61 1331.10 174.50 1359 C 169.02 1358.75 163.54 1358.5 158.07 1358.25 C 160.81 1353.19 162.46 1347.66 164.35 1342.26 C 173.51 1317.85 182.05 1293.21 191.26 1268.83 C 183.75 1262.17 179.19 1251.33 182.24 1241.42 C 175.80 1237.23 168.29 1235.14 161.59 1231.42 C 164.98 1227.98 168.38 1224.53 171.78 1221.10 C 177.82 1220.85 182.51 1225.42 188.22 1226.48 C 191.93 1224.83 194.74 1221.53 198.61 1220.10 C 202.04 1218.23 206.15 1218.48 209.67 1217.05 C 212.46 1211.46 214.10 1205.41 216.29 1199.57 C 226.61 1171.66 236.80 1143.69 247.21 1115.80 C 235.28 1109.96 223.23 1104.32 211.04 1099.05 C 198.40 1093.51 186.29 1086.75 173.35 1081.94 C 170.55 1091.19 166.12 1099.82 163.44 1109.12 C 158.50 1108.80 153.57 1108.48 148.63 1108.16 C 151.40 1097.14 155.81 1086.62 159.10 1075.78 C 128.14 1061.14 97.16 1046.53 66.17 1031.94 C 61.00 1029.53 46.07 1022.5 40.91 1020.07 C 43.42 1015.12 47.01 1010.85 50.80 1006.83 C 82.53 1022.67 114.95 1037.08 147.06 1052.12 C 152.69 1055.03 158.47 1057.64 164.45 1059.73 C 172.42 1036.92 180.39 1014.12 188.36 991.33 C 193.76 990.99 199.17 991.00 204.57 991.39 C 196.20 1016.64 187.83 1041.89 179.46 1067.14 C 193.81 1073.57 207.94 1080.48 222.36 1086.76 C 232.37 1091.71 242.59 1096.21 252.83 1100.69 C 255.28 1091.73 259.53 1083.42 262.37 1074.60 C 272.74 1046.87 282.71 1019.01 293.32 991.38"/><path fill-rule="nonzero" fill="currentColor" opacity=".22" d="M 454.69 990.64 C 460.11 991.79 465.34 993.75 470.33 996.15 C 467.00 1001.12 464.55 1006.60 461.58 1011.78 C 453.10 1028.07 444.56 1044.32 436.13 1060.64 C 433.74 1065.78 431.33 1070.94 428.03 1075.58 C 424.47 1072.12 420.55 1068.80 417.86 1064.60 C 418.06 1060.28 420.83 1056.62 422.63 1052.82 C 433.17 1032.01 444.30 1011.51 454.69 990.64"/></svg>'
