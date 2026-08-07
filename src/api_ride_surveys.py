"""Screen 9 end-ride survey (PLAN_RIDE_MODE_API.md phase A3, sql/052).

    POST /api/v1/tracked-rides/{ride_id}/survey   submit the ride's survey (single-shot)

Namespaced under /api/v1/tracked-rides (not /api/v1/rides — the separate
off-feed ride tracker, untouched by this program), the same
sub-resource-lives-in-its-own-router-file precedent as
src/api_ride_screenshots.py's /screenshots endpoints.

The survey is the source of THREE awards (src/points.py):

    ride_survey               4 pts  — scooter-feedback pane submitted,
                                        gated on ride_options.end_survey
                                        and not an own-device ride
    nav_route_feedback        4 pts  — a route rating tied to a resolved
                                        ride_routes row
    nav_qualitative_feedback  6 pts  — >=20 chars of trimmed free text

Every gate is read HERE, off the ride's own ride_options and the survey
payload — src/points.py's credit_* functions are only the formula + ledger
write, same division of labor as A2's credit_battery_contribution /
credit_nav_distance_bonus.

ride_route_id LINKING: when the survey names a ride_routes row, submitting
the survey is what stamps that row's tracked_ride_id to THIS ride (Screen 4
runs before Screen 6 start, so the row predates the ride it's about). The
row must be caller-owned and either unlinked or already linked to this same
ride, else 422 — this is what stops one stored route from being replayed
across multiple rides' surveys for repeat nav_route_feedback awards, and it
is also why a de-identified or simply made-up id fails exactly the same way
(a de-identified row's account_id is NULL, so the ownership predicate alone
already excludes it — no separate "is this stale" check is needed).

THE COSMO BASKET ANSWER IS ALSO A DEVICE-FEATURE REPORT (sql/065): a survey
carrying model_bonus.cosmo_front_basket additionally writes one row to
device_feature_reports — has_basket only, abstaining (NULL) on the three
features the survey never asked about — for src/device_features.py's
ten-minute processor to fold into the map's crowdsourced consensus. The
ride itself is the proof of presence (this handler only accepts the key on
a ride whose server-stamped vehicle_model is Cosmo), so the row carries
plate_valid=true with no plate, and source='ride_survey' so the audit trail
can tell it from a modal confirmation. It earns no device-feature points —
the survey's own ride_survey award already pays for this answer, and paying
twice for one tap would be a faucet. Abstentions mean the report can only
ever agree, disagree, or fill in on the BASKET: it cannot flip a vehicle
into needs_review over a bell it said nothing about.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .accounts import SessionUser, require_session
from .pg import connection
from .points import (
    credit_nav_qualitative_feedback,
    credit_nav_route_feedback,
    credit_ride_survey,
)

router = APIRouter()

# The exact 16-item vocabulary (PLAN_RIDE_MODE_API.md phase A3 / Screen 9's
# "IF no — what wasn't?" checklist). Anything outside this set is 422.
ISSUE_VOCABULARY = (
    "app_veo", "acceleration", "basket", "battery", "bell", "brakes",
    "connectivity", "customer_service", "dirty", "kickstand", "pedals",
    "phone_holder", "price", "speedometer", "scooterfyi_issue", "vandalized",
)

# model_bonus key -> the vehicle_model it requires. Astro/Cosmo/Apollo,
# capitalized exactly as src/ingest.py:_KNOWN_VEHICLE_TYPES app_name and
# device_state.current_vehicle_model_name already store them.
_MODEL_BONUS_KEYS: dict[str, str] = {
    "cosmo_front_basket": "Cosmo",
    "apollo_top_speed_mph": "Apollo",
    "astro_landscape_holder": "Astro",
}
_APOLLO_TOP_SPEED_MAX_MPH = 40

# Minimum post-trim length of nav_qualitative for the qualitative-feedback
# award. "Meaningful" is not machine-checkable; this is the whole check.
NAV_QUALITATIVE_MIN_CHARS = 20


class SurveyIn(BaseModel):
    # model_bonus collides with pydantic's "model_" protected-namespace
    # heuristic (it thinks we're shadowing a pydantic model_* method) —
    # it isn't; sql/052's column is named model_bonus and that name is
    # not renegotiable.
    model_config = ConfigDict(protected_namespaces=())

    # --- Left pane: Scooter Feedback -------------------------------------
    would_ride_again: bool | None = None
    was_perfect: bool | None = None
    issues: list[str] = Field(default_factory=list)
    model_bonus: dict[str, Any] = Field(default_factory=dict)
    # --- Right pane: Navigation Feedback ----------------------------------
    nav_route_rating: int | None = Field(default=None, ge=1, le=10)
    nav_deviated: bool | None = None
    nav_deviated_needs_improvement: bool | None = None
    nav_nps: int | None = Field(default=None, ge=0, le=10)
    # sql/052: TEXT, api-enforced 2000 char cap (there is no column CHECK).
    nav_qualitative: str | None = Field(default=None, max_length=2000)
    ride_route_id: UUID | None = None


def _parse_ride_id(ride_id: str) -> UUID:
    try:
        return UUID(ride_id)
    except ValueError:
        raise HTTPException(400, "ride id must be a UUID")


def _validate_issues(issues: list[str]) -> None:
    bad = [i for i in issues if i not in ISSUE_VOCABULARY]
    if bad:
        raise HTTPException(422, {
            "error": "bad_issue",
            "detail": f"issues outside the known vocabulary: {bad}",
        })


def _validate_model_bonus(model_bonus: dict[str, Any], vehicle_model: str | None) -> None:
    """Every key in `model_bonus` must belong to the STAMPED vehicle_model
    (server-side, from device_state — never the client's own claim), and
    carry the right type/bounds for that key. A key present when the model
    is NULL (unconfirmed) or a different model is 422, same as an unknown
    key — both mean "this survey is claiming something about a device
    fact we did not verify"."""
    for key, value in model_bonus.items():
        required_model = _MODEL_BONUS_KEYS.get(key)
        if required_model is None:
            raise HTTPException(422, {
                "error": "bad_model_bonus",
                "detail": f"unknown model_bonus key: {key!r}",
            })
        if vehicle_model != required_model:
            raise HTTPException(422, {
                "error": "bad_model_bonus",
                "detail": f"model_bonus.{key} requires this ride's stamped "
                          f"vehicle_model to be {required_model!r}, not {vehicle_model!r}",
            })
        if key == "apollo_top_speed_mph":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(422, {
                    "error": "bad_model_bonus",
                    "detail": "model_bonus.apollo_top_speed_mph must be numeric",
                })
            if not (0 <= value <= _APOLLO_TOP_SPEED_MAX_MPH):
                raise HTTPException(422, {
                    "error": "bad_model_bonus",
                    "detail": f"model_bonus.apollo_top_speed_mph must be between "
                              f"0 and {_APOLLO_TOP_SPEED_MAX_MPH}",
                })
        else:  # cosmo_front_basket / astro_landscape_holder: bool
            if not isinstance(value, bool):
                raise HTTPException(422, {
                    "error": "bad_model_bonus",
                    "detail": f"model_bonus.{key} must be true or false",
                })


def _vehicle_state_for(
    cur, vehicle_identifier: str | None,
) -> tuple[str | None, str | None]:
    """(current_vehicle_model_name, feature_status) from device_state for
    the ride's vehicle — (None, None) for a vehicle the feed never showed.

    The model (sql/016) is Astro/Cosmo/Apollo capitalized per
    src/ingest.py:_KNOWN_VEHICLE_TYPES, or None for an unconfirmed model —
    the same source src/api_tracked_rides.py's track-donation handler stamps
    onto track_donations.vehicle_model at donation time (A2); read fresh
    here rather than off that row because a survey can be submitted before
    the ride's track is ever donated. feature_status rides along for the
    basket report's status_at_report (sql/055's audit rule: record the
    status that was live when the report landed, because the vehicle will
    have moved on by the time anyone reads the ledger)."""
    if vehicle_identifier is None:
        return None, None
    cur.execute(
        "SELECT current_vehicle_model_name, feature_status FROM device_state "
        "WHERE vehicle_identifier = %s",
        (vehicle_identifier,),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _file_basket_feature_report(
    cur, *, vehicle_identifier: str, account_id: int,
    has_basket: bool, issues: list[str], feature_status: str | None,
) -> None:
    """One basket-only row into device_feature_reports (sql/065).

    Abstains (NULL) on bell/cup_holder/phone_holder and the plate — the
    survey never asked about any of them. Condition IS carried: a rider who
    says the basket exists and lists `basket` among the ride's issues has
    reported a present-but-poor basket, the same claim the modal's
    poor_condition checklist makes. points_awarded stays 0 by design (the
    ride_survey award already covers this answer)."""
    poor = ["basket"] if (has_basket and "basket" in issues) else []
    cur.execute(
        """
        INSERT INTO device_feature_reports (
            vehicle_identifier, account_id, submitted_plate, plate_valid,
            has_bell, has_cup_holder, has_phone_holder, has_basket,
            all_good_condition, poor_condition, status_at_report, source
        ) VALUES (%s, %s, NULL, TRUE, NULL, NULL, NULL, %s, %s, %s, %s,
                  'ride_survey')
        """,
        (
            vehicle_identifier, account_id, has_basket, not poor, poor,
            feature_status or "needs_features_confirmed",
        ),
    )


def _survey_response(
    *, survey_id, ride_id: UUID, vehicle_model: str | None, payload: SurveyIn,
    ride_route_id: UUID | None, created_at, points_awarded: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": str(survey_id),
        "ride_id": str(ride_id),
        "vehicle_model": vehicle_model,
        "would_ride_again": payload.would_ride_again,
        "was_perfect": payload.was_perfect,
        "issues": payload.issues,
        "model_bonus": payload.model_bonus,
        "nav_route_rating": payload.nav_route_rating,
        "nav_deviated": payload.nav_deviated,
        "nav_deviated_needs_improvement": payload.nav_deviated_needs_improvement,
        "nav_nps": payload.nav_nps,
        "nav_qualitative": payload.nav_qualitative,
        "ride_route_id": str(ride_route_id) if ride_route_id else None,
        "created_at": created_at.isoformat(),
        "points": points_awarded,
    }


@router.post("/api/v1/tracked-rides/{ride_id}/survey")
def submit_ride_survey(
    ride_id: str,
    user: SessionUser = Depends(require_session),
    payload: SurveyIn = Body(...),
) -> dict[str, Any]:
    rid = _parse_ride_id(ride_id)

    # Pure-client validation first, before a connection is taken — same
    # rule src/api_tracked_rides.py:_serialize_ride_options follows: a
    # malformed vocabulary item is a client bug, not a reason to hold a
    # pooled connection open.
    _validate_issues(payload.issues)

    with connection() as conn:
        with conn.cursor() as cur:
            # SELECT ... FOR UPDATE on the ride row, mirroring PATCH
            # .../end's idiom: this is what makes the single-shot check
            # below race-safe — a concurrent double-POST serializes behind
            # this lock and the second request observes the first's
            # already-inserted survey row, so it 409s cleanly instead of
            # surfacing ride_surveys' UNIQUE(tracked_ride_id) as a 500.
            cur.execute(
                "SELECT user_reported_ended_at, vehicle_identifier, ride_options, "
                "start_lat, start_lon FROM tracked_rides "
                "WHERE id = %s AND account_id = %s FOR UPDATE",
                (str(rid), user.account_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "no such ride")
            (user_reported_ended_at, vehicle_identifier, ride_options,
             start_lat, start_lon) = row

            if user_reported_ended_at is None:
                raise HTTPException(409, {
                    "error": "ride_not_ended",
                    "detail": "report this ride's end (PATCH .../end) before surveying it",
                })

            cur.execute(
                "SELECT 1 FROM ride_surveys WHERE tracked_ride_id = %s",
                (str(rid),),
            )
            if cur.fetchone() is not None:
                raise HTTPException(409, {
                    "error": "survey_already_submitted",
                    "detail": "this ride's survey has already been submitted",
                })

            # vehicle_model is stamped SERVER-SIDE — never the client's own
            # claim — which is what makes model_bonus's keys trustworthy
            # enough to gate an award on later.
            vehicle_model, feature_status = _vehicle_state_for(cur, vehicle_identifier)
            _validate_model_bonus(payload.model_bonus, vehicle_model)

            # ride_route_id linking. None is the normal case for a survey
            # with no stored route (private/off-nav ride, or nav_improvement
            # was off) — nothing to resolve, nothing to link.
            ride_route_id: UUID | None = payload.ride_route_id
            if ride_route_id is not None:
                # FOR UPDATE: without it, two concurrent surveys for two
                # DIFFERENT rides naming the same ride_route_id can both
                # observe tracked_ride_id IS NULL, both treat the row as
                # theirs to claim (and both award route-dependent points
                # below), and then race on the UPDATE — the last writer
                # wins the link while the loser keeps points for a link
                # that no longer exists. Locking the row serializes the
                # second transaction behind the first's commit, so it then
                # sees the FRESH tracked_ride_id and correctly 422s as
                # "already linked to a different ride" instead of racing.
                cur.execute(
                    "SELECT tracked_ride_id FROM ride_routes WHERE id = %s AND account_id = %s "
                    "FOR UPDATE",
                    (str(ride_route_id), user.account_id),
                )
                route_row = cur.fetchone()
                # Not found covers three cases identically, by design: the
                # id does not exist, it belongs to another account, or it
                # has been de-identified (account_id nulled by the 28h
                # sweep) — a stale id and a guessed one are
                # indistinguishable, and both fail the same ownership test.
                if route_row is None:
                    raise HTTPException(422, {
                        "error": "bad_ride_route_id",
                        "detail": "ride_route_id does not resolve to a route this "
                                  "account owns",
                    })
                (linked_ride_id,) = route_row
                if linked_ride_id is not None and str(linked_ride_id) != str(rid):
                    raise HTTPException(422, {
                        "error": "bad_ride_route_id",
                        "detail": "ride_route_id is already linked to a different ride",
                    })
                if linked_ride_id is None:
                    # Submitting the survey is what stamps the link —
                    # idempotent no-op if it already equals this ride.
                    cur.execute(
                        "UPDATE ride_routes SET tracked_ride_id = %s WHERE id = %s",
                        (str(rid), str(ride_route_id)),
                    )

            cur.execute(
                """
                INSERT INTO ride_surveys (
                    tracked_ride_id, account_id, vehicle_model,
                    would_ride_again, was_perfect, issues, model_bonus,
                    nav_route_rating, nav_deviated, nav_deviated_needs_improvement,
                    nav_nps, nav_qualitative, ride_route_id
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (str(rid), user.account_id, vehicle_model,
                 payload.would_ride_again, payload.was_perfect,
                 json.dumps(payload.issues), json.dumps(payload.model_bonus),
                 payload.nav_route_rating, payload.nav_deviated,
                 payload.nav_deviated_needs_improvement, payload.nav_nps,
                 payload.nav_qualitative,
                 str(ride_route_id) if ride_route_id is not None else None),
            )
            survey_id, created_at = cur.fetchone()

            # The basket answer doubles as a device-feature report (see the
            # module docstring). Reachable only when _validate_model_bonus
            # passed with the key present, which requires the server-stamped
            # model to be Cosmo — so vehicle_identifier is real and in
            # device_state. Same transaction as the survey row: the two are
            # one statement by the rider and must not exist without each
            # other.
            if "cosmo_front_basket" in payload.model_bonus:
                _file_basket_feature_report(
                    cur, vehicle_identifier=vehicle_identifier,
                    account_id=user.account_id,
                    has_basket=bool(payload.model_bonus["cosmo_front_basket"]),
                    issues=payload.issues, feature_status=feature_status,
                )

            # --- Award wiring. Every gate is read HERE, off ride_options
            # and the survey payload, per the module docstring. ------------
            points_awarded: list[dict[str, Any]] = []

            scooter_feedback_present = (
                payload.would_ride_again is not None
                or payload.was_perfect is not None
                or bool(payload.issues)
                or bool(payload.model_bonus)
            )
            end_survey_on = (
                isinstance(ride_options, dict) and ride_options.get("end_survey") is True
            )
            own_device = (
                isinstance(ride_options, dict) and ride_options.get("own_device") is True
            )
            if scooter_feedback_present and end_survey_on and not own_device:
                award = credit_ride_survey(
                    cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                    lat=start_lat, lng=start_lon, ride_id=str(rid),
                )
                if award is not None:
                    points_awarded.append({"action": award["action"], "points": award["points"]})

            # PLAN_RIDE_MODE_API.md §A3, verbatim: "nav_* require
            # ride_options.nav_improvement + a ride_routes row." One shared
            # gate for every nav_* award — previously only the route-row
            # half of this precondition was checked, so a rider who
            # explicitly opted OUT of nav_improvement (the same consent
            # that makes storing their route acceptable in the first
            # place) could still earn nav_route_feedback/
            # nav_qualitative_feedback points.
            nav_improvement_on = (
                isinstance(ride_options, dict) and ride_options.get("nav_improvement") is True
            )

            if (
                payload.nav_route_rating is not None
                and ride_route_id is not None
                and nav_improvement_on
            ):
                award = credit_nav_route_feedback(
                    cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                    lat=start_lat, lng=start_lon, ride_id=str(rid),
                )
                if award is not None:
                    points_awarded.append({"action": award["action"], "points": award["points"]})

            if (
                payload.nav_qualitative is not None
                and len(payload.nav_qualitative.strip()) >= NAV_QUALITATIVE_MIN_CHARS
                and nav_improvement_on
            ):
                award = credit_nav_qualitative_feedback(
                    cur, account_id=user.account_id, vehicle_identifier=vehicle_identifier,
                    lat=start_lat, lng=start_lon, ride_id=str(rid),
                )
                if award is not None:
                    points_awarded.append({"action": award["action"], "points": award["points"]})

        conn.commit()

    return _survey_response(
        survey_id=survey_id, ride_id=rid, vehicle_model=vehicle_model, payload=payload,
        ride_route_id=ride_route_id, created_at=created_at, points_awarded=points_awarded,
    )
