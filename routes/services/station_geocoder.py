"""Offline station enrichment using one existing geocoding operation per row."""

from decimal import Decimal, ROUND_HALF_UP

from routes.models import FuelStation
from .routing import GeocodingError, OpenRouteServiceClient


def _normalized(value):
    """Ignore case, whitespace and punctuation, without fuzzy name matching."""
    return ''.join(char for char in (value or '').casefold() if char.isalnum())


# US state names allow Pelias region and region_a to be checked consistently.
_STATE_NAMES = dict(item.split(':') for item in (
    'AL:Alabama|AK:Alaska|AZ:Arizona|AR:Arkansas|CA:California|CO:Colorado|'
    'CT:Connecticut|DE:Delaware|FL:Florida|GA:Georgia|HI:Hawaii|ID:Idaho|'
    'IL:Illinois|IN:Indiana|IA:Iowa|KS:Kansas|KY:Kentucky|LA:Louisiana|'
    'ME:Maine|MD:Maryland|MA:Massachusetts|MI:Michigan|MN:Minnesota|'
    'MS:Mississippi|MO:Missouri|MT:Montana|NE:Nebraska|NV:Nevada|'
    'NH:New Hampshire|NJ:New Jersey|NM:New Mexico|NY:New York|'
    'NC:North Carolina|ND:North Dakota|OH:Ohio|OK:Oklahoma|OR:Oregon|'
    'PA:Pennsylvania|RI:Rhode Island|SC:South Carolina|SD:South Dakota|'
    'TN:Tennessee|TX:Texas|UT:Utah|VT:Vermont|VA:Virginia|WA:Washington|'
    'WV:West Virginia|WI:Wisconsin|WY:Wyoming|DC:District of Columbia'
).split('|'))


def _validate_station_result(station, result):
    """Require country, state, city and an allowed Pelias layer.

    Accept venue/address and street matches for highway-style source addresses;
    reject administrative, unknown and missing layers. Labels are display text,
    not reliable geographic evidence. Prefer locality; localadmin
    is a fallback only when locality is absent (they can legitimately differ).
    Missing evidence or aliases needing manual review remain unresolved.
    """
    state = station.state.strip().upper()
    regions = [value for value in (result.region_code, result.region) if _normalized(value)]
    allowed_regions = {_normalized(state), _normalized(_STATE_NAMES.get(state))}
    city = result.locality if _normalized(result.locality) else result.localadmin
    if (
        state not in _STATE_NAMES
        or _normalized(result.country_code) not in {'us', 'usa'}
        or not regions
        or any(_normalized(region) not in allowed_regions for region in regions)
        or not _normalized(station.city)
        or _normalized(city) != _normalized(station.city)
        or _normalized(result.layer) not in {'venue', 'address', 'street'}
    ):
        raise GeocodingError('Station location could not be verified.')


def geocode_station(station: FuelStation, *, client: OpenRouteServiceClient) -> None:
    address = ', '.join(part.strip() for part in (
        station.truckstop_name, station.address, station.city, station.state,
    ) if part.strip())
    result = client.geocode(address)
    _validate_station_result(station, result)
    precision = Decimal('0.000001')
    # Convert through str to avoid importing binary float artifacts into Decimal.
    station.latitude = Decimal(str(result.coordinates.latitude)).quantize(precision, rounding=ROUND_HALF_UP)
    station.longitude = Decimal(str(result.coordinates.longitude)).quantize(precision, rounding=ROUND_HALF_UP)
    station.save(update_fields=['latitude', 'longitude'])
