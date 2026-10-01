import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routes.models import FuelStation


HEADERS = {
    'OPIS Truckstop ID': 'opis_truckstop_id',
    'Truckstop Name': 'truckstop_name',
    'Address': 'address',
    'City': 'city',
    'State': 'state',
    'Rack ID': 'rack_id',
    'Retail Price': 'retail_price',
}
BATCH_SIZE = 500


def parse_row(row, row_number):
    values = {field: row[header].strip() for header, field in HEADERS.items()}
    for header in ('OPIS Truckstop ID', 'Rack ID'):
        field = HEADERS[header]
        try:
            if not re.fullmatch(r'[+-]?[0-9]+', values[field]):
                raise ValueError
            values[field] = int(values[field])
        except ValueError as exc:
            raise CommandError(f'CSV row {row_number}: invalid {header}.') from exc

    try:
        price = Decimal(values['retail_price'])
        if not price.is_finite() or price <= 0:
            raise InvalidOperation
        values['retail_price'] = price
    except InvalidOperation as exc:
        raise CommandError(f'CSV row {row_number}: invalid Retail Price.') from exc

    station = FuelStation(**values)
    try:
        # Validate lengths, numeric ranges and decimal precision before insertion.
        station.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as exc:
        raise CommandError(f'CSV row {row_number}: {exc}') from exc
    return station


class Command(BaseCommand):
    help = 'Import every fuel price CSV row, optionally replacing existing records.'

    def add_arguments(self, parser):
        parser.add_argument('csv_path', type=Path)
        parser.add_argument(
            '--replace', action='store_true',
            help='Replace all FuelStation records atomically.',
        )

    def handle(self, *args, **options):
        path = options['csv_path']
        if not path.is_file():
            raise CommandError(f'CSV path is not an existing file: {path}')

        reader = None
        try:
            # utf-8-sig also accepts ordinary UTF-8 and removes an optional BOM.
            with path.open(encoding='utf-8-sig', newline='') as source:
                reader = csv.reader(source, strict=True)
                headers = [value.strip() for value in next(reader, [])]
                missing = HEADERS.keys() - set(headers)
                if missing:
                    raise CommandError(f'Missing required CSV headers: {", ".join(sorted(missing))}')
                if len(headers) != len(set(headers)):
                    raise CommandError('CSV contains duplicate headers.')

                count = 0
                batch = []
                with transaction.atomic():
                    if options['replace']:
                        FuelStation.objects.all().delete()
                    for cells in reader:
                        row_number = reader.line_num
                        if len(cells) != len(headers):
                            raise CommandError(f'CSV row {row_number}: incorrect number of columns.')
                        batch.append(parse_row(dict(zip(headers, cells)), row_number))
                        count += 1
                        if len(batch) == BATCH_SIZE:
                            FuelStation.objects.bulk_create(batch, batch_size=BATCH_SIZE)
                            batch.clear()
                    if batch:
                        FuelStation.objects.bulk_create(batch, batch_size=BATCH_SIZE)
        except (OSError, UnicodeError, csv.Error) as exc:
            line = reader.line_num if reader is not None else 0
            raise CommandError(f'Cannot read CSV {path} near row {line}: {exc}') from exc

        self.stdout.write(self.style.SUCCESS(f'Imported {count} fuel price rows.'))
