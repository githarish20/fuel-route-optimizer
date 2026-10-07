"""Import fuel stations from the assessment CSV.

Usage:
    python manage.py import_fuel_data [--path /path/to/file.csv]

Rules implemented (Section 2):
- Reads the supplied CSV (default: the assessment copy bundled with the project).
- Validates each record (required fields present, price numeric).
- Converts fuel prices to Decimal straight from the string (never float).
- Preserves source information (original identifier, name, address, city,
  state, rack id, price).
- Avoids duplicate stations: the first row for an OPIS Truckstop ID wins;
  later rows for the same ID (and IDs already in the database) are counted
  as duplicates, so re-running the command is idempotent.
- Malformed records never abort the import; they are counted as skipped and
  the first few reasons are printed to stderr.
- Uses bulk insertion (bulk_create in batches) inside a single transaction.
"""

import csv
import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import FuelStation

DEFAULT_CSV_PATH = Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv"

# The dataset's actual column names mapped to FuelStation field names.
CSV_COLUMN_MAP = {
    "OPIS Truckstop ID": "opis_truckstop_id",
    "Truckstop Name": "truckstop_name",
    "Address": "address",
    "City": "city",
    "State": "state",
    "Rack ID": "rack_id",
    "Retail Price": "retail_price",
}

# Values that must be present and non-empty for a row to be importable.
REQUIRED_FIELDS = (
    "opis_truckstop_id",
    "truckstop_name",
    "address",
    "city",
    "state",
    "retail_price",
)

# Prices are plain unsigned decimals (e.g. "3.00733333"); anything else
# is an invalid price.
PRICE_PATTERN = re.compile(r"^\d+(\.\d+)?$")

BATCH_SIZE = 1000


class RowInvalid(Exception):
    """Base class: a row failed validation."""


class RowMissingRequired(RowInvalid):
    """One or more required values are missing/empty."""


class RowInvalidPrice(RowInvalid):
    """The retail price is not a valid decimal number."""


class Command(BaseCommand):
    help = "Import fuel stations from fuel-prices-for-be-assessment.csv."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=str(DEFAULT_CSV_PATH),
            help=(
                "CSV file to import (default: the assessment CSV bundled "
                "with the project)."
            ),
        )

    def handle(self, *args, **options):
        csv_path = Path(options["path"])
        if not csv_path.is_file():
            raise CommandError(f"CSV file not found: {csv_path}")

        total = 0
        imported = 0
        skipped = 0
        duplicates = 0
        invalid_prices = 0
        missing_required = 0
        malformed_details = []
        seen_ids = set()
        batch = []

        self.stdout.write(f"Reading CSV: {csv_path}")

        try:
            fh = csv_path.open(newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Could not open CSV file: {exc}") from exc

        with fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            missing_columns = [c for c in CSV_COLUMN_MAP if c not in header]
            if missing_columns:
                raise CommandError(
                    "CSV is missing required columns: " + ", ".join(missing_columns)
                )

            existing_ids = set(
                FuelStation.objects.values_list("opis_truckstop_id", flat=True)
            )

            with transaction.atomic():
                for row in reader:
                    total += 1
                    line_no = reader.line_num
                    try:
                        record = self._validate_row(row)
                    except RowMissingRequired as exc:
                        skipped += 1
                        missing_required += 1
                        malformed_details.append(f"line {line_no}: {exc}")
                        continue
                    except RowInvalidPrice as exc:
                        skipped += 1
                        invalid_prices += 1
                        malformed_details.append(f"line {line_no}: {exc}")
                        continue
                    except Exception as exc:  # noqa: BLE001
                        # Requirement: malformed records must not abort the
                        # whole import. Details are reported below.
                        skipped += 1
                        malformed_details.append(f"line {line_no}: {exc}")
                        continue

                    station_id = record["opis_truckstop_id"]
                    if station_id in seen_ids or station_id in existing_ids:
                        # First occurrence of a station wins (confirmed rule).
                        duplicates += 1
                        continue
                    seen_ids.add(station_id)
                    batch.append(FuelStation(**record))
                    imported += 1
                    if len(batch) >= BATCH_SIZE:
                        FuelStation.objects.bulk_create(batch, batch_size=BATCH_SIZE)
                        batch = []

                if batch:
                    FuelStation.objects.bulk_create(batch, batch_size=BATCH_SIZE)

        for detail in malformed_details[:10]:
            self.stderr.write(f"Skipped row: {detail}")
        if len(malformed_details) > 10:
            self.stderr.write(
                f"... and {len(malformed_details) - 10} more skipped rows"
            )

        report = (
            ("Total rows", total),
            ("Imported rows", imported),
            ("Skipped rows", skipped),
            ("Duplicate rows", duplicates),
            ("Invalid prices", invalid_prices),
            ("Missing required fields", missing_required),
        )
        for label, value in report:
            self.stdout.write(f"{label}: {value}")

    def _validate_row(self, row):
        """Return cleaned FuelStation field values for one CSV row.

        Raises RowMissingRequired / RowInvalidPrice for invalid records.
        """
        record = {}
        for csv_column, field in CSV_COLUMN_MAP.items():
            value = row.get(csv_column)
            record[field] = "" if value is None else str(value).strip()

        missing = [f for f in REQUIRED_FIELDS if not record[f]]
        if missing:
            raise RowMissingRequired("missing required fields: " + ", ".join(missing))

        if not PRICE_PATTERN.match(record["retail_price"]):
            raise RowInvalidPrice(
                f"invalid retail price: {record['retail_price']!r}"
            )
        # Convert straight from the string — never via float.
        record["retail_price"] = Decimal(record["retail_price"])

        # Rack ID is source information but not required by the spec.
        if not record["rack_id"]:
            record["rack_id"] = None

        return record
