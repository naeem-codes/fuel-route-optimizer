"""Orchestrate two location lookups and one driving-route request."""

from dataclasses import dataclass

from .routing import GeocodingError, GeocodingResult, OpenRouteServiceClient, RouteResult


class LocationNotFoundError(Exception):
    """Identify which input could not be resolved without exposing provider data."""

    def __init__(self, field):
        self.field = field
        super().__init__(f'Could not resolve the {field} location.')


@dataclass(frozen=True)
class RoutePlan:
    start_query: str
    finish_query: str
    start: GeocodingResult
    finish: GeocodingResult
    route: RouteResult

    def as_dict(self):
        def location(query, result):
            return {
                'query': query,
                'label': result.label,
                'coordinates': {
                    'longitude': result.coordinates.longitude,
                    'latitude': result.coordinates.latitude,
                },
            }

        # Round only at the response boundary; retain provider precision internally.
        return {
            'start': location(self.start_query, self.start),
            'finish': location(self.finish_query, self.finish),
            'route': {
                'distance_miles': round(self.route.distance_meters / 1609.344, 3),
                'duration_hours': round(self.route.duration_seconds / 3600, 4),
                'geometry': self.route.geometry,
            },
        }


def plan_route(start: str, finish: str, *, client=None) -> RoutePlan:
    """Plan validated inputs, stopping immediately if either lookup fails."""
    if client is None:
        client = OpenRouteServiceClient()
    try:
        start_location = client.geocode(start)
    except GeocodingError:
        raise LocationNotFoundError('start') from None
    try:
        finish_location = client.geocode(finish)
    except GeocodingError:
        raise LocationNotFoundError('finish') from None
    route = client.get_route(start_location.coordinates, finish_location.coordinates)
    return RoutePlan(start, finish, start_location, finish_location, route)
