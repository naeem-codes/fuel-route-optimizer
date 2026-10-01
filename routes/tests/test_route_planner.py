from unittest.mock import Mock, call

from django.test import TestCase

from routes.services.route_planner import LocationNotFoundError, plan_route
from routes.services.routing import Coordinates, GeocodingError, GeocodingResult, OpenRouteServiceClient, RouteResult


START = GeocodingResult(Coordinates(-96.797, 32.777), 'Dallas, TX, USA', 'USA', 'Texas')
FINISH = GeocodingResult(Coordinates(-87.63, 41.88), 'Chicago, IL, USA', 'USA', 'Illinois')
ROUTE = RouteResult(643737.6, 54000.0, (START.coordinates, FINISH.coordinates))


class RoutePlannerTests(TestCase):
    def setUp(self):
        self.client = Mock(spec=OpenRouteServiceClient)
        self.client.geocode.side_effect = [START, FINISH]
        self.client.get_route.return_value = ROUTE

    def test_exact_call_order_and_normalized_result(self):
        result = plan_route('Dallas, TX', 'Chicago, IL', client=self.client)
        self.assertEqual(self.client.mock_calls, [
            call.geocode('Dallas, TX'), call.geocode('Chicago, IL'),
            call.get_route(START.coordinates, FINISH.coordinates),
        ])
        self.assertIs(result.route, ROUTE)
        self.assertEqual(result.as_dict()['route'], {
            'distance_miles': 400.0, 'duration_hours': 15.0, 'geometry': ROUTE.geometry,
        })

    def test_rounding_only_at_response_boundary(self):
        route = RouteResult(123456.789, 12345.6789, ROUTE.coordinates)
        self.client.get_route.return_value = route
        result = plan_route('Dallas', 'Chicago', client=self.client)
        self.assertEqual(result.route.distance_meters, 123456.789)
        self.assertEqual(result.route.duration_seconds, 12345.6789)
        self.assertEqual(result.as_dict()['route']['distance_miles'], 76.712)
        self.assertEqual(result.as_dict()['route']['duration_hours'], 3.4294)

    def test_geocoding_failure_stops_subsequent_operations(self):
        for field, results, expected_calls in (
            ('start', [GeocodingError('private')], [call.geocode('Dallas')]),
            ('finish', [START, GeocodingError('private')], [call.geocode('Dallas'), call.geocode('Chicago')]),
        ):
            with self.subTest(field=field):
                self.client.reset_mock()
                self.client.geocode.side_effect = results
                with self.assertRaises(LocationNotFoundError) as error:
                    plan_route('Dallas', 'Chicago', client=self.client)
                self.assertEqual(error.exception.field, field)
                self.assertNotIn('private', str(error.exception))
                self.assertEqual(self.client.mock_calls, expected_calls)
