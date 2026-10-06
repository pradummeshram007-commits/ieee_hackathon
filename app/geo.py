"""Geography helpers: haversine distance, road factor, and integer matrices.

Everything handed to OR-Tools must be an integer, so this module is the single
place where floating point geography is converted into the integer units the
routing model speaks:

* distance -> **metres** (int)
* time     -> **minutes** (int, rounded *up* so the model never assumes a
  truck arrives earlier than it physically can; optimistic rounding would let
  the solver sneak past a spoilage deadline it cannot actually meet)
"""

from __future__ import annotations

import math
from typing import Iterable, List, NamedTuple, Sequence

#: Straight-line distance is multiplied by this to approximate road distance.
#: 1.3 is the usual "circuity factor" for Indian district road networks and
#: keeps us honest without needing a maps API.
ROAD_FACTOR = 1.3

#: Mean earth radius in metres (WGS-84 authalic radius).
EARTH_RADIUS_M = 6_371_008.8


class Point(NamedTuple):
    """A geographic point. ``label`` is carried only for readable debugging."""

    lat: float
    lng: float
    label: str = ""


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS-84 points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def point_distance_m(a: Point, b: Point) -> float:
    """Great-circle distance between two :class:`Point` values, in metres."""
    return haversine_m(a.lat, a.lng, b.lat, b.lng)


def road_distance_m(a: Point, b: Point) -> int:
    """Road distance estimate in **whole metres** (haversine x road factor)."""
    if a.lat == b.lat and a.lng == b.lng:
        return 0
    return int(round(point_distance_m(a, b) * ROAD_FACTOR))


def build_distance_matrix(points: Sequence[Point]) -> List[List[int]]:
    """Symmetric road-distance matrix in metres, ready for OR-Tools."""
    n = len(points)
    matrix = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = road_distance_m(points[i], points[j])
            matrix[i][j] = d
            matrix[j][i] = d
    return matrix


def travel_minutes(distance_m: int, speed_kmph: float) -> int:
    """Travel time in **whole minutes**, rounded up.

    ``distance_m`` is metres, ``speed_kmph`` km/h.  Rounding up is deliberate:
    see the module docstring.
    """
    if distance_m <= 0:
        return 0
    if speed_kmph <= 0:
        raise ValueError("speed_kmph must be positive")
    return int(math.ceil(distance_m * 60.0 / (speed_kmph * 1000.0)))


def build_time_matrix(
    distance_matrix: Sequence[Sequence[int]],
    speed_kmph: float,
    service_minutes: Sequence[int] | None = None,
) -> List[List[int]]:
    """Travel-time matrix in minutes for one vehicle speed.

    ``service_minutes[i]`` (when given) is folded into every arc *leaving*
    node ``i``.  That is the standard OR-Tools trick for service times: the
    time to traverse ``i -> j`` becomes "serve i, then drive to j", which
    makes the cumulative time variable at each node mean *arrival time*.
    """
    n = len(distance_matrix)
    service = list(service_minutes) if service_minutes is not None else [0] * n
    if len(service) != n:
        raise ValueError("service_minutes must have one entry per node")
    return [
        [service[i] + travel_minutes(distance_matrix[i][j], speed_kmph) for j in range(n)]
        for i in range(n)
    ]


def offset_point(origin: Point, bearing_deg: float, distance_km: float, label: str = "") -> Point:
    """Point reached by travelling ``distance_km`` from ``origin`` on a bearing.

    Used by the dataset generator to scatter villages at known distances and
    bearings from Nagpur, so "5-60 km around the city" is exact rather than
    approximated by jittering latitude and longitude.
    """
    angular = (distance_km * 1000.0) / EARTH_RADIUS_M
    theta = math.radians(bearing_deg)
    phi1 = math.radians(origin.lat)
    lambda1 = math.radians(origin.lng)

    phi2 = math.asin(
        math.sin(phi1) * math.cos(angular) + math.cos(phi1) * math.sin(angular) * math.cos(theta)
    )
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * math.sin(phi2),
    )
    return Point(round(math.degrees(phi2), 6), round(math.degrees(lambda2), 6), label)


def total_path_distance_m(points: Iterable[Point]) -> int:
    """Road distance of an ordered path, in metres."""
    ordered = list(points)
    return sum(road_distance_m(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1))
