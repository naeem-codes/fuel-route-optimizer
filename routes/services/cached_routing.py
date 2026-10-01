"""Best-effort provider-result caching, separate from fuel data and enrichment.

LocMemCache safely stores the immutable normalized dataclasses. Versioned keys
contain only hashes, never addresses, request headers, or credentials.
"""

from decimal import Decimal
import hashlib
import json
import logging
import math

from django.conf import settings
from django.core.cache import cache

from .routing import Coordinates, GeocodingResult, RouteResult


logger = logging.getLogger(__name__)


def _key(kind, value):
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()
    version = 2 if kind == 'geocode' else 1
    return f'heigit:routing:v{version}:{kind}:{digest}'


def _valid_coordinates(point):
    if not isinstance(point, Coordinates):
        return False
    try:
        Coordinates(point.longitude, point.latitude)
    except (ValueError, OverflowError, TypeError):
        return False
    return True


def _valid_geocode(value):
    return isinstance(value, GeocodingResult) and _valid_coordinates(value.coordinates) and all(
        field is None or isinstance(field, str) for field in (
            value.label, value.country_code, value.region, value.region_code,
            value.locality, value.localadmin, value.layer,
        )
    )


def _valid_route(value):
    return (
        isinstance(value, RouteResult)
        and all(type(number) in (int, float) and math.isfinite(number) and number >= 0
                for number in (value.distance_meters, value.duration_seconds))
        and isinstance(value.coordinates, tuple) and len(value.coordinates) >= 2
        and all(_valid_coordinates(point) for point in value.coordinates)
    )


class CachedRoutingClient:
    def __init__(self, provider):
        self.provider = provider

    def _get_or_fetch(self, key, validator, fetch):
        if settings.ROUTING_CACHE_TTL_SECONDS == 0:
            return fetch()
        # Catch only cache operations. Provider exceptions must propagate normally.
        try:
            value = cache.get(key)
        except Exception:
            logger.warning('Routing cache read failed; using provider.')
            value = None
        if validator(value):
            return value
        value = fetch()
        if validator(value):
            try:
                cache.set(key, value, timeout=settings.ROUTING_CACHE_TTL_SECONDS)
            except Exception:
                logger.warning('Routing cache write failed; result remains available.')
        return value

    def geocode(self, location):
        # Leave invalid inputs to the existing client's validation.
        if not isinstance(location, str) or not location.strip():
            return self.provider.geocode(location)
        return self._get_or_fetch(
            _key('geocode', location.strip().casefold()), _valid_geocode,
            lambda: self.provider.geocode(location),
        )

    def get_route(self, start, finish):
        if not _valid_coordinates(start) or not _valid_coordinates(finish):
            return self.provider.get_route(start, finish)
        def pair(point):
            # Canonical decimal text equates 1 and 1.0 without rounding positions.
            return [format(Decimal(str(value)).normalize(), 'f') if value else '0'
                    for value in (point.longitude, point.latitude)]
        return self._get_or_fetch(
            _key('driving-car-geojson', [pair(start), pair(finish)]), _valid_route,
            lambda: self.provider.get_route(start, finish),
        )
