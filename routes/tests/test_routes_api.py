from unittest.mock import call, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from routes.services.routing import GeocodingError, RouteNotFoundError, RoutingProviderError
from .test_route_planner import START, FINISH, ROUTE


class RoutesAPITests(TestCase):
    def setUp(self):
        self.api = APIClient()
        patcher = patch('routes.services.route_planner.OpenRouteServiceClient', autospec=True)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = self.factory.return_value
        self.provider.geocode.side_effect = [START, FINISH]
        self.provider.get_route.return_value = ROUTE

    def post(self, payload=None):
        if payload is None:
            payload = {'start': 'Dallas, TX', 'finish': 'Chicago, IL'}
        return self.api.post('/api/routes/', payload, format='json')

    def test_success_response_and_exact_provider_operations(self):
        response = self.post({'start': ' Dallas, TX ', 'finish': ' Chicago, IL '})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'start': {'query': 'Dallas, TX', 'label': 'Dallas, TX, USA',
                      'coordinates': {'longitude': -96.797, 'latitude': 32.777}},
            'finish': {'query': 'Chicago, IL', 'label': 'Chicago, IL, USA',
                       'coordinates': {'longitude': -87.63, 'latitude': 41.88}},
            'route': {'distance_miles': 400.0, 'duration_hours': 15.0, 'geometry': ROUTE.geometry},
            'fuel': {
                'vehicle': {'mpg': 10, 'max_range_miles': 500, 'tank_capacity_gallons': 50},
                'stops': [],
                'summary': {'total_gallons_consumed': '40.000000',
                            'total_gallons_purchased': '0.000000', 'total_fuel_cost': '0.00'},
            },
        })
        self.factory.assert_called_once_with()
        self.assertEqual(self.provider.mock_calls, [
            call.geocode('Dallas, TX'), call.geocode('Chicago, IL'),
            call.get_route(START.coordinates, FINISH.coordinates),
        ])

    def test_missing_blank_nonstring_and_oversized_fields(self):
        for field in ('start', 'finish'):
            for value in ('missing', '', ' \t\n ', None, 123, 1.5, True, [], {}, 'x' * 256):
                with self.subTest(field=field, value=value):
                    payload = {'start': 'Dallas', 'finish': 'Chicago'}
                    if value == 'missing':
                        del payload[field]
                    else:
                        payload[field] = value
                    response = self.post(payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(field, response.json())
        self.factory.assert_not_called()

    def test_identical_locations(self):
        response = self.post({'start': ' Dallas, TX ', 'finish': 'dALLAS, tx'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('non_field_errors', response.json())
        self.factory.assert_not_called()

    def test_invalid_body_shapes_and_json(self):
        for body in ('[]', '"Dallas"', '42', 'null', '{invalid'):
            with self.subTest(body=body):
                response = self.api.post('/api/routes/', body, content_type='application/json')
                self.assertEqual(response.status_code, 400)
        self.factory.assert_not_called()

    def test_unresolved_locations_return_422_and_stop(self):
        for field, results, expected in (
            ('start', [GeocodingError('secret')], [call.geocode('Dallas, TX')]),
            ('finish', [START, GeocodingError('secret')], [call.geocode('Dallas, TX'), call.geocode('Chicago, IL')]),
        ):
            with self.subTest(field=field):
                self.provider.reset_mock()
                self.provider.geocode.side_effect = results
                response = self.post()
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {'error': {
                    'code': 'location_not_found', 'message': f'Could not resolve the {field} location.',
                }})
                self.assertEqual(self.provider.mock_calls, expected)

    def test_route_not_found(self):
        self.provider.get_route.side_effect = RouteNotFoundError('secret provider internals')
        response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {'error': {
            'code': 'route_not_found', 'message': 'No driving route could be found.',
        }})

    def test_provider_failures_at_each_stage_are_sanitized(self):
        # Step 3 maps HTTP, network and timeout failures to RoutingProviderError.
        for reason in ('HTTP 503', 'connection failed', 'request timed out'):
            for stage in ('start', 'finish', 'route'):
                with self.subTest(reason=reason, stage=stage):
                    self.provider.reset_mock()
                    error = RoutingProviderError(f'{reason}: secret Authorization header')
                    self.provider.geocode.side_effect = {
                        'start': [error], 'finish': [START, error], 'route': [START, FINISH],
                    }[stage]
                    self.provider.get_route.side_effect = error if stage == 'route' else None
                    response = self.post()
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(response.json(), {'error': {
                        'code': 'routing_provider_error', 'message': 'Routing provider could not complete the request.',
                    }})
                    self.assertEqual(len(self.provider.mock_calls), {'start': 1, 'finish': 2, 'route': 3}[stage])

    @override_settings(OPENROUTESERVICE_API_KEY='')
    def test_missing_api_key(self):
        from routes.services.routing import OpenRouteServiceClient
        self.factory.side_effect = OpenRouteServiceClient
        response = self.post()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {'error': {
            'code': 'routing_configuration_error', 'message': 'Routing service is not configured.',
        }})
        self.assertEqual(self.provider.mock_calls, [])

    def test_health_endpoint_preserved(self):
        response = self.api.get('/api/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})
        self.factory.assert_not_called()

    def test_get_routes_is_not_allowed(self):
        self.assertEqual(self.api.get('/api/routes/').status_code, 405)
        self.factory.assert_not_called()
