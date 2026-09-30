"""Offline station enrichment using one existing geocoding operation per row."""

from decimal import Decimal, ROUND_HALF_UP

from routes.models import FuelStation
from .routing import OpenRouteServiceClient


def geocode_station(station: FuelStation, *, client: OpenRouteServiceClient) -> None:
    address = ', '.join(part.strip() for part in (
        station.truckstop_name, station.address, station.city, station.state,
    ) if part.strip())
    result = client.geocode(address)
    precision = Decimal('0.000001')
    # Convert through str to avoid importing binary float artifacts into Decimal.
    station.latitude = Decimal(str(result.coordinates.latitude)).quantize(precision, rounding=ROUND_HALF_UP)
    station.longitude = Decimal(str(result.coordinates.longitude)).quantize(precision, rounding=ROUND_HALF_UP)
    station.save(update_fields=['latitude', 'longitude'])
