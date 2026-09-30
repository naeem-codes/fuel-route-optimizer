from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from unittest.mock import Mock

import httpx
from django.test import SimpleTestCase, override_settings

from routes.services.routing import (
    Coordinates, GeocodingError, OpenRouteServiceClient, RouteNotFoundError,
    RoutingConfigurationError, RoutingProviderError,
)


GEOCODING = {
    'type': 'FeatureCollection',
    'features': [{
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [-97.7431, 30.2672]},
        'properties': {'label': 'Austin, TX, USA', 'country_a': 'USA', 'region': 'Texas'},
    }],
}
ROUTE = {
    'type': 'FeatureCollection',
    'features': [{
        'type': 'Feature',
        'geometry': {'type': 'LineString', 'coordinates': [[-97.7431, 30.2672], [-96.797, 32.777]]},
        'properties': {'summary': {'distance': 314123.4, 'duration': 11234.5}},
    }],
}
START = Coordinates(-97.7431, 30.2672)
FINISH = Coordinates(-96.797, 32.777)


class RoutingClientTests(SimpleTestCase):
    def setUp(self):
        self.handler = Mock(return_value=httpx.Response(200, json=GEOCODING))
        self.client = OpenRouteServiceClient(
            api_key='test-secret', transport=httpx.MockTransport(self.handler),
        )

    def invoke(self, operation):
        if operation == 'geocode':
            return self.client.geocode('Austin, TX')
        return self.client.get_route(START, FINISH)

    def assert_one_request(self):
        self.handler.assert_called_once()
        request = self.handler.call_args.args[0]
        self.assertEqual(request.headers['Authorization'], 'test-secret')
        self.assertEqual(request.url.host, 'api.heigit.org')
        self.assertEqual(request.extensions['timeout'], {
            'connect': 5.0, 'read': 15.0, 'write': 15.0, 'pool': 15.0,
        })
        self.assertNotIn('test-secret', str(request.url))
        return request

    def test_geocoding_result_and_request(self):
        result = self.client.geocode('  Austin, TX  ')
        self.assertEqual(result.coordinates, START)
        self.assertEqual((result.label, result.country_code, result.region), ('Austin, TX, USA', 'USA', 'Texas'))
        with self.assertRaises(FrozenInstanceError):
            result.label = 'changed'
        request = self.assert_one_request()
        self.assertEqual(request.method, 'GET')
        self.assertEqual(request.url.path, '/pelias/v1/search')
        self.assertEqual(dict(request.url.params), {'text': 'Austin, TX', 'size': '1', 'boundary.country': 'USA'})

    def test_blank_location_never_calls_provider(self):
        for value in ('', ' \t\n ', None):
            with self.subTest(value=value), self.assertRaises(GeocodingError):
                self.client.geocode(value)
        self.handler.assert_not_called()

    def test_no_geocoding_results(self):
        self.handler.return_value = httpx.Response(200, json={'type': 'FeatureCollection', 'features': []})
        with self.assertRaises(GeocodingError):
            self.client.geocode('Unknown location')
        self.assert_one_request()

    def test_optional_geocoding_metadata(self):
        payload = deepcopy(GEOCODING)
        payload['features'][0]['properties'] = {}
        self.handler.return_value = httpx.Response(200, json=payload)
        result = self.client.geocode('Austin')
        self.assertIsNone(result.label)
        self.assertIsNone(result.country_code)

    def test_routing_result_and_request(self):
        self.handler.return_value = httpx.Response(200, json=ROUTE)
        result = self.client.get_route(START, FINISH)
        self.assertEqual(result.distance_meters, 314123.4)
        self.assertEqual(result.duration_seconds, 11234.5)
        self.assertEqual(result.coordinates, (START, FINISH))
        self.assertEqual(result.geometry, ROUTE['features'][0]['geometry'])
        geometry = result.geometry
        geometry['coordinates'].clear()
        self.assertEqual(len(result.geometry['coordinates']), 2)
        request = self.assert_one_request()
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.url.path, '/openrouteservice/v2/directions/driving-car/geojson')
        self.assertEqual(json.loads(request.content), {
            'coordinates': [START.as_pair(), FINISH.as_pair()], 'units': 'm', 'instructions': False,
        })

    def test_no_route_results(self):
        self.handler.return_value = httpx.Response(200, json={'type': 'FeatureCollection', 'features': []})
        with self.assertRaises(RouteNotFoundError):
            self.client.get_route(START, FINISH)
        self.assert_one_request()

    def test_provider_no_route_error_codes(self):
        for code in (2009, 2010):
            with self.subTest(code=code):
                self.handler.reset_mock()
                self.handler.return_value = httpx.Response(404, json={'error': {'code': code}})
                with self.assertRaises(RouteNotFoundError):
                    self.client.get_route(START, FINISH)
                self.assert_one_request()

    def test_http_failures_are_sanitized_for_both_operations(self):
        for operation in ('geocode', 'route'):
            for status in (301, 401, 403, 404, 429, 500, 503):
                with self.subTest(operation=operation, status=status):
                    self.handler.reset_mock()
                    self.handler.return_value = httpx.Response(status, text='test-secret', headers={'Location': 'https://example.com'})
                    with self.assertRaisesMessage(RoutingProviderError, f'HTTP {status}') as error:
                        self.invoke(operation)
                    self.assertNotIn('test-secret', str(error.exception))
                    self.assert_one_request()

    def test_timeouts_and_network_failures_for_both_operations(self):
        for operation in ('geocode', 'route'):
            for exception in (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError):
                with self.subTest(operation=operation, exception=exception):
                    self.handler.reset_mock()
                    self.handler.side_effect = exception('test-secret')
                    with self.assertRaises(RoutingProviderError) as error:
                        self.invoke(operation)
                    self.assertNotIn('test-secret', str(error.exception))
                    self.assertIsNone(error.exception.__cause__)
                    self.assertTrue(error.exception.__suppress_context__)
                    self.assert_one_request()

    def test_invalid_json_for_both_operations(self):
        for operation in ('geocode', 'route'):
            with self.subTest(operation=operation):
                self.handler.reset_mock()
                self.handler.return_value = httpx.Response(200, text='<html>bad gateway</html>')
                with self.assertRaisesMessage(RoutingProviderError, 'invalid JSON'):
                    self.invoke(operation)
                self.assert_one_request()

    def test_malformed_responses_for_both_operations(self):
        for operation in ('geocode', 'route'):
            for payload in (None, [], {}, {'type': 'FeatureCollection', 'features': None},
                            {'type': 'FeatureCollection', 'features': [None]},
                            {'type': 'FeatureCollection', 'features': [{}]}):
                with self.subTest(operation=operation, payload=payload):
                    self.handler.reset_mock()
                    self.handler.return_value = httpx.Response(200, content=json.dumps(payload))
                    with self.assertRaises(RoutingProviderError):
                        self.invoke(operation)
                    self.assert_one_request()

    def test_invalid_geocoding_geometry_and_metadata(self):
        for field, value in (('geometry', None), ('geometry', {'type': 'LineString', 'coordinates': []}),
                             ('geometry', {'type': 'Point', 'coordinates': [200, 30]}),
                             ('geometry', {'type': 'Point', 'coordinates': ['-97', 30]}),
                             ('properties', {'label': []})):
            with self.subTest(field=field, value=value):
                payload = deepcopy(GEOCODING)
                payload['features'][0][field] = value
                self.handler.return_value = httpx.Response(200, json=payload)
                with self.assertRaises(RoutingProviderError):
                    self.client.geocode('Austin')

    def test_invalid_route_measurements(self):
        for field in ('distance', 'duration'):
            for value in (None, '123', -1, True, float('inf'), float('nan')):
                with self.subTest(field=field, value=value):
                    payload = deepcopy(ROUTE)
                    payload['features'][0]['properties']['summary'][field] = value
                    self.handler.return_value = httpx.Response(200, content=json.dumps(payload))
                    with self.assertRaises(RoutingProviderError):
                        self.client.get_route(START, FINISH)

    def test_invalid_route_geometry(self):
        for geometry in (None, {'type': 'Point', 'coordinates': [1, 2]},
                         {'type': 'LineString', 'coordinates': []},
                         {'type': 'LineString', 'coordinates': [[1, 2], [1, 100]]}):
            with self.subTest(geometry=geometry):
                payload = deepcopy(ROUTE)
                payload['features'][0]['geometry'] = geometry
                self.handler.return_value = httpx.Response(200, json=payload)
                with self.assertRaises(RoutingProviderError):
                    self.client.get_route(START, FINISH)

    def test_invalid_input_coordinates(self):
        for pair in ((181, 0), (0, -91), (float('nan'), 0), (True, 0)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                Coordinates(*pair)
        with self.assertRaises(ValueError):
            self.client.get_route([0, 0], FINISH)
        self.handler.assert_not_called()

    def test_two_geocodes_and_one_route_make_exactly_three_requests(self):
        self.handler.side_effect = [
            httpx.Response(200, json=GEOCODING), httpx.Response(200, json=GEOCODING),
            httpx.Response(200, json=ROUTE),
        ]
        start = self.client.geocode('Austin')
        finish = self.client.geocode('Dallas')
        self.client.get_route(start.coordinates, finish.coordinates)
        self.assertEqual([call.args[0].method for call in self.handler.call_args_list], ['GET', 'GET', 'POST'])

    @override_settings(OPENROUTESERVICE_API_KEY='')
    def test_missing_api_key(self):
        for key in (None, '', '   '):
            with self.subTest(key=key), self.assertRaisesMessage(RoutingConfigurationError, 'OPENROUTESERVICE_API_KEY'):
                OpenRouteServiceClient(api_key=key, transport=httpx.MockTransport(self.handler))
        self.handler.assert_not_called()

    @override_settings(OPENROUTESERVICE_API_KEY='test-secret')
    def test_key_defaults_to_django_setting(self):
        self.client = OpenRouteServiceClient(transport=httpx.MockTransport(self.handler))
        self.client.geocode('Austin')
        self.assert_one_request()
