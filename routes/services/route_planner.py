"""Orchestrate two location lookups and one driving-route request."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from routes.models import FuelStation

from .fuel_optimizer import FuelCandidate, FuelPlan, optimize_fuel
from .station_matcher import MatchedStation, geometry_length_miles, match_stations

from .routing import GeocodingError, GeocodingResult, OpenRouteServiceClient, RouteResult, RoutingProviderError


MPG = 10
MAX_RANGE_MILES = 500


def _fixed(value, places):
    """Fuel quantities/prices/costs are fixed-point JSON strings, never floats."""
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), 'f')


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
    distance_miles: Decimal
    fuel_plan: FuelPlan
    matched_stations: tuple[MatchedStation, ...]

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

        metadata = {station.station_id: station for station in self.matched_stations}
        stops = []
        for stop in self.fuel_plan.stops:
            station = metadata[stop.station_id]
            stops.append({
                'station_id': stop.station_id,
                'opis_truckstop_id': station.opis_truckstop_id,
                'name': stop.station_name,
                'address': station.address, 'city': station.city, 'state': station.state,
                'coordinates': {'longitude': float(station.longitude), 'latitude': float(station.latitude)},
                'route_mile': float(round(stop.route_mile, 3)),
                'distance_from_route_miles': round(station.distance_from_route_miles, 3),
                'price_per_gallon': _fixed(stop.price_per_gallon, 8),
                'gallons_purchased': _fixed(stop.gallons_purchased, 6),
                'fuel_cost': _fixed(stop.fuel_cost, 2),
            })
        # Round only at the response boundary; retain provider precision internally.
        return {
            'start': location(self.start_query, self.start),
            'finish': location(self.finish_query, self.finish),
            'route': {
                'distance_miles': float(round(self.distance_miles, 3)),
                'duration_hours': round(self.route.duration_seconds / 3600, 4),
                'geometry': self.route.geometry,
            },
            'fuel': {
                'vehicle': {'mpg': MPG, 'max_range_miles': MAX_RANGE_MILES,
                            'tank_capacity_gallons': MAX_RANGE_MILES // MPG},
                'stops': stops,
                'summary': {
                    'total_gallons_consumed': _fixed(self.fuel_plan.total_gallons_consumed, 6),
                    'total_gallons_purchased': _fixed(self.fuel_plan.total_gallons_purchased, 6),
                    'total_fuel_cost': _fixed(self.fuel_plan.total_fuel_cost, 2),
                },
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
    distance_miles = Decimal(str(route.distance_meters)) / Decimal('1609.344')
    if distance_miles <= 0:
        raise RoutingProviderError('Routing provider returned a zero-length route.')
    if distance_miles <= MAX_RANGE_MILES:
        fuel_plan = optimize_fuel(distance_miles, (), mpg=MPG, max_range_miles=MAX_RANGE_MILES)
        return RoutePlan(start, finish, start_location, finish_location, route,
                         distance_miles, fuel_plan, ())
    geometry = route.geometry
    stations = FuelStation.objects.filter(latitude__isnull=False, longitude__isnull=False).defer('rack_id')
    matched = match_stations(geometry, stations)
    geometry_distance = Decimal(str(geometry_length_miles(geometry)))
    if geometry_distance <= 0:
        raise RoutingProviderError('Routing provider returned a zero-length geometry.')
    # Provider road distance can differ from the geometry's haversine length.
    # Calibrate projected positions proportionally onto the same distance axis
    # used for fuel consumption. Keep geometry and perpendicular offsets intact.
    candidates = [FuelCandidate(
        station.station_id, station.truckstop_name, station.retail_price,
        min(distance_miles, Decimal(str(station.distance_along_route_miles)) / geometry_distance * distance_miles),
    ) for station in matched]
    fuel_plan = optimize_fuel(distance_miles, candidates, mpg=MPG, max_range_miles=MAX_RANGE_MILES)
    return RoutePlan(start, finish, start_location, finish_location, route,
                     distance_miles, fuel_plan, tuple(matched))
