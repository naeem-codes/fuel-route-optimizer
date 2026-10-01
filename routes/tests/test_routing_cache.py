from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock, patch

import httpx
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from routes.models import FuelStation
from routes.services.cached_routing import CachedRoutingClient
from routes.services.routing import Coordinates, OpenRouteServiceClient, RoutingProviderError


@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'routing-cache-tests',
}}, ROUTING_CACHE_TTL_SECONDS=86400)
class RoutingCacheTests(TestCase):
    def test_extended_geocoding_metadata_round_trip(self):
        from .test_routing import GEOCODING
        from copy import deepcopy
        payload = deepcopy(GEOCODING)
        payload['features'][0]['properties'].update(
            region_a='TX', locality='Austin', localadmin='Austin', layer='venue',
        )
        self.handler.side_effect = None
        self.handler.return_value = httpx.Response(200, json=payload)
        first = self.client.geocode('Austin')
        cached = self.client.geocode(' AUSTIN ')
        self.assertEqual(cached, first)
        self.assertEqual((cached.region_code, cached.locality, cached.localadmin, cached.layer),
                         ('TX', 'Austin', 'Austin', 'venue'))
        self.handler.assert_called_once()

    def test_corrupt_extended_metadata_is_not_reused(self):
        valid = self.client.geocode('Start')
        self.handler.reset_mock()
        with patch('routes.services.cached_routing.cache.get', return_value=replace(valid, locality=123)):
            self.client.geocode('Start')
        self.handler.assert_called_once()

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.distance = 400 * 1609.344
        self.handler = Mock(side_effect=self.respond)
        self.provider = OpenRouteServiceClient(api_key='secret-test-key', transport=httpx.MockTransport(self.handler))
        self.client = CachedRoutingClient(self.provider)
        self.api = APIClient()

    def respond(self, request):
        if request.method == 'GET':
            query = request.url.params['text'].strip().casefold()
            coordinate = [-100, 0] if query == 'start' else [-80, 0]
            return httpx.Response(200, json={'type': 'FeatureCollection', 'features': [{
                'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': coordinate},
                'properties': {'label': query.title(), 'country_a': 'USA'},
            }]})
        return httpx.Response(200, json={'type': 'FeatureCollection', 'features': [{
            'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': [[-100, 0], [-80, 0]]},
            'properties': {'summary': {'distance': self.distance, 'duration': 3600}},
        }]})

    def post(self, start='Start', finish='Finish', queries=0, status=200):
        with patch('routes.services.route_planner.OpenRouteServiceClient', return_value=self.provider):
            with self.assertNumQueries(queries):
                response = self.api.post('/api/routes/', {'start': start, 'finish': finish}, format='json')
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def test_cold_and_warm_api_are_identical(self):
        first = self.post()
        self.assertEqual([c.args[0].method for c in self.handler.call_args_list], ['GET', 'GET', 'POST'])
        self.handler.reset_mock()
        self.assertEqual(self.post(), first)
        self.handler.assert_not_called()

    def test_case_whitespace_identity_preserves_response_query(self):
        self.post()
        self.handler.reset_mock()
        data = self.post('  START  ', ' finish ')
        self.handler.assert_not_called()
        self.assertEqual(data['start']['query'], 'START')
        self.assertEqual(data['finish']['query'], 'finish')

    def test_partial_cache(self):
        self.client.geocode('Start')
        self.handler.reset_mock()
        self.post()
        self.assertEqual([c.args[0].method for c in self.handler.call_args_list], ['GET', 'POST'])
        self.assertEqual(self.handler.call_args_list[0].args[0].url.params['text'], 'Finish')

    def test_directional_coordinate_key_normalization(self):
        self.client.get_route(Coordinates(-100, 0), Coordinates(-80, 0))
        self.client.get_route(Coordinates(-100.0, -0.0), Coordinates(-80.0, 0.0))
        self.assertEqual(self.handler.call_count, 1)
        self.client.get_route(Coordinates(-80, 0), Coordinates(-100, 0))
        self.assertEqual(self.handler.call_count, 2)

    def test_geocode_and_route_failures_not_cached(self):
        for operation in ('geocode', 'route'):
            for failure in ('http', 'timeout', 'malformed', 'empty'):
                with self.subTest(operation=operation, failure=failure):
                    cache.clear()
                    self.handler.reset_mock()
                    if failure == 'timeout':
                        self.handler.side_effect = httpx.ReadTimeout('secret-test-key Authorization')
                    else:
                        self.handler.side_effect = None
                        self.handler.return_value = {
                            'http': httpx.Response(503, text='secret-test-key'),
                            'malformed': httpx.Response(200, json={}),
                            'empty': httpx.Response(200, json={'type': 'FeatureCollection', 'features': []}),
                        }[failure]
                    def invoke():
                        return self.client.geocode('Start') if operation == 'geocode' else self.client.get_route(Coordinates(-100, 0), Coordinates(-80, 0))
                    with self.assertRaises(RoutingProviderError) as error:
                        invoke()
                    self.assertNotIn('secret-test-key', str(error.exception))
                    self.handler.side_effect = self.respond
                    invoke()
                    invoke()
                    self.assertEqual(self.handler.call_count, 2)

    @override_settings(ROUTING_CACHE_TTL_SECONDS=123)
    def test_configured_ttl(self):
        with patch('routes.services.cached_routing.cache.set', wraps=cache.set) as setter:
            self.client.geocode('Start')
        self.assertEqual(setter.call_args.kwargs['timeout'], 123)

    @override_settings(ROUTING_CACHE_TTL_SECONDS=0)
    def test_zero_ttl_does_not_store(self):
        with patch('routes.services.cached_routing.cache.set') as setter:
            self.client.geocode('Start')
            self.client.geocode('Start')
        setter.assert_not_called()
        self.assertEqual(self.handler.call_count, 2)

    def test_corrupt_cache_falls_back(self):
        for value in ({'raw': 'provider'}, 'broken', 123):
            with self.subTest(value=value):
                with patch('routes.services.cached_routing.cache.get', return_value=value):
                    self.client.geocode('Start')
                    self.client.get_route(Coordinates(-100, 0), Coordinates(-80, 0))
        self.assertEqual(self.handler.call_count, 6)

    def test_cache_failures_are_best_effort_and_logs_are_sanitized(self):
        with patch('routes.services.cached_routing.cache.get', side_effect=RuntimeError('secret-test-key Authorization')):
            with patch('routes.services.cached_routing.cache.set', side_effect=RuntimeError('secret-test-key Authorization')):
                with self.assertLogs('routes.services.cached_routing', level='WARNING') as logs:
                    self.post()
        self.assertEqual(self.handler.call_count, 3)
        self.assertNotIn('secret-test-key', ' '.join(logs.output))
        self.assertNotIn('Authorization', ' '.join(logs.output))

    def test_provider_failure_logging_is_sanitized(self):
        self.handler.side_effect = httpx.ReadTimeout('secret-test-key Authorization')
        with self.assertLogs('routes.views', level='WARNING') as logs:
            data = self.post(status=502)
        self.assertNotIn('secret-test-key', str(data) + ' '.join(logs.output))
        self.assertNotIn('Authorization', str(data) + ' '.join(logs.output))

    def test_warm_long_route_requeries_local_prices(self):
        self.distance = 1000 * 1609.344
        fields = dict(opis_truckstop_id=1, truckstop_name='Stop', address='Road', city='City',
                      state='TX', rack_id=1, latitude=Decimal(0))
        first = FuelStation.objects.create(**fields, longitude=Decimal(-92), retail_price=Decimal(2))
        FuelStation.objects.create(**fields, longitude=Decimal(-84), retail_price=Decimal(3))
        cold = self.post(queries=1)
        self.assertEqual(cold['fuel']['summary']['total_fuel_cost'], '110.00')
        self.assertEqual(self.handler.call_count, 3)
        self.handler.reset_mock()
        first.retail_price = Decimal(1)
        first.save(update_fields=['retail_price'])
        warm = self.post(queries=1)
        self.assertEqual(warm['fuel']['summary']['total_fuel_cost'], '70.00')
        self.handler.assert_not_called()

    @patch('routes.views.logger')
    def test_warm_provider_cache_does_not_hide_fuel_infeasibility(self, _logger):
        self.distance = 1000 * 1609.344
        self.assertEqual(self.post(queries=1, status=422)['error']['code'], 'fuel_route_infeasible')
        self.handler.reset_mock()
        self.assertEqual(self.post(queries=1, status=422)['error']['code'], 'fuel_route_infeasible')
        self.handler.assert_not_called()
