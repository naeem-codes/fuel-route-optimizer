"""OpenRouteService adapter. Coordinates use (longitude, latitude) order."""

from dataclasses import dataclass
import math

import httpx
from django.conf import settings


class RoutingProviderError(Exception):
    """The provider could not complete a request or returned invalid data."""


class RoutingConfigurationError(RoutingProviderError):
    """The provider client is not configured."""


class GeocodingError(RoutingProviderError):
    """A location is blank or could not be resolved."""


class RouteNotFoundError(RoutingProviderError):
    """No driving route could be found."""


@dataclass(frozen=True)
class Coordinates:
    longitude: float
    latitude: float

    def __post_init__(self):
        for value, limit in ((self.longitude, 180), (self.latitude, 90)):
            if type(value) not in (int, float) or not math.isfinite(value) or not -limit <= value <= limit:
                raise ValueError('Coordinates must be finite longitude/latitude values in range.')

    def as_pair(self) -> list[float]:
        return [self.longitude, self.latitude]


@dataclass(frozen=True)
class GeocodingResult:
    coordinates: Coordinates
    label: str | None
    country_code: str | None
    region: str | None


@dataclass(frozen=True)
class RouteResult:
    distance_meters: float
    duration_seconds: float
    coordinates: tuple[Coordinates, ...]

    @property
    def geometry(self) -> dict:
        """Return a fresh GeoJSON LineString, without provider-specific fields."""
        return {'type': 'LineString', 'coordinates': [point.as_pair() for point in self.coordinates]}


def _coordinates(value) -> Coordinates:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('Expected a longitude/latitude pair.')
    return Coordinates(*value)


def _optional_text(properties, key):
    value = properties.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError('Invalid location metadata.')
    return value


def _measurement(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid route measurement.')
    return float(value)


class OpenRouteServiceClient:
    """One HTTP request per method; no retries or redirects.

    An optional HTTPX transport allows offline testing. The API key defaults to
    Django's environment-backed setting; .env files are not loaded implicitly.
    """

    BASE_URL = 'https://api.heigit.org'
    TIMEOUT = httpx.Timeout(15.0, connect=5.0)

    def __init__(self, api_key: str | None = None, *, transport: httpx.BaseTransport | None = None):
        key = settings.OPENROUTESERVICE_API_KEY if api_key is None else api_key
        if not isinstance(key, str) or not key.strip():
            raise RoutingConfigurationError('OPENROUTESERVICE_API_KEY is required.')
        self._api_key = key.strip()
        self._transport = transport

    def _request(self, method, path, **kwargs):
        try:
            with httpx.Client(
                transport=self._transport, timeout=self.TIMEOUT, follow_redirects=False,
                headers={'Authorization': self._api_key, 'Accept': 'application/geo+json, application/json'},
            ) as client:
                response = client.request(method, self.BASE_URL + path, **kwargs)
        except httpx.TimeoutException:
            raise RoutingProviderError('Routing provider request timed out.') from None
        except httpx.RequestError:
            raise RoutingProviderError('Could not connect to the routing provider.') from None

        if not response.is_success:
            # ORS uses 2009/2010 when a route or a routable point cannot be found.
            if method == 'POST' and response.status_code in (400, 404):
                try:
                    code = response.json()['error']['code']
                except (ValueError, KeyError, TypeError):
                    code = None
                if code in (2009, 2010):
                    raise RouteNotFoundError('No driving route could be found.')
            raise RoutingProviderError(f'Routing provider returned HTTP {response.status_code}.')
        try:
            return response.json()
        except ValueError:
            raise RoutingProviderError('Routing provider returned invalid JSON.') from None

    @staticmethod
    def _features(payload):
        if not isinstance(payload, dict) or payload.get('type') != 'FeatureCollection':
            raise ValueError('Expected a FeatureCollection.')
        features = payload['features']
        if not isinstance(features, list):
            raise ValueError('Expected a feature list.')
        return features

    def geocode(self, location: str) -> GeocodingResult:
        if not isinstance(location, str) or not location.strip():
            raise GeocodingError('Location must not be blank.')
        payload = self._request('GET', '/pelias/v1/search', params={
            'text': location.strip(), 'size': 1, 'boundary.country': 'USA',
        })
        try:
            features = self._features(payload)
            if not features:
                raise GeocodingError('No location could be resolved.')
            feature = features[0]
            if feature['type'] != 'Feature' or feature['geometry']['type'] != 'Point':
                raise ValueError('Expected a point feature.')
            properties = feature['properties']
            if not isinstance(properties, dict):
                raise ValueError('Expected location properties.')
            return GeocodingResult(
                coordinates=_coordinates(feature['geometry']['coordinates']),
                label=_optional_text(properties, 'label'),
                country_code=_optional_text(properties, 'country_a'),
                region=_optional_text(properties, 'region'),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RoutingProviderError('Routing provider returned an invalid geocoding response.') from None

    def get_route(self, start_coordinates: Coordinates, finish_coordinates: Coordinates) -> RouteResult:
        if not isinstance(start_coordinates, Coordinates) or not isinstance(finish_coordinates, Coordinates):
            raise ValueError('Start and finish must be Coordinates instances.')
        payload = self._request('POST', '/openrouteservice/v2/directions/driving-car/geojson', json={
            'coordinates': [start_coordinates.as_pair(), finish_coordinates.as_pair()],
            'units': 'm', 'instructions': False,
        })
        try:
            features = self._features(payload)
            if not features:
                raise RouteNotFoundError('No driving route could be found.')
            feature = features[0]
            geometry = feature['geometry']
            if feature['type'] != 'Feature' or geometry['type'] != 'LineString':
                raise ValueError('Expected a LineString feature.')
            points = geometry['coordinates']
            if not isinstance(points, list) or len(points) < 2:
                raise ValueError('Expected at least two route points.')
            summary = feature['properties']['summary']
            return RouteResult(
                distance_meters=_measurement(summary['distance']),
                duration_seconds=_measurement(summary['duration']),
                coordinates=tuple(_coordinates(point) for point in points),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RoutingProviderError('Routing provider returned an invalid routing response.') from None
