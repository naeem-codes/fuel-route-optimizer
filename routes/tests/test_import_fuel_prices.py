import csv
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase

from routes.models import FuelStation


HEADERS = [
    'OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City',
    'State', 'Rack ID', 'Retail Price',
]
ROW = ['123', 'Truck Stop', '123 Main St', 'Austin', 'TX', '42', '3.12345678']


class ImportFuelPricesTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'prices.csv'

    def write_csv(self, rows, headers=HEADERS):
        with self.path.open('w', encoding='utf-8', newline='') as source:
            writer = csv.writer(source)
            writer.writerow(headers)
            writer.writerows(rows)

    def run_import(self, replace=False):
        output = StringIO()
        call_command('import_fuel_prices', str(self.path), replace=replace, stdout=output)
        return output.getvalue()

    def test_import_preserves_all_rows_duplicate_ids_and_exact_prices(self):
        second = ROW.copy()
        second[-1] = '4.65432198'
        self.write_csv([ROW, second, ROW])
        self.assertIn('Imported 3 fuel price rows.', self.run_import())
        stations = list(FuelStation.objects.order_by('pk'))
        self.assertEqual(len(stations), 3)
        self.assertEqual([s.opis_truckstop_id for s in stations], [123, 123, 123])
        self.assertEqual([s.retail_price for s in stations], [
            Decimal('3.12345678'), Decimal('4.65432198'), Decimal('3.12345678'),
        ])
        self.assertEqual(
            (stations[0].truckstop_name, stations[0].address, stations[0].city,
             stations[0].state, stations[0].rack_id),
            ('Truck Stop', '123 Main St', 'Austin', 'TX', 42),
        )

    def test_default_import_appends(self):
        self.write_csv([ROW])
        self.run_import()
        self.run_import()
        self.assertEqual(FuelStation.objects.count(), 2)

    def test_replace_removes_existing_records(self):
        self.write_csv([ROW])
        self.run_import()
        old_pk = FuelStation.objects.get().pk
        self.write_csv([['456', *ROW[1:]]])
        self.run_import(replace=True)
        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertFalse(FuelStation.objects.filter(pk=old_pk).exists())
        self.assertEqual(FuelStation.objects.get().opis_truckstop_id, 456)

    def test_missing_required_headers(self):
        for header in HEADERS:
            with self.subTest(header=header):
                self.write_csv([], [h for h in HEADERS if h != header])
                with self.assertRaisesMessage(CommandError, header):
                    self.run_import()

    def test_malformed_numeric_values(self):
        for column, value in [(0, 'abc'), (5, '4.2'), (6, 'oops'), (6, 'NaN'),
                              (6, 'Infinity'), (6, '3.123456789'), (0, '9223372036854775808')]:
            with self.subTest(column=column, value=value):
                row = ROW.copy()
                row[column] = value
                self.write_csv([ROW, row])
                with self.assertRaisesMessage(CommandError, 'CSV row 3:'):
                    self.run_import()
                self.assertEqual(FuelStation.objects.count(), 0)

    def test_late_failure_rolls_back_batches_and_preserves_existing_records(self):
        self.write_csv([ROW])
        self.run_import()
        original = list(FuelStation.objects.values())
        for replace in (False, True):
            with self.subTest(replace=replace):
                self.write_csv([ROW] * 501 + [['bad', *ROW[1:]]])
                with self.assertRaisesMessage(CommandError, 'CSV row 503:'):
                    self.run_import(replace=replace)
                self.assertEqual(list(FuelStation.objects.values()), original)

    def test_database_failure_rolls_back_replacement(self):
        self.write_csv([ROW])
        self.run_import()
        original = list(FuelStation.objects.values())
        with patch('django.db.models.query.QuerySet.bulk_create', side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                self.run_import(replace=True)
        self.assertEqual(list(FuelStation.objects.values()), original)

    def test_whitespace_bom_and_extra_columns(self):
        self.write_csv([[f' {value} ' for value in ROW] + ['ignored']], HEADERS + ['Extra'])
        self.path.write_text('\ufeff' + self.path.read_text(), encoding='utf-8')
        self.run_import()
        self.assertEqual(FuelStation.objects.get().truckstop_name, 'Truck Stop')

    def test_missing_file_and_directory(self):
        for path in (self.path, self.path.parent):
            with self.subTest(path=path):
                with self.assertRaisesMessage(CommandError, 'not an existing file'):
                    call_command('import_fuel_prices', str(path))

    def test_unreadable_file(self):
        self.write_csv([ROW])
        with patch.object(Path, 'open', side_effect=PermissionError('Permission denied')):
            with self.assertRaisesMessage(CommandError, 'Cannot read CSV'):
                self.run_import()

    def test_invalid_row_shape_and_text(self):
        for row in (ROW[:-1], ROW + ['extra'], [], ['', *ROW[1:]],
                    [*ROW[:4], 'TEX', *ROW[5:]],
                    [ROW[0], 'x' * 256, *ROW[2:]]):
            with self.subTest(row=row):
                self.write_csv([row])
                with self.assertRaisesMessage(CommandError, 'CSV row 2:'):
                    self.run_import()

    def test_malformed_csv_quoting(self):
        self.write_csv([])
        with self.path.open('a') as source:
            source.write('"unterminated')
        with self.assertRaisesMessage(CommandError, 'Cannot read CSV'):
            self.run_import()

    def test_duplicate_headers(self):
        self.write_csv([], HEADERS + [HEADERS[0]])
        with self.assertRaisesMessage(CommandError, 'duplicate headers'):
            self.run_import()

    def test_header_only_csv_is_valid_empty_dataset(self):
        self.write_csv([ROW])
        self.run_import()
        self.write_csv([])
        self.assertIn('Imported 0 fuel price rows.', self.run_import(replace=True))
        self.assertEqual(FuelStation.objects.count(), 0)
