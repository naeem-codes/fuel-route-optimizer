"""Pure minimum-cost fueling with a free full tank at departure.

Distances are along the route; station detours, reserve fuel, and stop penalties
are not modeled. Decimal arithmetic uses 40 significant digits, with no currency
or gallon quantization. Feasibility is tracked in miles of fuel range so division
by an mpg with a recurring decimal cannot change a reachability decision.
"""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext
from typing import Iterable


class FuelRouteInfeasibleError(Exception):
    """The route contains a gap that cannot be crossed with a full tank."""


class FuelOptimizationInputError(ValueError):
    """The optimizer received invalid domain input."""


@dataclass(frozen=True)
class FuelCandidate:
    station_id: int
    station_name: str
    price_per_gallon: Decimal
    distance_along_route_miles: Decimal


@dataclass(frozen=True)
class FuelStop:
    station_id: int
    station_name: str
    route_mile: Decimal
    price_per_gallon: Decimal
    gallons_purchased: Decimal
    fuel_cost: Decimal


@dataclass(frozen=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    total_gallons_purchased: Decimal
    total_fuel_cost: Decimal
    total_gallons_consumed: Decimal


def _decimal(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise FuelOptimizationInputError(f'{name} must be a finite number.')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise FuelOptimizationInputError(f'{name} must be a finite number.') from None
    if not result.is_finite() or (positive and result <= 0):
        raise FuelOptimizationInputError(f'{name} must be finite' + (' and positive.' if positive else '.'))
    return result


def optimize_fuel(
    route_distance_miles,
    candidates: Iterable[FuelCandidate],
    mpg=10,
    max_range_miles=500,
) -> FuelPlan:
    """Return an optimal purchase plan or raise a domain exception.

    Exchange argument: fuel bought here instead of at a cheaper reachable stop
    cannot reduce cost. Buy only enough to reach the first cheaper stop. When no
    cheaper stop is reachable, maximize useful fuel bought here, capped by the
    destination. Never discard fuel already carried from an earlier stop.

    Sort inputs; at identical miles prefer lowest price, then lowest station ID.
    Other source records remain untouched. A monotonic stack finds the next
    strictly cheaper station in O(n); sorting dominates at O(n log n), O(n) space.
    Equal-price stations need not minimize stop count, only purchase cost.
    """
    with localcontext() as context:
        context.prec = 40
        distance = _decimal(route_distance_miles, 'Route distance', positive=True)
        efficiency = _decimal(mpg, 'MPG', positive=True)
        capacity = _decimal(max_range_miles, 'Maximum range', positive=True)
        try:
            candidate_iterator = iter(candidates)
        except TypeError:
            raise FuelOptimizationInputError('Candidates must be iterable.') from None
        normalized = []
        for candidate in candidate_iterator:
            if not isinstance(candidate, FuelCandidate):
                raise FuelOptimizationInputError('Candidates must be FuelCandidate instances.')
            mile = _decimal(candidate.distance_along_route_miles, 'Station route mile')
            price = _decimal(candidate.price_per_gallon, 'Station price', positive=True)
            if not 0 <= mile <= distance:
                raise FuelOptimizationInputError('Station route mile must be within the route.')
            if type(candidate.station_id) is not int:
                raise FuelOptimizationInputError('Station ID must be an integer.')
            normalized.append(replace(candidate, price_per_gallon=price, distance_along_route_miles=mile))
        normalized.sort(key=lambda station: (
            station.distance_along_route_miles, station.price_per_gallon, station.station_id,
        ))
        stations = []
        for station in normalized:
            if not stations or station.distance_along_route_miles != stations[-1].distance_along_route_miles:
                stations.append(station)

        # Validate the whole route before producing any plan.
        previous = Decimal(0)
        for mile in [s.distance_along_route_miles for s in stations] + [distance]:
            if mile - previous > capacity:
                raise FuelRouteInfeasibleError(f'No reachable fuel station between miles {previous} and {mile}.')
            previous = mile

        next_cheaper = [None] * len(stations)
        stack = []
        for index in range(len(stations) - 1, -1, -1):
            while stack and stations[stack[-1]].price_per_gallon >= stations[index].price_per_gallon:
                stack.pop()
            if stack:
                next_cheaper[index] = stack[-1]
            stack.append(index)

        remaining_range = capacity
        previous = Decimal(0)
        stops = []
        for index, station in enumerate(stations):
            mile = station.distance_along_route_miles
            remaining_range -= mile - previous
            target_range = min(capacity, distance - mile)
            cheaper = next_cheaper[index]
            if cheaper is not None:
                target_range = min(target_range, stations[cheaper].distance_along_route_miles - mile)
            purchase_range = max(Decimal(0), target_range - remaining_range)
            if purchase_range > 0:
                gallons = purchase_range / efficiency
                stops.append(FuelStop(
                    station.station_id, station.station_name, mile,
                    station.price_per_gallon, gallons, gallons * station.price_per_gallon,
                ))
                remaining_range += purchase_range
            previous = mile

        return FuelPlan(
            tuple(stops),
            sum((stop.gallons_purchased for stop in stops), Decimal(0)),
            sum((stop.fuel_cost for stop in stops), Decimal(0)),
            distance / efficiency,
        )
