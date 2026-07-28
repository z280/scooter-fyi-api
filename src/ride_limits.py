"""OPERATOR-SET HARD INVARIANTS on what a ride can be.

    MAX_POINTS_PER_RIDE       no ride awards more than 100 points, total
    MAX_LEG_METERS            no two consecutive points are >3 km apart
    MAX_RIDE_DISTANCE_METERS  no ride is longer than 80 km

READ THIS BEFORE CHANGING ANY OF THE THREE NUMBERS BELOW.

These are NOT tuned heuristics and they are NOT thresholds anybody
measured. They are three sentences the operator said about what a ride is
allowed to be, transcribed. That makes them different in kind from the
plausibility bounds in src/api_rides.py (`_MAX_AVG_SPEED_MPS`,
`_POLYLINE_DISTANCE_FACTOR`), which ARE tuned — those were picked by
argument from e-scooter governor speeds and the UCI hour record, and a
better argument may legitimately move them.

Nothing here can be moved by a better argument. If one of these numbers is
wrong, the operator changes it; a future reader who finds a cap
inconvenient must not "optimize" it, widen it to make a test pass, or
special-case around it. The three constants are deliberately in their own
module, imported everywhere and defined nowhere else, so that there is
exactly one line to change and it is impossible to change one copy and
miss another.

WHY EACH ONE EXISTS
-------------------
* MAX_POINTS_PER_RIDE — `end_tracked_ride` credited 2 points per waypoint
  with no ceiling plus a flat 20 for GBFS validation, so a 600-waypoint
  ride paid out 1220. Waypoint count is rider-controlled, which made the
  points economy an unbounded faucet driven by how often a phone posts.
* MAX_LEG_METERS — a ride's path was summed over consecutive fixes with no
  sanity check on the gap between them, so two waypoints on opposite sides
  of the world recorded ~15 000 km of "riding". A real sampling gap on a
  scooter is metres to hundreds of metres; 3 km is already far beyond any
  honest gap and well inside the absurd.
* MAX_RIDE_DISTANCE_METERS — miles_100 is 160 934 m, and both the one-shot
  claim ceiling (200 000 m) and the waypoint-summed path allowed a single
  ride to clear it. 80 km is the operator's statement of the longest thing
  that counts as one ride.
"""

from __future__ import annotations

from collections.abc import Sequence

from .geo import distance_meters, path_length_meters

# --- OPERATOR-SET INVARIANT: no ride awards more than this, ever. ----------
# Per RIDE, summed across every point-awarding action attributable to it —
# not per action. Enforced in exactly one place, src/points.py:credit_points,
# so that a future third award for a ride cannot bypass it by not knowing
# about it. `qr_scan` is deliberately NOT subject to this: it is a device
# scan, not a ride award, and it is worth 100 on its own.
MAX_POINTS_PER_RIDE = 100

# --- OPERATOR-SET INVARIANT: no two consecutive path points are further -----
# --- apart than this. -------------------------------------------------------
# Applies to a ride's measured path: start -> each uploaded fix -> reported
# end. See leg_is_plausible() for the one deliberate exemption (a ride with
# no track at all, where start -> end is the whole ride rather than a
# sampling gap).
MAX_LEG_METERS = 3_000.0

# --- OPERATOR-SET INVARIANT: no ride is longer than this. -------------------
# Applies to every mechanism that can record a distance: the one-shot
# client-asserted claim, the waypoint-summed path on an off-feed ride, and
# the waypoint-summed path on a tracked ride.
MAX_RIDE_DISTANCE_METERS = 80_000.0

# Integer form for the pydantic bound on the one-shot POST body, which
# validates `distance_m` as an int. Same number, so the request-level
# rejection and the measured-path clamp can never disagree.
MAX_RIDE_DISTANCE_M_INT = int(MAX_RIDE_DISTANCE_METERS)


def leg_is_plausible(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True when two consecutive path points are within MAX_LEG_METERS.

    Note the boundary: a leg of EXACTLY MAX_LEG_METERS is plausible. The
    invariant is "no more than 3 km apart", so 3000.0 m is allowed and
    3000.1 m is not.
    """
    return distance_meters(a[0], a[1], b[0], b[1]) <= MAX_LEG_METERS


def measure_path(
    points: Sequence[tuple[float, float]], *, cap_legs: bool = True
) -> tuple[float, int]:
    """(distance_m, legs_excluded) over a (lat, lon) path.

    With cap_legs, any single leg longer than MAX_LEG_METERS contributes
    ZERO to the distance instead of contributing a number we don't believe.
    The count of excluded legs comes back so the caller can mark the
    measurement as partial rather than passing off a hole as a complete
    path.

    cap_legs=False is for the ONE case where a long leg is not a sampling
    gap: a ride that uploaded no waypoints at all, whose entire path is
    start -> reported end. That leg IS the ride, not a gap in it, so
    capping it at 3 km would silently cap every trackless ride at 3 km and
    contradict the 80 km ride cap. A trackless ride is bounded by
    MAX_RIDE_DISTANCE_METERS instead, via clamp_distance().
    """
    if not cap_legs:
        return path_length_meters(points), 0

    total = 0.0
    excluded = 0
    for a, b in zip(points, points[1:]):
        leg = distance_meters(a[0], a[1], b[0], b[1])
        if leg > MAX_LEG_METERS:
            excluded += 1
            continue
        total += leg
    return total, excluded


def clamp_distance(distance_m: float) -> tuple[float, float | None]:
    """(recorded_distance, clamped_from).

    clamped_from is None when the distance was already within the cap. When
    it is not None the ride is recorded AT the cap and clamped_from carries
    what was actually measured, so the row never claims a distance above
    the invariant while also never quietly losing what we saw.

    Boundary: exactly MAX_RIDE_DISTANCE_METERS is NOT clamped.
    """
    if distance_m <= MAX_RIDE_DISTANCE_METERS:
        return distance_m, None
    return MAX_RIDE_DISTANCE_METERS, distance_m


def close_out_path(
    start_lat: float | None, start_lon: float | None,
    track: Sequence[tuple[float, float]],
    end_lat: float, end_lon: float,
) -> tuple[list[tuple[float, float]], float, str, float | None]:
    """Close a ride at /end: (points, distance_m, distance_source, clamped_from).

    THE ONE RULE THIS FILE EXISTS TO GUARANTEE: reporting an end NEVER
    fails. There is no input to this function that raises. A rider whose GPS
    lied is not a rider who should be stuck in a ride they cannot leave —
    the active-ride unique index makes a refused end sticky until the
    24-hour sweep, which is the harshest failure this system can produce.
    So every implausible input is handled by recording LESS and saying so,
    never by refusing.

    Shared by both ride tables rather than reimplemented in each. They must
    enforce identically because src/badges.py sums distance across both, so
    a rider's mileage must not depend on which mechanism logged the ride;
    two copies that "must stay in sync" is exactly how that stops being
    true. Anything measured per-table would be a second implementation and
    a second thing to get wrong.

    Three cases:

      * A ride WITH a track whose final leg (last fix -> reported end) is
        over the leg cap. That leg is dropped, along with the reported end
        point, so distance and polyline still cover exactly the same
        points. Source becomes '..._partial'. The rider's reported end is
        still stored in end_lat/end_lon — it is their report and we keep
        it; we simply decline to measure a leg we don't believe.
      * A ride with NO track at all. start -> end is the whole ride, not a
        sampling gap, so the leg cap does not apply (see measure_path) and
        'straight_line' stands however long it is. Bounding it is the ride
        cap's job, below.
      * Any ride over the ride cap. Recorded AT the cap, with what was
        measured preserved in clamped_from.
    """
    points: list[tuple[float, float]] = []
    if start_lat is not None and start_lon is not None:
        points.append((start_lat, start_lon))
    points.extend(track)

    has_track = bool(track)
    end_excluded = False
    if has_track and points and not leg_is_plausible(points[-1], (end_lat, end_lon)):
        end_excluded = True
    else:
        points.append((end_lat, end_lon))

    measured, dropped = measure_path(points, cap_legs=has_track)
    recorded, clamped_from = clamp_distance(measured)
    source = partial_source(
        "waypoints" if has_track else "straight_line",
        partial=end_excluded or dropped > 0,
    )
    return points, recorded, source, clamped_from


def partial_source(source: str, *, partial: bool) -> str:
    """distance_source, suffixed when legs were excluded from the measurement.

    'waypoints' -> 'waypoints_partial'. The suffix is the field's way of
    saying "this number is a lower bound over a path with a hole in it",
    which is a different claim from 'waypoints' and must not be flattened
    into it — a consumer that treats them the same is treating a measured
    ride and a partially-disbelieved one as equal evidence.
    """
    if not partial:
        return source
    return f"{source}_partial"
