from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
from itertools import permutations, product
from unittest import TestCase
from unittest.mock import patch

from routes.services.fuel_optimizer import (
    FuelCandidate, FuelOptimizationInputError, FuelRouteInfeasibleError, optimize_fuel,
)


D = Decimal


def station(mile, price, identifier=1, name='Truck Stop'):
    return FuelCandidate(identifier, name, D(str(price)), D(str(mile)))


def reference_cost(distance, stations, capacity):
    """Tiny independent exhaustive integer-gallon DP; mpg=1, integer miles.

    Enumerate every legal purchase amount at every stop, retaining minimum cost
    for each fuel level. Only used with small integral scenarios in tests.
    """
    states = {capacity: D(0)}
    previous = 0
    for mile, price in sorted(stations) + [(distance, None)]:
        arrivals = {}
        for fuel, cost in states.items():
            remaining = fuel - (mile - previous)
            if remaining < 0:
                continue
            for purchase in range(capacity - remaining + 1) if price is not None else (0,):
                new_fuel = remaining + purchase
                new_cost = cost + purchase * (price or 0)
                if new_fuel not in arrivals or new_cost < arrivals[new_fuel]:
                    arrivals[new_fuel] = new_cost
        states, previous = arrivals, mile
    return min(states.values()) if states else None


class FuelOptimizerTests(TestCase):
    def test_noniterable_candidates_raise_domain_error(self):
        for candidates in (None, 42):
            with self.subTest(candidates=candidates), self.assertRaises(FuelOptimizationInputError):
                optimize_fuel(600, candidates)

    def test_strict_station_scalar_types(self):
        for candidate in (
            FuelCandidate(1, 'Stop', D(3), True),
            FuelCandidate(1, 'Stop', True, D(400)),
            FuelCandidate(True, 'Stop', D(3), D(400)),
            FuelCandidate(1.0, 'Stop', D(3), D(400)),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(FuelOptimizationInputError):
                optimize_fuel(600, [candidate])

    def test_cheap_origin_cannot_overfill_free_starting_tank(self):
        plan = self.assert_plan(800, [station(0, '0.000001', 1), station(500, 4, 2)],
                                [(500, 30)], 120)
        self.assertEqual([stop.station_id for stop in plan.stops], [2])
        with self.assertRaises(FuelRouteInfeasibleError):
            optimize_fuel(800, [station(0, '0.000001')])

    def test_initial_fuel_carried_past_multiple_expensive_stations(self):
        plan = self.assert_plan(650, [station(100, 9, 1), station(200, 8, 2),
                                     station(300, 7, 3), station(450, 3, 4)],
                                [(450, 15)], 45)
        # Arrive with five free gallons; only fifteen of the twenty needed are bought.
        self.assertEqual([stop.station_id for stop in plan.stops], [4])

    def test_same_mile_all_permutations_choose_lowest_cheap_id(self):
        candidates = [station(500, 5, 1), station(500, 2, 9),
                      station(500, 2, 3), station(500, 2, 7)]
        baseline = optimize_fuel(800, candidates)
        for ordering in permutations(candidates):
            with self.subTest(ids=[s.station_id for s in ordering]):
                plan = self.assert_plan(800, ordering, [(500, 30)], 60)
                self.assertEqual(plan, baseline)
                self.assertEqual(plan.stops[0].station_id, 3)

    def test_fractional_custom_vehicle_exact_arithmetic(self):
        self.assert_plan('12.5', [station('4.5', '3.125', 1), station('8.5', '2.75', 2)],
                         [('4.5', '1.25'), ('8.5', 2)], '9.40625', mpg=2, max_range=6)

    def test_no_stop_structure_and_immutability(self):
        plan = self.assert_plan(125, [], expected=[], cost=0)
        self.assertEqual(plan.stops, ())
        self.assertIsInstance(plan.stops, tuple)
        self.assertEqual(plan.total_gallons_purchased, D('0'))
        self.assertEqual(plan.total_fuel_cost, D('0'))
        self.assertEqual(plan.total_gallons_consumed, D('12.5'))
        with self.assertRaises(FrozenInstanceError):
            plan.stops = ()

    def test_cheaper_context_station_with_zero_purchase_is_omitted(self):
        # Mile 300 is the next cheaper target from mile 100, but free fuel reaches
        # both it and the still-cheaper mile 500 station without a purchase.
        plan = self.assert_plan(700, [station(100, 9, 1), station(300, 5, 2), station(500, 2, 3)],
                                [(500, 20)], 40)
        self.assertEqual([stop.station_id for stop in plan.stops], [3])

    def test_fractional_grid_reference_optimality(self):
        # Independently enumerate every half-gallon purchase through the integer
        # DP in scaled units. At mpg=1, one tick is 0.5 mile and 0.5 gallon.
        # Cost per tick is price/2, not a rounded production purchase quantity.
        feasible = infeasible = fractional = 0
        tick = D('0.5')
        for prices in product((None, D('1.25'), D('3.75')), repeat=3):
            raw = [(mile, price) for mile, price in zip((2, 3, 5), prices) if price is not None]
            expected = reference_cost(8, [(mile, price * tick) for mile, price in raw], 3)
            candidates = [station(D(mile) * tick, price, mile) for mile, price in raw]
            with self.subTest(prices=prices):
                if expected is None:
                    infeasible += 1
                    with self.assertRaises(FuelRouteInfeasibleError):
                        optimize_fuel(4, candidates, mpg=1, max_range_miles=D('1.5'))
                else:
                    feasible += 1
                    plan = self.assert_plan(4, candidates, cost=expected, mpg=1, max_range=D('1.5'))
                    fractional += any(stop.gallons_purchased % 1 for stop in plan.stops)
        self.assertGreater(feasible, 0)
        self.assertGreater(infeasible, 0)
        self.assertGreater(fractional, 0)

    def assert_plan(self, distance, candidates, expected=None, cost=None, mpg=10, max_range=500):
        plan = optimize_fuel(distance, candidates, mpg=mpg, max_range_miles=max_range)
        # Independently simulate every selected stop and the final leg.
        with localcontext() as context:
            context.prec = 40
            efficiency, capacity = D(str(mpg)), D(str(max_range)) / D(str(mpg))
            fuel, previous = capacity, D(0)
            for stop in plan.stops:
                self.assertGreaterEqual(stop.route_mile, previous)
                fuel -= (stop.route_mile - previous) / efficiency
                self.assertGreaterEqual(fuel, 0)
                self.assertGreater(stop.gallons_purchased, 0)
                fuel += stop.gallons_purchased
                self.assertLessEqual(fuel, capacity)
                self.assertEqual(stop.fuel_cost, stop.gallons_purchased * stop.price_per_gallon)
                previous = stop.route_mile
            fuel -= (D(str(distance)) - previous) / efficiency
            self.assertGreaterEqual(fuel, 0)
            self.assertEqual(plan.total_gallons_purchased, sum((s.gallons_purchased for s in plan.stops), D(0)))
            self.assertEqual(plan.total_fuel_cost, sum((s.fuel_cost for s in plan.stops), D(0)))
            self.assertEqual(plan.total_gallons_consumed, D(str(distance)) / efficiency)
            self.assertEqual(plan.total_gallons_purchased, max(D(0), (D(str(distance)) - D(str(max_range))) / efficiency))
        if expected is not None:
            self.assertEqual([(s.route_mile, s.gallons_purchased) for s in plan.stops],
                             [(D(str(m)), D(str(g))) for m, g in expected])
        if cost is not None:
            self.assertEqual(plan.total_fuel_cost, D(str(cost)))
        return plan

    def test_invalid_vehicle_and_route_numbers(self):
        for field in ('route_distance_miles', 'mpg', 'max_range_miles'):
            for value in (0, -1, 'NaN', 'Infinity', '-Infinity', float('nan'), float('inf'), None, True, 'bad'):
                kwargs = dict(route_distance_miles=1000, candidates=[], mpg=10, max_range_miles=500)
                kwargs[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(FuelOptimizationInputError):
                    optimize_fuel(**kwargs)

    def test_invalid_station_distances_and_prices_even_on_short_route(self):
        for mile in ('NaN', 'Infinity', '-Infinity', -1, '100.000001'):
            with self.subTest(mile=mile), self.assertRaises(FuelOptimizationInputError):
                optimize_fuel(100, [station(mile, 3)])
        for price in (0, -1, 'NaN', 'Infinity', '-Infinity'):
            with self.subTest(price=price), self.assertRaises(FuelOptimizationInputError):
                optimize_fuel(100, [station(50, price)])

    def test_invalid_candidate_types(self):
        for candidate in ({}, FuelCandidate('id', 'Name', D(3), D(50)), FuelCandidate(1, 'Name', None, D(50))):
            with self.subTest(candidate=candidate), self.assertRaises(FuelOptimizationInputError):
                optimize_fuel(100, [candidate])

    def test_no_stop_routes(self):
        for distance in (1, '499.999999', 500):
            with self.subTest(distance=distance):
                self.assert_plan(distance, [], expected=[], cost=0)
                self.assert_plan(distance, [station(0, 1), station(distance, 2)], expected=[], cost=0)

    def test_destination_just_beyond_initial_range(self):
        self.assert_plan('500.000001', [station(500, 3)], [(500, '0.0000001')], '0.0000003')
        with self.assertRaises(FuelRouteInfeasibleError):
            optimize_fuel('500.000001', [])

    def test_infeasible_gaps(self):
        scenarios = (
            (1000, []), (1000, [station(501, 2)]),
            (1500, [station(400, 2), station(901, 1), station(1300, 1)]),
            (1001, [station(500, 2)]),
            (1100, [station(400, 2), station('900.000001', 1)]),
        )
        for distance, candidates in scenarios:
            with self.subTest(distance=distance, candidates=candidates), self.assertRaises(FuelRouteInfeasibleError):
                optimize_fuel(distance, candidates)

    def test_sparse_stations_at_exact_range(self):
        self.assert_plan(1500, [station(500, 3), station(1000, 4)], [(500, 50), (1000, 50)], 350)

    def test_cheaper_ahead_buys_only_enough_to_reach_it(self):
        self.assert_plan(1000, [station(400, 5), station(600, 3)], [(400, 10), (600, 40)], 170)

    def test_cheapest_reachable_fills_and_skips_expensive(self):
        self.assert_plan(1200, [station(400, 2), station(700, 6), station(900, 5)],
                         [(400, 40), (900, 30)], 230)

    def test_destination_aware_small_final_purchase(self):
        self.assert_plan(550, [station(400, 4)], [(400, 5)], 20)

    def test_existing_origin_fuel_skips_unneeded_stations(self):
        self.assert_plan(800, [station(100, 9), station(300, 8), station(500, 2)], [(500, 30)], 60)

    def test_leftover_fuel_changes_next_purchase_amount(self):
        self.assert_plan(1000, [station(300, 1), station(600, 4), station(800, 3)],
                         [(300, 30), (800, 20)], 90)

    def test_price_patterns(self):
        for name, prices, purchases, cost in (
            ('decreasing', [3, 2, 1], [(400, 30), (800, 40), (1200, 30)], 200),
            ('increasing', [1, 2, 3], [(400, 40), (800, 40), (1200, 20)], 180),
            ('equal', [2, 2, 2], [(400, 40), (800, 40), (1200, 20)], 200),
        ):
            with self.subTest(pattern=name):
                self.assert_plan(1500, [station(m, p) for m, p in zip((400, 800, 1200), prices)], purchases, cost)

    def test_alternating_prices_and_strategically_cheap_station(self):
        self.assert_plan(1400, [station(400, 5), station(500, 1), station(900, 9), station(1000, 8)],
                         [(500, 50), (1000, 40)], 370)

    def test_long_route_exact_stop_sequence_and_totals(self):
        plan = self.assert_plan(1800, [station(400, 4), station(800, 3), station(1200, 2), station(1600, 1)],
                                [(400, 30), (800, 40), (1200, 40), (1600, 20)], 340)
        self.assertEqual(plan.total_gallons_purchased, D(130))

    def test_same_mile_prefers_cheapest_then_lowest_id(self):
        candidates = [station(500, 5, 1), station(500, 2, 9, 'Chosen'), station(500, 2, 10)]
        original = list(candidates)
        plan = self.assert_plan(800, candidates, [(500, 30)], 60)
        self.assertEqual(plan.stops[0].station_id, 9)
        self.assertEqual(plan.stops[0].station_name, 'Chosen')
        self.assertEqual(candidates, original)
        self.assertEqual(optimize_fuel(800, reversed(candidates)), plan)

    def test_unsorted_input_sorted_without_mutating_it(self):
        candidates = [station(800, 2), station(400, 3), station(1200, 1)]
        original = list(candidates)
        self.assert_plan(1500, candidates, [(400, 30), (800, 40), (1200, 30)], 200)
        self.assertEqual(candidates, original)

    def test_cheaper_station_barely_within_reach(self):
        self.assert_plan(1100, [station(400, 5), station('899.999999', 3)],
                         [(400, '39.9999999'), ('899.999999', '20.0000001')], '259.9999998')

    def test_cheaper_station_just_outside_reach_requires_expensive_bridge(self):
        self.assert_plan(1100, [station(400, 5), station(700, 6), station('900.000001', 3)],
                         [(400, 40), (700, '0.0000001'), ('900.000001', '19.9999999')], '260.0000003')

    def test_destination_before_cheaper_station_rejects_out_of_route_candidate(self):
        with self.assertRaises(FuelOptimizationInputError):
            optimize_fuel(650, [station(400, 5), station(700, 1)])
        self.assert_plan(650, [station(400, 5)], [(400, 15)], 75)

    def test_station_at_destination_never_gets_a_purchase(self):
        self.assert_plan(650, [station(400, 5), station(650, 1)], [(400, 15)], 75)

    def test_expensive_station_unavoidable(self):
        self.assert_plan(1100, [station(400, 2), station(900, 9)], [(400, 40), (900, 20)], 260)

    def test_exact_decimal_cost_and_float_boundary_conversion(self):
        candidate = FuelCandidate(7, 'Precise Stop', 3.12345678, 500.0)
        plan = self.assert_plan(501.2, [candidate], [(500, '0.12')], '0.3748148136')
        self.assertIsInstance(plan.stops[0].price_per_gallon, Decimal)
        self.assertEqual(plan.stops[0].price_per_gallon, D('3.12345678'))
        with self.assertRaises(FrozenInstanceError):
            plan.total_fuel_cost = D(0)

    def test_custom_vehicle_parameters(self):
        self.assert_plan(120, [station(40, 3), station(80, 2)], [(40, 4), (80, 8)], 28, mpg=5, max_range=60)

    def test_nonterminating_mpg_does_not_change_feasibility(self):
        plan = optimize_fuel(1500, [station(500, 3), station(1000, 3)], mpg=3)
        self.assertEqual([s.route_mile for s in plan.stops], [D(500), D(1000)])
        with localcontext() as context:
            context.prec = 40
            self.assertEqual(plan.stops[0].gallons_purchased, D(500) / 3)
        with self.assertRaises(FuelRouteInfeasibleError):
            optimize_fuel('1500.000001', [station(500, 3), station(1000, 3)], mpg=3)

    def test_exhaustive_reference_optimality(self):
        # 3^6 = 729 price/availability configurations, including infeasible gaps.
        for prices in product((None, 1, 3), repeat=6):
            raw = [(mile, price) for mile, price in enumerate(prices, 1) if price is not None]
            expected = reference_cost(8, raw, 3)
            candidates = [station(mile, price, mile) for mile, price in raw]
            with self.subTest(prices=prices):
                if expected is None:
                    with self.assertRaises(FuelRouteInfeasibleError):
                        optimize_fuel(8, candidates, mpg=1, max_range_miles=3)
                else:
                    self.assert_plan(8, candidates, cost=expected, mpg=1, max_range=3)

    def test_thousands_of_candidates(self):
        candidates = [station(mile, 3, mile) for mile in range(1, 8001)]
        plan = self.assert_plan(8100, candidates, cost=2280)
        self.assertEqual(plan.total_gallons_purchased, D(760))

    def test_no_database_or_network_access(self):
        with patch('django.db.backends.utils.CursorWrapper.execute', side_effect=AssertionError('DB access')):
            with patch('httpx.Client.request', side_effect=AssertionError('Network access')):
                self.assert_plan(600, [station(500, 3)], [(500, 10)], 30)
