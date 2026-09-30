from decimal import Decimal
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from routes.models import FuelStation
from routes.services.routing import (
    Coordinates, GeocodingError, GeocodingResult, OpenRouteServiceClient, RoutingProviderError,
)
from routes.services.station_geocoder import geocode_station


RESULT = GeocodingResult(Coordinates(-96.7971235, 32.7776545), 'Dallas', 'USA', 'Texas')


class StationGeocoderTests(TestCase):
    def setUp(self):
        self.provider = Mock(spec=OpenRouteServiceClient)
        self.provider.geocode.return_value = RESULT

    def station(self, **changes):
        values = dict(opis_truckstop_id=123, truckstop_name=' Truck Stop ', address=' 123 Main St ',
                      city=' Dallas ', state='TX', rack_id=42, retail_price=Decimal('3.12345678'))
        values.update(changes)
        return FuelStation.objects.create(**values)

    def run_command(self, **options):
        output = StringIO()
        with patch('routes.management.commands.geocode_fuel_stations.OpenRouteServiceClient', return_value=self.provider):
            call_command('geocode_fuel_stations', stdout=output, **options)
        self.provider.get_route.assert_not_called()
        return output.getvalue()

    def test_coordinates_nullable_and_blank(self):
        station = self.station(truckstop_name='Truck Stop', address='123 Main St', city='Dallas')
        station.full_clean()
        self.assertIsNone(station.latitude)
        self.assertIsNone(station.longitude)
        for field in ('latitude', 'longitude'):
            self.assertTrue(FuelStation._meta.get_field(field).blank)

    def test_service_address_precision_and_single_update(self):
        station = self.station()
        before = FuelStation.objects.values().get(pk=station.pk)
        with self.assertNumQueries(1):
            geocode_station(station, client=self.provider)
        self.provider.geocode.assert_called_once_with('Truck Stop, 123 Main St, Dallas, TX')
        self.provider.get_route.assert_not_called()
        station.refresh_from_db()
        self.assertEqual(station.latitude, Decimal('32.777655'))
        self.assertEqual(station.longitude, Decimal('-96.797124'))
        after = FuelStation.objects.values().get(pk=station.pk)
        for field in ('latitude', 'longitude'):
            before.pop(field)
            after.pop(field)
        self.assertEqual(before, after)

    def test_default_selects_missing_either_coordinate_and_skips_complete(self):
        missing = [self.station(), self.station(latitude=Decimal('30')), self.station(longitude=Decimal('-97'))]
        complete = self.station(latitude=Decimal('30'), longitude=Decimal('-97'))
        output = self.run_command()
        self.assertEqual(self.provider.geocode.call_count, 3)
        self.assertEqual(output.strip(), 'Attempted: 3; resolved: 3; unresolved: 0.')
        for station in missing:
            station.refresh_from_db()
            self.assertEqual(station.latitude, Decimal('32.777655'))
            self.assertEqual(station.longitude, Decimal('-96.797124'))
        complete.refresh_from_db()
        self.assertEqual((complete.latitude, complete.longitude), (Decimal('30'), Decimal('-97')))

    def test_force_replaces_complete_coordinates(self):
        station = self.station(latitude=Decimal('30'), longitude=Decimal('-97'))
        self.run_command(force=True)
        self.provider.geocode.assert_called_once()
        station.refresh_from_db()
        self.assertEqual(station.latitude, Decimal('32.777655'))

    def test_limit_counts_attempts_including_unresolved(self):
        stations = [self.station() for _ in range(3)]
        self.provider.geocode.side_effect = [GeocodingError('private'), RESULT]
        output = self.run_command(limit=2)
        self.assertEqual(self.provider.geocode.call_count, 2)
        self.assertIn('Attempted: 2; resolved: 1; unresolved: 1.', output)
        for station in stations:
            station.refresh_from_db()
        self.assertIsNone(stations[0].latitude)
        self.assertIsNotNone(stations[1].latitude)
        self.assertIsNone(stations[2].latitude)

    def test_invalid_limit_rejected_before_provider_use(self):
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaisesMessage(CommandError, 'positive integer'):
                self.run_command(limit=limit)
        self.provider.geocode.assert_not_called()

    def test_unresolved_force_preserves_existing_coordinates(self):
        station = self.station(latitude=Decimal('30'), longitude=Decimal('-97'))
        self.provider.geocode.side_effect = GeocodingError('secret')
        output = self.run_command(force=True)
        self.assertNotIn('secret', output)
        station.refresh_from_db()
        self.assertEqual((station.latitude, station.longitude), (Decimal('30'), Decimal('-97')))

    @override_settings(OPENROUTESERVICE_API_KEY='')
    def test_missing_key_raises_clear_command_error(self):
        self.station()
        with self.assertRaisesMessage(CommandError, 'requires OPENROUTESERVICE_API_KEY'):
            call_command('geocode_fuel_stations', stdout=StringIO())
        self.assertIsNone(FuelStation.objects.get().latitude)

    def test_systemic_failure_stops_and_keeps_previous_success(self):
        stations = [self.station() for _ in range(3)]
        self.provider.geocode.side_effect = [RESULT, RoutingProviderError('secret Authorization'), RESULT]
        with self.assertRaisesMessage(CommandError, 'processing stopped') as error:
            self.run_command()
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(self.provider.geocode.call_count, 2)
        self.provider.get_route.assert_not_called()
        for station in stations:
            station.refresh_from_db()
        self.assertIsNotNone(stations[0].latitude)
        self.assertIsNone(stations[1].latitude)
        self.assertIsNone(stations[2].latitude)

    def test_duplicate_rows_remain_separate_and_each_get_one_call(self):
        first, second = self.station(), self.station()
        self.run_command()
        self.assertEqual(FuelStation.objects.count(), 2)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(self.provider.geocode.call_count, 2)
        self.assertEqual(len(self.provider.mock_calls), 2)

    def test_no_eligible_stations_makes_no_provider_calls(self):
        self.station(latitude=Decimal('30'), longitude=Decimal('-97'))
        self.assertIn('Attempted: 0; resolved: 0; unresolved: 0.', self.run_command())
        self.provider.geocode.assert_not_called()
