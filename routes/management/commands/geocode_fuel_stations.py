from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from routes.models import FuelStation
from routes.services.routing import (
    GeocodingError, OpenRouteServiceClient, RoutingConfigurationError, RoutingProviderError,
)
from routes.services.station_geocoder import geocode_station


class Command(BaseCommand):
    help = 'Populate missing station coordinates offline, saving each successful row.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, help='Attempt at most N stations (positive integer).')
        parser.add_argument('--force', action='store_true', help='Include stations with complete coordinates.')

    def handle(self, *args, **options):
        limit = options['limit']
        if limit is not None and limit <= 0:
            raise CommandError('--limit must be a positive integer.')
        try:
            client = OpenRouteServiceClient()
        except RoutingConfigurationError:
            raise CommandError('Station geocoding requires OPENROUTESERVICE_API_KEY.') from None

        stations = FuelStation.objects.order_by('pk')
        if not options['force']:
            stations = stations.filter(Q(latitude__isnull=True) | Q(longitude__isnull=True))
        if limit is not None:
            stations = stations[:limit]

        resolved = unresolved = 0
        # Each success is committed independently, so interrupted runs can resume.
        for station in stations.iterator(chunk_size=200):
            try:
                geocode_station(station, client=client)
            except GeocodingError:
                unresolved += 1
                self.stdout.write(f'Station {station.pk}: unresolved.')
            except RoutingConfigurationError:
                raise CommandError('Station geocoding configuration failed; processing stopped.') from None
            except RoutingProviderError:
                raise CommandError(
                    f'Station geocoding provider failed at station {station.pk}; processing stopped. '
                    f'Resolved: {resolved}; unresolved: {unresolved}. Earlier updates are saved.'
                ) from None
            else:
                resolved += 1

        self.stdout.write(self.style.SUCCESS(
            f'Attempted: {resolved + unresolved}; resolved: {resolved}; unresolved: {unresolved}.'
        ))
