from decimal import Decimal
from unittest.mock import call, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from routes.models import FuelStation
from routes.services.routing import Coordinates, GeocodingResult, RouteResult
from routes.services.station_matcher import match_stations


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}})
class FuelRouteIntegrationTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.start = GeocodingResult(Coordinates(-100, 0), 'Start', 'USA', None)
        self.finish = GeocodingResult(Coordinates(-80, 0), 'Finish', 'USA', None)
        # Equatorial synthetic geometry makes fractional route positions linear.
        # Provider distance deliberately differs from geometry length to exercise
        # calibration onto one authoritative 1,000-mile fuel-distance axis.
        self.route = RouteResult(1609344, 54000, (self.start.coordinates, self.finish.coordinates))
        patcher = patch('routes.services.route_planner.OpenRouteServiceClient', autospec=True)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = self.factory.return_value

    def station(self, mile, price='3', **changes):
        values = dict(opis_truckstop_id=123, truckstop_name='Truck Stop', address='123 Main St',
                      city='Test City', state='TX', rack_id=42, retail_price=Decimal(price),
                      latitude=Decimal('0'), longitude=Decimal('-100') + Decimal(str(mile)) / 50)
        values.update(changes)
        return FuelStation.objects.create(**values)

    def request(self, expected_status=200, expected_queries=1):
        self.provider.reset_mock()
        self.provider.geocode.side_effect = [self.start, self.finish]
        self.provider.get_route.return_value = self.route
        # Long routes use one bounding-box SELECT; short routes need no queries.
        with self.assertNumQueries(expected_queries):
            response = self.api.post('/api/routes/', {'start': 'Start', 'finish': 'Finish'}, format='json')
        self.assertEqual(response.status_code, expected_status, response.content)
        self.assertEqual(self.provider.mock_calls, [
            call.geocode('Start'), call.geocode('Finish'),
            call.get_route(self.start.coordinates, self.finish.coordinates),
        ])
        return response.json()

    def test_long_route_cost_selection_metadata_geometry_and_query_count(self):
        first = self.station(400, '5', truckstop_name='First Stop')
        self.station(500, '9')
        cheaper = self.station(600, '3.12345678', opis_truckstop_id=456, truckstop_name='Cheaper Stop')
        with patch('routes.services.station_geocoder.geocode_station', side_effect=AssertionError('Runtime enrichment')):
            data = self.request()
        self.assertEqual(data['route'], {'distance_miles': 1000.0, 'duration_hours': 15.0, 'geometry': self.route.geometry})
        stops = data['fuel']['stops']
        self.assertEqual([stop['station_id'] for stop in stops], [first.pk, cheaper.pk])
        self.assertEqual(stops[0], {
            'station_id': first.pk, 'opis_truckstop_id': 123, 'name': 'First Stop',
            'address': '123 Main St', 'city': 'Test City', 'state': 'TX',
            'coordinates': {'longitude': -92.0, 'latitude': 0.0},
            'route_mile': 400.0, 'distance_from_route_miles': 0.0,
            'price_per_gallon': '5.00000000', 'gallons_purchased': '10.000000', 'fuel_cost': '50.00',
        })
        self.assertEqual(stops[1]['opis_truckstop_id'], 456)
        self.assertEqual(stops[1]['price_per_gallon'], '3.12345678')
        self.assertEqual(stops[1]['gallons_purchased'], '40.000000')
        self.assertEqual(stops[1]['fuel_cost'], '124.94')
        self.assertEqual(data['fuel']['summary'], {
            'total_gallons_consumed': '100.000000', 'total_gallons_purchased': '50.000000',
            'total_fuel_cost': '174.94',
        })

    def test_short_routes_without_stations(self):
        for miles in (100, 500):
            with self.subTest(miles=miles):
                self.route = RouteResult(miles * 1609.344, 3600, self.route.coordinates)
                with patch('routes.services.route_planner.match_stations') as matcher:
                    with patch('routes.services.route_planner.geometry_length_miles') as length:
                        data = self.request(expected_queries=0)
                    length.assert_not_called()
                    matcher.assert_not_called()
                self.assertEqual(data['fuel']['stops'], [])
                self.assertEqual(data['fuel']['summary']['total_gallons_purchased'], '0.000000')
                self.assertEqual(data['fuel']['summary']['total_fuel_cost'], '0.00')

    def test_no_coordinates_infeasible_without_runtime_enrichment(self):
        self.station(400, latitude=None)
        self.station(600, longitude=None)
        self.station(700, latitude=None, longitude=None)
        data = self.request(422)
        self.assertEqual(data, {'error': {'code': 'fuel_route_infeasible',
            'message': 'A feasible fuel plan could not be found for this route.'}})

    def test_no_stations_infeasible(self):
        self.assertEqual(self.request(422)['error']['code'], 'fuel_route_infeasible')

    def test_stations_outside_corridor_are_unusable(self):
        self.station(400, latitude=Decimal('1'))
        self.station(600, latitude=Decimal('1'))
        self.assertEqual(self.request(422)['error']['code'], 'fuel_route_infeasible')

    def test_first_station_beyond_range(self):
        self.station(501)
        self.assertEqual(self.request(422)['error']['code'], 'fuel_route_infeasible')

    def test_unreachable_gap(self):
        self.station(300)
        self.station(801)
        self.assertEqual(self.request(422)['error']['code'], 'fuel_route_infeasible')

    def test_duplicates_preserved_and_selection_deterministic(self):
        costly = self.station(400, '8')
        cheapest = self.station(400, '2')
        same_price = self.station(400, '2')
        final = self.station(800, '3')
        matched = match_stations(self.route.geometry, FuelStation.objects.all())
        self.assertEqual({s.station_id for s in matched}, {costly.pk, cheapest.pk, same_price.pk, final.pk})
        first = self.request()
        second = self.request()
        self.assertEqual(first, second)
        self.assertEqual([s['station_id'] for s in first['fuel']['stops']], [cheapest.pk, final.pk])
        self.assertEqual([s['gallons_purchased'] for s in first['fuel']['stops']], ['40.000000', '10.000000'])
        self.assertEqual(first['fuel']['summary']['total_fuel_cost'], '110.00')

    def test_missing_coordinates_do_not_affect_success(self):
        self.station(400, '5')
        self.station(600, '3')
        self.station(500, '0.01', latitude=None)
        self.station(500, '0.01', longitude=None)
        data = self.request()
        self.assertEqual(data['fuel']['summary']['total_fuel_cost'], '170.00')

    def test_destination_station_does_not_exceed_provider_distance(self):
        self.station(400, '2')
        self.station(800, '3')
        destination = self.station(1000, '1')
        data = self.request()
        self.assertNotIn(destination.pk, [s['station_id'] for s in data['fuel']['stops']])

    def test_fractional_gallons_formatting(self):
        self.station(400, '3.12345678')
        self.route = RouteResult(float(Decimal('501.2') * Decimal('1609.344')), 3600, self.route.coordinates)
        data = self.request()
        self.assertEqual(data['fuel']['stops'][0]['gallons_purchased'], '0.120000')
        self.assertEqual(data['fuel']['stops'][0]['fuel_cost'], '0.37')
        self.assertEqual(data['fuel']['summary']['total_fuel_cost'], '0.37')
