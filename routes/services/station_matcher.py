"""Local corridor matching for ordinary road LineStrings.

Segments are projected in a local equirectangular plane, scaling longitude by
cosine of the segment's midpoint latitude. Haversine distances measure segment
length and separation from the projected point. This approximation is intended
for road geometries with short segments, not sparse transcontinental chords.
Antimeridian-crossing segments are rejected rather than matched incorrectly.
"""

from dataclasses import dataclass
from decimal import Decimal
import math

from django.db.models import QuerySet


DEFAULT_CORRIDOR_MILES = 5.0
EARTH_RADIUS_MILES = 3958.7613


@dataclass(frozen=True)
class MatchedStation:
    station_id: int
    opis_truckstop_id: int
    truckstop_name: str
    address: str
    city: str
    state: str
    retail_price: Decimal
    latitude: Decimal
    longitude: Decimal
    distance_along_route_miles: float
    distance_from_route_miles: float


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('Each route point must be a longitude/latitude pair.')
    for number, limit in zip(value, (180, 90)):
        if type(number) not in (int, float) or not -limit <= number <= limit:
            raise ValueError('Route coordinates must be finite numbers within geographic bounds.')
    return tuple(map(float, value))


def _haversine(a, b):
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def geometry_length_miles(geometry):
    """Length on the matcher's distance scale, for provider-distance calibration.

    Call after match_stations has validated the geometry.
    """
    points = geometry['coordinates']
    return sum(_haversine(a, b) for a, b in zip(points, points[1:]))


@dataclass(frozen=True)
class _Segment:
    start: tuple
    finish: tuple
    longitude_scale: float
    dx: float
    dy: float
    squared_length: float
    length_miles: float
    cumulative_miles: float
    bounds: tuple


def _bounds(points, lat_padding, lon_padding):
    return (
        min(p[0] for p in points) - lon_padding,
        max(p[0] for p in points) + lon_padding,
        min(p[1] for p in points) - lat_padding,
        max(p[1] for p in points) + lat_padding,
    )


def _inside(point, bounds):
    west, east, south, north = bounds
    return west <= point[0] <= east and south <= point[1] <= north


def _project(point, segment):
    x = (point[0] - segment.start[0]) * segment.longitude_scale
    y = point[1] - segment.start[1]
    fraction = 0.0 if segment.squared_length == 0 else max(
        0.0, min(1.0, (x * segment.dx + y * segment.dy) / segment.squared_length),
    )
    projected = (
        segment.start[0] + fraction * (segment.finish[0] - segment.start[0]),
        segment.start[1] + fraction * (segment.finish[1] - segment.start[1]),
    )
    return _haversine(point, projected), segment.cumulative_miles + fraction * segment.length_miles


def match_stations(geometry, stations, max_distance_miles=DEFAULT_CORRIDOR_MILES) -> list[MatchedStation]:
    """Match a queryset or iterable of saved stations, preserving duplicate rows.

    Equal nearest projections (e.g. a route revisiting a location) prefer the
    earliest route position. Output ties are ordered by database primary key.
    """
    if type(max_distance_miles) not in (int, float) or not math.isfinite(max_distance_miles) or max_distance_miles < 0:
        raise ValueError('Corridor radius must be a finite, non-negative number of miles.')
    if not isinstance(geometry, dict) or geometry.get('type') != 'LineString':
        raise ValueError('Route geometry must be a GeoJSON LineString.')
    coordinates = geometry.get('coordinates')
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise ValueError('Route geometry requires at least two points.')
    points = [_point(value) for value in coordinates]
    if any(abs(a[0] - b[0]) > 180 for a, b in zip(points, points[1:])):
        raise ValueError('Antimeridian-crossing route segments are not supported.')

    lat_padding = math.degrees(max_distance_miles / EARTH_RADIUS_MILES)
    # Conservative padding at the largest absolute latitude in the corridor.
    max_latitude = min(90, max(abs(p[1]) for p in points) + lat_padding)
    lon_padding = min(360, lat_padding / max(math.cos(math.radians(max_latitude)), 1e-12))
    # A tiny expansion avoids excluding boundary candidates due to float error.
    bounds = _bounds(points, lat_padding + 1e-9, lon_padding + 1e-9)
    segments = []
    cumulative = 0.0
    for start, finish in zip(points, points[1:]):
        scale = math.cos(math.radians((start[1] + finish[1]) / 2))
        dx, dy = (finish[0] - start[0]) * scale, finish[1] - start[1]
        length = _haversine(start, finish)
        segments.append(_Segment(
            start, finish, scale, dx, dy, dx * dx + dy * dy, length, cumulative,
            _bounds((start, finish), lat_padding + 1e-9, lon_padding + 1e-9),
        ))
        cumulative += length

    if isinstance(stations, QuerySet):
        west, east, south, north = bounds
        stations = stations.filter(
            latitude__isnull=False, longitude__isnull=False,
            latitude__gte=south, latitude__lte=north,
            longitude__gte=west, longitude__lte=east,
        ).iterator(chunk_size=500)

    matches = []
    for station in stations:
        if station.latitude is None or station.longitude is None:
            continue
        point = (float(station.longitude), float(station.latitude))
        if not _inside(point, bounds):
            continue
        nearest = None
        for segment in segments:
            if not _inside(point, segment.bounds):
                continue
            projection = _project(point, segment)
            if nearest is None or projection < nearest:
                nearest = projection
        if nearest is None or nearest[0] > max_distance_miles:
            continue
        matches.append(MatchedStation(
            station.pk, station.opis_truckstop_id, station.truckstop_name,
            station.address, station.city, station.state, station.retail_price,
            station.latitude, station.longitude, nearest[1], nearest[0],
        ))
    return sorted(matches, key=lambda match: (match.distance_along_route_miles, match.station_id))
