"""Admin SMS watch on one vehicle (docs/FLEET_REPORTS_PLAN.md §4.1(5)).

An admin subscribes to a single vehicle's movement and state changes and is
texted through src/comms.py. Wiring, not new machinery: the per-cycle shape,
the "checked out" reading and the send-failure taxonomy are dibs_watch's.

WHO GETS TEXTED. The /admin pages are GitHub-OAuth sessions with no rider
account, and a verified phone lives on a rider account. So a watch names
the admin-allowlisted ACCOUNT whose verified phone receives the texts, plus
the GitHub login that created it. Subscribing requires:
  * the account's email on the admin allowlist (sql/021) — a watch can only
    ever text an admin;
  * `accounts.phone_verified_at` (sql/045) — a typed, unproved number is
    never texted, exactly dibs_watch's rule;
  * not `accounts.sms_opted_out_at` (sql/046, the STOP mirror);
  * an explicit consent tick on the form, stored as `consent_at`.
The first text is a confirmation naming the vehicle, the expiry and STOP,
so the phone's owner hears about the watch before any alert.

CONSENT, QUOTA, STOP. comms refuses an opted-out number with 409 across
every application on the shared sender; that ends the watch
(`opted_out`), as does a number comms cannot use (`unusable`). Our own
quota is per watch: at most MAX_TEXTS_PER_WATCH texts, then the watch ends
(`cap_reached`); every change seen in one cycle goes out as ONE text; and a
watch expires (`expires_at`, at most MAX_WATCH_HOURS). A transient comms
failure (quota, outage) drops that one alert — an admin watch is a nudge,
not a ledger — and the next change texts again.

WHAT COUNTS AS A CHANGE. Leaving / rejoining the feed, a rental starting /
ending (`is_reserved`), `is_disabled` flipping, and a move of more than
MOVE_RADIUS_M while not in a rental (positions during a rental are the
rider's, and would text every two minutes). The first cycle after
subscribing records the baseline silently.

ONE ROW, NO LOCK ACROSS THE GATEWAY. The state update and the texts_sent
increment are committed BEFORE the send, guarded on the old texts_sent, so
an overlapping cycle cannot text the same change twice; the idempotency key
names the watch and the text number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from .comms import CommsError, OptedOut, QuotaExceeded, UnusableRecipient, send_sms
from .geo import distance_meters
from .ingest import TaggedDevice
from .pg import connection

log = logging.getLogger(__name__)

MAX_WATCH_HOURS = 7 * 24
DEFAULT_WATCH_HOURS = 24
MAX_TEXTS_PER_WATCH = 20
#: Same 50 m as device_state's JITTER_RADIUS_M: past it, a non-rental
#: position change is a move, not GPS noise.
MOVE_RADIUS_M = 50.0
#: At most this many live watches per admin account — a page that can text
#: is a page that can be misused, even by an admin.
MAX_LIVE_WATCHES_PER_ACCOUNT = 10

END_EXPIRED = "expired"
END_UNSUBSCRIBED = "unsubscribed"
END_OPTED_OUT = "opted_out"
END_UNUSABLE = "unusable"
END_NO_PHONE = "no_phone"
END_UNVERIFIED = "unverified"
END_CAP = "cap_reached"


class WatchError(Exception):
    """A refused subscription; str(e) is safe to show the admin."""


@dataclass(frozen=True)
class Observed:
    in_feed: bool
    reserved: bool
    disabled: bool
    lat: float | None
    lon: float | None


def observe(device: TaggedDevice | None) -> Observed:
    if device is None:
        return Observed(False, False, False, None, None)
    return Observed(True, device.is_reserved is True, device.is_disabled is True,
                    device.lat, device.lon)


def changes(prev: dict[str, Any], now: Observed) -> list[str]:
    """Human-readable changes from the stored state to this cycle's. Pure."""
    out: list[str] = []
    if prev["in_feed"] is None:
        return out  # baseline cycle
    if prev["in_feed"] and not now.in_feed:
        out.append("left the feed")
    elif not prev["in_feed"] and now.in_feed:
        out.append("is back in the feed")
    if now.in_feed:
        if bool(prev["reserved"]) != now.reserved:
            out.append("rental started" if now.reserved else "rental ended")
        if bool(prev["disabled"]) != now.disabled:
            out.append("marked disabled" if now.disabled else "no longer disabled")
        if (not now.reserved and prev["lat"] is not None and now.lat is not None
                and distance_meters(prev["lat"], prev["lon"], now.lat, now.lon)
                > MOVE_RADIUS_M):
            d = distance_meters(prev["lat"], prev["lon"], now.lat, now.lon)
            out.append(f"moved {int(round(d))} m")
    return out


def alert_text(vehicle_name: str, what: list[str], watch_id: int) -> str:
    return (f"Watch #{watch_id}: {vehicle_name} " + "; ".join(what)
            + ". Reply STOP to stop all texts.")


def confirmation_text(vehicle_name: str, expires_at: datetime) -> str:
    return (f"You are now watching {vehicle_name} for movement and state "
            f"changes until {expires_at:%Y-%m-%d %H:%M} UTC. Reply STOP to stop "
            "all texts.")


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe (called from the /admin pages)
# ---------------------------------------------------------------------------

def subscribe(*, vehicle_identifier: str, account_email: str, login: str,
              hours: int, consent: bool) -> dict[str, Any]:
    """Create a watch and send its confirmation text. Raises WatchError."""
    from . import vehicle_identity
    from .accounts import admin_emails, normalize_email

    if not consent:
        raise WatchError("tick the consent box: this sends SMS to that account's phone")
    if not 1 <= hours <= MAX_WATCH_HOURS:
        raise WatchError(f"hours must be 1..{MAX_WATCH_HOURS}")
    try:
        email = normalize_email(account_email)
    except Exception:  # noqa: BLE001
        raise WatchError("not an email address")
    with connection() as conn:
        with conn.cursor() as cur:
            if email not in admin_emails(cur):
                raise WatchError("that email is not on the admin allowlist")
            cur.execute(
                "SELECT id, phone_number, phone_verified_at, sms_opted_out_at "
                "FROM accounts WHERE lower(email) = %s ORDER BY id LIMIT 1",
                (email,),
            )
            acct = cur.fetchone()
            if acct is None:
                raise WatchError("no account with that email — sign in once on the app")
            account_id, phone, verified, opted_out = acct
            if not phone:
                raise WatchError("that account has no phone number")
            if verified is None:
                raise WatchError("that account's phone is not verified (sign in by text once)")
            if opted_out is not None:
                raise WatchError("that phone has texted STOP; reply UNSTOP to resume")
            cur.execute("SELECT vehicle_plate FROM device_state WHERE vehicle_identifier = %s",
                        (vehicle_identifier,))
            st = cur.fetchone()
            if st is None:
                raise WatchError("unknown vehicle")
            cur.execute(
                "SELECT COUNT(*) FROM admin_device_watches "
                "WHERE account_id = %s AND ended_at IS NULL AND expires_at > NOW()",
                (account_id,),
            )
            if int(cur.fetchone()[0]) >= MAX_LIVE_WATCHES_PER_ACCOUNT:
                raise WatchError(f"at most {MAX_LIVE_WATCHES_PER_ACCOUNT} live watches per account")
            cur.execute(
                """
                INSERT INTO admin_device_watches (
                    vehicle_identifier, created_by_login, account_id, consent_at,
                    expires_at
                ) VALUES (%s, %s, %s, NOW(), NOW() + make_interval(hours => %s))
                RETURNING id, expires_at
                """,
                (vehicle_identifier, login, account_id, hours),
            )
            watch_id, expires_at = cur.fetchone()
        conn.commit()
    name = vehicle_identity.display_name(vehicle_identifier, st[0])
    try:
        send_sms(phone, confirmation_text(name, expires_at),
                 idempotency_key=f"admin-watch:{watch_id}:start",
                 metadata={"kind": "admin_watch_start", "watch_id": watch_id})
    except OptedOut as e:
        _end(watch_id, END_OPTED_OUT)
        raise WatchError(str(e))
    except UnusableRecipient:
        _end(watch_id, END_UNUSABLE)
        raise WatchError("comms cannot deliver to that number")
    except CommsError as e:
        # Not configured, quota, outage: no watch that cannot text.
        _end(watch_id, END_UNSUBSCRIBED, login="system")
        raise WatchError(f"could not send the confirmation text: {e}")
    log.info("admin watch %d on %s by %s → account %d until %s", watch_id,
             vehicle_identifier, login, account_id, expires_at)
    return {"id": int(watch_id), "expires_at": expires_at}


def unsubscribe(watch_id: int, *, login: str, account_id: int | None = None) -> bool:
    """End a live watch. False when there is no live watch to end.

    `account_id` scopes the update to the watch on THAT account's phone, and
    the account-session API passes it so one admin cannot stop a watch texting
    another admin's phone. The /admin pages pass nothing and keep their
    administrator-wide reach: that surface is the full desk, and an operator
    sitting at it may need to stop a watch for a colleague who has gone home.
    """
    where = "WHERE id = %s AND ended_at IS NULL"
    params: list[Any] = [END_UNSUBSCRIBED, login, watch_id]
    if account_id is not None:
        where += " AND account_id = %s"
        params.append(account_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_device_watches SET ended_at = NOW(), ended_reason = %s, "
                f"ended_by_login = %s {where}",
                params,
            )
            changed = cur.rowcount
        conn.commit()
    return bool(changed)


def _end(watch_id: int, reason: str, *, login: str | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_device_watches SET ended_at = NOW(), ended_reason = %s, "
                "ended_by_login = %s WHERE id = %s AND ended_at IS NULL",
                (reason, login, watch_id),
            )
        conn.commit()


def list_watches(cur, vehicle_identifier: str | None = None,
                 include_ended: bool = True, limit: int = 200) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if vehicle_identifier:
        where.append("w.vehicle_identifier = %s")
        params.append(vehicle_identifier)
    if not include_ended:
        where.append("w.ended_at IS NULL AND w.expires_at > NOW()")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(
        f"""
        SELECT w.id, w.vehicle_identifier, w.created_by_login, w.account_id,
               a.public_username, w.created_at, w.expires_at, w.ended_at,
               w.ended_reason, w.ended_by_login, w.texts_sent, w.last_texted_at,
               w.last_event, (w.ended_at IS NULL AND w.expires_at > NOW())
          FROM admin_device_watches w
          LEFT JOIN accounts a ON a.id = w.account_id
          {sql_where}
         ORDER BY (w.ended_at IS NULL AND w.expires_at > NOW()) DESC, w.created_at DESC
         LIMIT %s
        """,
        (*params, limit),
    )
    cols = ("id", "vehicle_identifier", "created_by_login", "account_id",
            "public_username", "created_at", "expires_at", "ended_at", "ended_reason",
            "ended_by_login", "texts_sent", "last_texted_at", "last_event", "live")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# The per-cycle pass
# ---------------------------------------------------------------------------

def watch_for_cycle(snapshot_time: datetime, devices: Iterable[TaggedDevice]) -> dict[str, int]:
    from . import vehicle_identity

    observed = {d.vehicle_identifier: d for d in devices if d.vehicle_identifier}
    stats = {"live": 0, "texted": 0, "ended": 0, "deferred": 0}
    with connection() as conn:
        with conn.cursor() as cur:
            # Expire first: an expired watch never texts again.
            cur.execute(
                "UPDATE admin_device_watches SET ended_at = NOW(), ended_reason = %s "
                "WHERE ended_at IS NULL AND expires_at <= NOW()",
                (END_EXPIRED,),
            )
            stats["ended"] += cur.rowcount
            cur.execute(
                """
                SELECT w.id, w.vehicle_identifier, w.last_in_feed, w.last_reserved,
                       w.last_disabled, w.last_lat, w.last_lon, w.texts_sent,
                       a.phone_number, a.phone_verified_at, a.sms_opted_out_at,
                       ds.vehicle_plate
                  FROM admin_device_watches w
                  LEFT JOIN accounts a ON a.id = w.account_id
                  LEFT JOIN device_state ds ON ds.vehicle_identifier = w.vehicle_identifier
                 WHERE w.ended_at IS NULL
                """
            )
            rows = cur.fetchall()
        conn.commit()
    stats["live"] = len(rows)
    for (wid, vid, in_feed, reserved, disabled, lat, lon, sent, phone, verified,
         opted_out, plate) in rows:
        if not phone:
            _end(wid, END_NO_PHONE)
            stats["ended"] += 1
            continue
        if verified is None:
            _end(wid, END_UNVERIFIED)
            stats["ended"] += 1
            continue
        if opted_out is not None:
            _end(wid, END_OPTED_OUT)
            stats["ended"] += 1
            continue
        now = observe(observed.get(vid))
        prev = {"in_feed": in_feed, "reserved": reserved, "disabled": disabled,
                "lat": lat, "lon": lon}
        what = changes(prev, now)
        # Positions are only the baseline while NOT in a rental, so the
        # release point is compared with where it was parked.
        keep_pos = now.reserved or not now.in_feed
        new_lat = lat if keep_pos else now.lat
        new_lon = lon if keep_pos else now.lon
        texting = bool(what) and sent < MAX_TEXTS_PER_WATCH
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE admin_device_watches
                       SET last_in_feed = %s, last_reserved = %s, last_disabled = %s,
                           last_lat = %s, last_lon = %s,
                           last_seen_at = CASE WHEN %s THEN %s ELSE last_seen_at END,
                           texts_sent = texts_sent + %s,
                           last_texted_at = CASE WHEN %s THEN NOW() ELSE last_texted_at END,
                           last_event = COALESCE(%s, last_event)
                     WHERE id = %s AND texts_sent = %s AND ended_at IS NULL
                    """,
                    (now.in_feed, now.reserved, now.disabled, new_lat, new_lon,
                     now.in_feed, snapshot_time, 1 if texting else 0, texting,
                     "; ".join(what) if what else None, wid, sent),
                )
                claimed = cur.rowcount == 1
            conn.commit()
        if not (claimed and texting):
            continue
        name = vehicle_identity.display_name(vid, plate)
        try:
            send_sms(phone, alert_text(name, what, wid),
                     idempotency_key=f"admin-watch:{wid}:{sent + 1}",
                     metadata={"kind": "admin_watch", "watch_id": wid})
            stats["texted"] += 1
        except OptedOut:
            _end(wid, END_OPTED_OUT)
            stats["ended"] += 1
            continue
        except UnusableRecipient:
            _end(wid, END_UNUSABLE)
            stats["ended"] += 1
            continue
        except (QuotaExceeded, CommsError) as e:
            log.warning("admin watch %d alert dropped: %s", wid, e)
            stats["deferred"] += 1
        if sent + 1 >= MAX_TEXTS_PER_WATCH:
            _end(wid, END_CAP)
            stats["ended"] += 1
    return stats
