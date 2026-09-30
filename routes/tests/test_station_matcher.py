from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from routes.models import FuelStation
from routes.services import station_matcher
from routes.services.station_matcher import match_stations


LINE = {'type': 'LineString', 'coordinates': [[-100, 35], [-99, 35]]}


class StationMatcherTests(TestCase):
    def station(self, longitude='-99.5', latitude='35', **changes):
        values = dict(opis_truckstop_id=123, truckstop_name='Truck Stop', address='123 Main St',
                      city='Amarillo', state='TX', rack_id=42, retail_price=Decimal('3.12345678'),
                      longitude=None if longitude is None else Decimal(longitude),
                      latitude=None if latitude is None else Decimal(latitude))
        values.update(changes)
        return FuelStation.objects.create(**values)

    def test_geometry_validation(self):
        for geometry in (None, {}, {'type': 'Point', 'coordinates': [-100, 35]},
                         {'type': 'LineString'}, {'type': 'LineString', 'coordinates': []},
                         {'type': 'LineString', 'coordinates': [[-100, 35]]}):
            with self.subTest(geometry=geometry), self.assertRaises(ValueError):
                match_stations(geometry, [])
        for point in (None, [], [1], [1, 2, 3], ['-100', 35], [True, 35],
                      [181, 35], [-100, 91], [float('nan'), 35], [float('inf'), 35]):
            with self.subTest(point=point), self.assertRaises(ValueError):
                match_stations({'type': 'LineString', 'coordinates': [[-100, 35], point]}, [])

    def test_invalid_radius(self):
        for radius in (-1, True, None, '5', float('inf'), float('nan')):
            with self.subTest(radius=radius), self.assertRaises(ValueError):
                match_stations(LINE, [], radius)

    def test_on_route_metadata_price_and_negative_longitude(self):
        station = self.station()
        result, = match_stations(LINE, FuelStation.objects.all())
        self.assertEqual(result.station_id, station.pk)
        self.assertEqual(result.opis_truckstop_id, 123)
        self.assertEqual((result.truckstop_name, result.address, result.city, result.state),
                         ('Truck Stop', '123 Main St', 'Amarillo', 'TX'))
        self.assertEqual(result.retail_price, Decimal('3.12345678'))
        self.assertEqual((result.longitude, result.latitude), (Decimal('-99.5'), Decimal('35')))
        self.assertAlmostEqual(result.distance_from_route_miles, 0, delta=0.001)
        # One longitude degree at 35 N is approximately 56.6 miles.
        self.assertAlmostEqual(result.distance_along_route_miles, 28.3, delta=0.1)

    def test_corridor_and_known_perpendicular_distance(self):
        nearby = self.station(latitude='35.05')
        self.station(latitude='35.10')
        result, = match_stations(LINE, FuelStation.objects.all())
        self.assertEqual(result.station_id, nearby.pk)
        self.assertAlmostEqual(result.distance_from_route_miles, 3.455, delta=0.01)
        self.assertAlmostEqual(result.distance_along_route_miles, 28.3, delta=0.1)
        self.assertEqual(match_stations(LINE, FuelStation.objects.all(), 3), [])

    def test_missing_either_coordinate_ignored(self):
        stations = [self.station(latitude=None), self.station(longitude=None),
                    self.station(latitude=None, longitude=None)]
        self.assertEqual(match_stations(LINE, stations), [])
        self.assertEqual(match_stations(LINE, FuelStation.objects.all()), [])

    def test_sorted_by_projected_position_not_input_order(self):
        late = self.station(longitude='-99.1')
        early = self.station(longitude='-99.9')
        middle = self.station()
        results = match_stations(LINE, [late, early, middle])
        self.assertEqual([r.station_id for r in results], [early.pk, middle.pk, late.pk])
        distances = [r.distance_along_route_miles for r in results]
        self.assertEqual(distances, sorted(distances))

    def test_endpoint_projections(self):
        before = self.station(longitude='-100.02')
        after = self.station(longitude='-98.98')
        results = match_stations(LINE, [after, before])
        self.assertAlmostEqual(results[0].distance_along_route_miles, 0, delta=0.001)
        self.assertAlmostEqual(results[1].distance_along_route_miles, 56.6, delta=0.1)
        for result in results:
            self.assertAlmostEqual(result.distance_from_route_miles, 1.132, delta=0.01)

    def test_cumulative_distance_and_nearest_segment(self):
        geometry = {'type': 'LineString', 'coordinates': [[-100, 35], [-99, 35], [-99, 36]]}
        station = self.station(longitude='-98.99', latitude='35.5')
        result, = match_stations(geometry, [station])
        # 56.6 miles east, then 34.55 miles north (not origin-to-station distance).
        self.assertAlmostEqual(result.distance_along_route_miles, 91.15, delta=0.2)
        self.assertAlmostEqual(result.distance_from_route_miles, 0.562, delta=0.01)

    def test_queryset_bounding_box_filters_before_projection(self):
        self.station(longitude='-120', latitude='40')
        self.station(latitude=None)
        with patch.object(station_matcher, '_project', wraps=station_matcher._project) as projection:
            with self.assertNumQueries(1) as queries:
                self.assertEqual(match_stations(LINE, FuelStation.objects.all()), [])
            projection.assert_not_called()
        sql = queries.captured_queries[0]['sql']
        self.assertIn('"latitude" >=', sql)
        self.assertIn('"longitude" >=', sql)
        self.assertIn('"longitude" <=', sql)

    def test_segment_bounds_skip_distant_segments(self):
        geometry = {'type': 'LineString', 'coordinates': [[-100, 35], [-99, 35], [-98, 35]]}
        station = self.station(longitude='-99.9')
        with patch.object(station_matcher, '_project', wraps=station_matcher._project) as projection:
            match_stations(geometry, [station])
            self.assertEqual(projection.call_count, 1)

    def test_duplicate_rows_preserved(self):
        first, second = self.station(), self.station()
        results = match_stations(LINE, FuelStation.objects.all())
        self.assertEqual([r.station_id for r in results], [first.pk, second.pk])
        self.assertEqual([r.opis_truckstop_id for r in results], [123, 123])

    def test_repeated_points_and_zero_radius(self):
        geometry = {'type': 'LineString', 'coordinates': [[-100, 35], [-100, 35]]}
        station = self.station(longitude='-100')
        result, = match_stations(geometry, [station], 0)
        self.assertAlmostEqual(result.distance_along_route_miles, 0, delta=0.001)
        self.assertAlmostEqual(result.distance_from_route_miles, 0, delta=0.001)

    def test_loop_prefers_earliest_equal_projection(self):
        geometry = {'type': 'LineString', 'coordinates': [[-100, 35], [-99, 35], [-100, 35]]}
        result, = match_stations(geometry, [self.station(longitude='-100')])
        self.assertAlmostEqual(result.distance_along_route_miles, 0, delta=0.001)

    def test_antimeridian_rejected(self):
        with self.assertRaisesMessage(ValueError, 'Antimeridian'):
            match_stations({'type': 'LineString', 'coordinates': [[179, 35], [-179, 35]]}, [])

    def test_matching_is_local_and_does_not_write(self):
        station = self.station()
        with patch('routes.services.routing.OpenRouteServiceClient', side_effect=AssertionError('Provider used')):
            with patch('httpx.Client.request', side_effect=AssertionError('Network used')):
                with self.assertNumQueries(0):
                    self.assertEqual(len(match_stations(LINE, [station])), 1)
