"""Preprocess fuel-station coordinates (Section 3).

Usage:
    python manage.py geocode_fuel_stations [--limit N]

Fuel stations are geocoded ONCE as a preprocessing step — never during a
user route request. The command:

- selects stations that have no coordinates and were not already marked
  as failed (restartable: re-running continues where the last run stopped),
- builds the query from dataset fields only ("<name>, <city>, <state>") —
  the original CSV is never modified. The dataset's highway-exit style
  addresses (e.g. "I-44, EXIT 283 & US-69") were verified against the
  provider to break matching, so the query uses the station name (geocoded
  as a POI where it exists in OSM) plus city and state.
- sends requests through the shared geocoding service, which throttles to
  the provider's limit of 1 request/second and caches successful results,
- saves successful coordinates,
- records failed attempts on the station (geocode_failed + geocode_error),
- never re-geocodes a station that already has coordinates.

--limit restricts the number of requests in one run so large batches can be
executed deliberately (the provider's usage policy requires single-threaded,
rate-limited, cached access).
"""

from django.core.management.base import BaseCommand

from routing.models import FuelStation
from routing.services import geocoding
from routing.services.geocoding import GeocodingError


class Command(BaseCommand):
    help = (
        "Geocode fuel stations that have no coordinates yet "
        "(preprocessing step; rate-limited and restartable)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of stations to geocode in this run.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]

        already_geocoded = FuelStation.objects.filter(
            latitude__isnull=False
        ).count()
        previously_failed = FuelStation.objects.filter(
            geocode_failed=True
        ).count()

        pending = FuelStation.objects.filter(
            latitude__isnull=True,
            geocode_failed=False,
        ).order_by("id")
        pending_count = pending.count()
        if limit is not None:
            pending = pending[:limit]

        to_process = len(pending) if limit is not None else pending_count

        self.stdout.write(f"Provider: Nominatim (OpenStreetMap) — {geocoding.OSM_ATTRIBUTION}")
        self.stdout.write(f"Stations already geocoded (never re-geocoded): {already_geocoded}")
        self.stdout.write(f"Stations previously failed (skipped): {previously_failed}")
        self.stdout.write(f"Stations pending: {pending_count}")
        self.stdout.write(f"Requests to send this run: {to_process}")

        succeeded = 0
        failed = 0
        no_match = 0

        for station in pending.iterator():
            # Address built only from information in the dataset.
            address = f"{station.truckstop_name}, {station.city}, {station.state}"
            try:
                result = geocoding.geocode_address(address)
            except GeocodingError as exc:
                station.geocode_failed = True
                station.geocode_error = str(exc)
                station.save(update_fields=["geocode_failed", "geocode_error"])
                failed += 1
                continue

            if result is None:
                station.geocode_failed = True
                station.geocode_error = "No matching location found"
                station.save(update_fields=["geocode_failed", "geocode_error"])
                no_match += 1
                continue

            station.latitude = result["latitude"]
            station.longitude = result["longitude"]
            station.geocode_failed = False
            station.geocode_error = ""
            station.save(
                update_fields=[
                    "latitude",
                    "longitude",
                    "geocode_failed",
                    "geocode_error",
                ]
            )
            succeeded += 1

        self.stdout.write(self.style.SUCCESS("Geocoding run complete."))
        self.stdout.write(f"Succeeded: {succeeded}")
        self.stdout.write(f"No match: {no_match}")
        self.stdout.write(f"Failed (recorded on station): {failed}")
        self.stdout.write(
            "Note: attribution required — "
            f"{geocoding.OSM_ATTRIBUTION}"
        )
