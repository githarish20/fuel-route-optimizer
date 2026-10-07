"""Tests for the routing application.

Section 1: health endpoint.
Section 2: fuel data import (valid import, price parsing, duplicates,
missing required values, invalid prices).
Section 3: geocoding service (external API always mocked).
Section 4: routing service (external API always mocked).
Section 5: stations along a route (local geometry only).
Section 6: fuel-stop optimization (local, deterministic, Decimal money).
Section 7: POST /api/v1/routes/ (external providers always mocked).

All import tests use small CSV files created inside the tests — they never
read the production assessment CSV.
"""

import builtins
import io
import json
import math
import os
import tempfile
import urllib.error
import urllib.parse
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from routing.models import FuelStation
from routing.services import fuel_optimizer
from routing.services import geocoding
from routing.services import route_stations
from routing.services import routing as routing_service

CSV_HEADER = (
    "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
)

# Counter labels exactly as printed by the import_fuel_data command.
REPORT_LABELS = (
    "Total rows",
    "Imported rows",
    "Skipped rows",
    "Duplicate rows",
    "Invalid prices",
    "Missing required fields",
)


def write_test_csv(*rows):
    """Create a temporary CSV with the real header; returns its path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", newline="", delete=False, encoding="utf-8"
    )
    with tmp:
        tmp.write(CSV_HEADER)
        for row in rows:
            tmp.write(row + "\n")
    return tmp.name


def run_import(csv_path):
    """Run import_fuel_data on a test CSV; return parsed report counters."""
    stdout = io.StringIO()
    call_command(
        "import_fuel_data",
        "--path",
        csv_path,
        stdout=stdout,
        stderr=io.StringIO(),  # skip details are asserted via counters instead
    )
    report = {}
    for line in stdout.getvalue().splitlines():
        label, sep, value = line.rpartition(": ")
        if sep and value.isdigit():
            report[label] = int(value)
    return report


class HealthEndpointTests(TestCase):
    """Tests for GET /api/v1/health/."""

    def test_health_returns_200_with_ok_status(self):
        response = self.client.get("/api/v1/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class ImportTestCase(TestCase):
    """Base class: test CSV lifecycle helpers."""

    def setUp(self):
        self._csv_paths = []

    def tearDown(self):
        for path in self._csv_paths:
            if os.path.exists(path):
                os.unlink(path)

    def make_csv(self, *rows):
        path = write_test_csv(*rows)
        self._csv_paths.append(path)
        return path

    def import_csv(self, *rows):
        return run_import(self.make_csv(*rows))


class ValidImportTests(ImportTestCase):
    """1. Valid fuel station import."""

    def test_valid_row_is_imported_with_all_source_fields(self):
        report = self.import_csv(
            '101,TEST STATION ONE,"I-95, EXIT 10",Testville,TX,100,3.459'
        )

        self.assertEqual(
            report,
            {
                "Total rows": 1,
                "Imported rows": 1,
                "Skipped rows": 0,
                "Duplicate rows": 0,
                "Invalid prices": 0,
                "Missing required fields": 0,
            },
        )
        station = FuelStation.objects.get()
        self.assertEqual(station.opis_truckstop_id, "101")
        self.assertEqual(station.truckstop_name, "TEST STATION ONE")
        self.assertEqual(station.address, "I-95, EXIT 10")
        self.assertEqual(station.city, "Testville")
        self.assertEqual(station.state, "TX")
        self.assertEqual(station.rack_id, "100")
        self.assertEqual(station.retail_price, Decimal("3.459"))
        self.assertIsNone(station.latitude)
        self.assertIsNone(station.longitude)

    def test_source_whitespace_padding_is_stripped(self):
        self.import_csv("102,TEST TWO,US-50,Padded City   ,CO,200,3.100")

        station = FuelStation.objects.get()
        self.assertEqual(station.city, "Padded City")


class PriceParsingTests(ImportTestCase):
    """2. Price parsing."""

    def test_price_is_stored_as_exact_decimal_not_float(self):
        self.import_csv("103,PRECISION TEST,US-1,Denver,CO,300,3.00733333")

        station = FuelStation.objects.get()
        self.assertIsInstance(station.retail_price, Decimal)
        self.assertEqual(station.retail_price, Decimal("3.00733333"))

    def test_simple_price_parses_exactly(self):
        self.import_csv("104,SIMPLE PRICE,US-2,Aurora,CO,301,3.899")

        station = FuelStation.objects.get()
        self.assertEqual(station.retail_price, Decimal("3.899"))


class DuplicateHandlingTests(ImportTestCase):
    """5.3 Duplicate handling: one row per station identifier."""

    def test_repeated_station_id_imports_once_and_keeps_first_price(self):
        report = self.import_csv(
            "201,DUP STATION,US-3,First City,KS,400,3.111",
            "201,DUP STATION,US-3,First City,KS,400,9.999",
        )

        self.assertEqual(
            report,
            {
                "Total rows": 2,
                "Imported rows": 1,
                "Skipped rows": 0,
                "Duplicate rows": 1,
                "Invalid prices": 0,
                "Missing required fields": 0,
            },
        )
        station = FuelStation.objects.get(opis_truckstop_id="201")
        self.assertEqual(station.retail_price, Decimal("3.111"))

    def test_exact_duplicate_rows_are_not_imported_twice(self):
        row = "202,EXACT DUP,US-4,Plainview,TX,410,3.222"
        report = self.import_csv(row, row, row)

        self.assertEqual(report["Imported rows"], 1)
        self.assertEqual(report["Duplicate rows"], 2)
        self.assertEqual(FuelStation.objects.count(), 1)

    def test_second_run_is_idempotent(self):
        rows = (
            "203,STATION A,US-5,Alpha,NE,420,3.333",
            "204,STATION B,US-6,Beta,NE,421,3.444",
        )
        path = self.make_csv(*rows)
        first = run_import(path)
        second = run_import(path)

        self.assertEqual(first["Imported rows"], 2)
        self.assertEqual(second["Imported rows"], 0)
        self.assertEqual(second["Duplicate rows"], 2)
        self.assertEqual(second["Total rows"], 2)
        self.assertEqual(FuelStation.objects.count(), 2)


class MissingRequiredValueTests(ImportTestCase):
    """5.4 Missing required values (and short/malformed rows)."""

    def test_rows_with_missing_required_values_are_skipped_without_crashing(self):
        report = self.import_csv(
            "301,MISSING CITY,US-7,,MO,430,3.500",  # empty city
            "302,MISSING PRICE,US-8,Clinton,MO,431,",  # empty price
            "303,ONLY,TWO",  # malformed short row
            "304,VALID STATION,US-9,Sedalia,MO,432,3.555",  # valid row still imports
        )

        self.assertEqual(
            report,
            {
                "Total rows": 4,
                "Imported rows": 1,
                "Skipped rows": 3,
                "Duplicate rows": 0,
                "Invalid prices": 0,
                "Missing required fields": 3,
            },
        )
        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(
            FuelStation.objects.get().opis_truckstop_id, "304"
        )


class InvalidPriceTests(ImportTestCase):
    """5.5 Invalid price handling."""

    def test_invalid_prices_are_skipped_without_crashing(self):
        report = self.import_csv(
            "401,BAD PRICE,US-10,Carthage,MO,440,abc",  # non-numeric
            '402,COMMA PRICE,US-11,Joplin,MO,441,"3,49"',  # decimal comma
            "403,GOOD PRICE,US-12,Neosho,MO,442,3.666",  # valid row still imports
        )

        self.assertEqual(
            report,
            {
                "Total rows": 3,
                "Imported rows": 1,
                "Skipped rows": 2,
                "Duplicate rows": 0,
                "Invalid prices": 2,
                "Missing required fields": 0,
            },
        )
        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(
            FuelStation.objects.get().opis_truckstop_id, "403"
        )


class GeocodingServiceTests(TestCase):
    """Section 3: the external geocoding API is always mocked.

    Covers: successful geocoding, invalid response, HTTP failure, timeout,
    cached result. No test ever performs a real network call.
    """

    SUCCESS_BODY = json.dumps(
        [
            {
                "place_id": 100,
                "licence": "Data \u00a9 OpenStreetMap contributors, ODbL 1.0.",
                "lat": "45.1234567",
                "lon": "-93.7654321",
                "display_name": "Test Place, Minnesota, United States",
            }
        ]
    )

    def setUp(self):
        cache.clear()  # every test starts with an empty geocode cache
        geocoding._last_request_at = 0.0  # avoid throttle sleeps in tests

    @staticmethod
    def _fake_response(body):
        """A mocked urllib response object supporting the `with` block."""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body.encode("utf-8")
        return response

    def test_successful_geocoding_returns_neutral_result(self):
        with mock.patch(
            "routing.services.geocoding.urllib.request.urlopen",
            return_value=self._fake_response(self.SUCCESS_BODY),
        ) as urlopen:
            result = geocoding.geocode_address("123 Main St, Testville, TX")

        self.assertEqual(
            result,
            {
                "latitude": 45.1234567,
                "longitude": -93.7654321,
                "display_name": "Test Place, Minnesota, United States",
            },
        )
        self.assertIsInstance(result["latitude"], float)
        self.assertIsInstance(result["longitude"], float)
        # The provider's usage policy requires an identifying User-Agent.
        request = urlopen.call_args[0][0]
        self.assertEqual(
            request.get_header("User-agent"), geocoding.USER_AGENT
        )

    def test_invalid_response_raises(self):
        invalid_bodies = (
            "this is not json",  # unparseable body
            '{"error": "not a list"}',  # not a result array
            '[{"place_id": 1}]',  # missing lat/lon/display_name
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                with mock.patch(
                    "routing.services.geocoding.urllib.request.urlopen",
                    return_value=self._fake_response(body),
                ):
                    with self.assertRaises(geocoding.GeocodingInvalidResponse):
                        geocoding.geocode_address("123 Main St")

    def test_http_failure_raises(self):
        http_error = urllib.error.HTTPError(
            "https://nominatim.openstreetmap.org/search",
            503,
            "Service Unavailable",
            {},
            None,
        )
        with mock.patch(
            "routing.services.geocoding.urllib.request.urlopen",
            side_effect=http_error,
        ):
            with self.assertRaises(geocoding.GeocodingHTTPError):
                geocoding.geocode_address("123 Main St")

    def test_timeout_raises(self):
        timeout_cases = (
            TimeoutError("timed out"),
            urllib.error.URLError(TimeoutError("timed out")),
        )
        for exc in timeout_cases:
            with self.subTest(exc=repr(exc)):
                with mock.patch(
                    "routing.services.geocoding.urllib.request.urlopen",
                    side_effect=exc,
                ):
                    with self.assertRaises(geocoding.GeocodingTimeout):
                        geocoding.geocode_address("123 Main St")

    def test_cached_result_avoids_second_api_call(self):
        with mock.patch(
            "routing.services.geocoding.urllib.request.urlopen",
            return_value=self._fake_response(self.SUCCESS_BODY),
        ) as urlopen:
            first = geocoding.geocode_address("123 Main St, Testville, TX")
            second = geocoding.geocode_address("123 Main St, Testville, TX")

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(first, second)

    def test_no_match_is_cached_and_avoids_second_api_call(self):
        # Section 9 external-call discipline: a valid "no match" response
        # is a provider response too, so retrying the same unmatched
        # address must not issue a second live geocoding request.
        with mock.patch(
            "routing.services.geocoding.urllib.request.urlopen",
            return_value=self._fake_response("[]"),
        ) as urlopen:
            first = geocoding.geocode_address("Nowhereville, ZZ")
            second = geocoding.geocode_address("Nowhereville, ZZ")

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(urlopen.call_count, 1)


class RoutingServiceTests(TestCase):
    """Section 4: the external routing API is always mocked.

    Covers: successful route, timeout, HTTP error, invalid response,
    cached route, and that a cached request does not call the API.
    No test ever performs a real network call.
    """

    ROUTE_BODY = json.dumps(
        {
            "code": "Ok",
            "routes": [
                {
                    "distance": 16093.44,  # meters -> 10 miles
                    "duration": 600.0,  # seconds -> 10 minutes
                    "legs": [],
                    "weight": 600.0,
                    "weight_name": "routability",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-73.9857, 40.7484], [-74.006, 40.7128]],
                    },
                }
            ],
        }
    )

    START = (40.7484, -73.9857)  # (latitude, longitude)
    FINISH = (40.7128, -74.006)  # (latitude, longitude)

    def setUp(self):
        cache.clear()  # isolate the route cache between tests

    @staticmethod
    def _fake_response(body):
        """A mocked urllib response object supporting the `with` block."""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body.encode("utf-8")
        return response

    def _get_route(self):
        service = routing_service.RoutingService()
        return service.get_route(*self.START, *self.FINISH)

    def test_successful_route(self):
        with mock.patch(
            "routing.services.routing.urllib.request.urlopen",
            return_value=self._fake_response(self.ROUTE_BODY),
        ) as urlopen:
            result = self._get_route()

        self.assertEqual(
            set(result), {"distance_miles", "duration_minutes", "geometry"}
        )
        self.assertAlmostEqual(result["distance_miles"], 10.0, places=9)
        self.assertAlmostEqual(result["duration_minutes"], 10.0, places=9)
        self.assertEqual(
            result["geometry"],
            {
                "type": "LineString",
                "coordinates": [[-73.9857, 40.7484], [-74.006, 40.7128]],
            },
        )
        # Exactly one call, {longitude},{latitude} order, geojson geometry.
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args[0][0]
        self.assertIn(
            "driving/-73.9857,40.7484;-74.006,40.7128", request.full_url
        )
        self.assertIn("geometries=geojson", request.full_url)
        self.assertIn("overview=full", request.full_url)
        self.assertEqual(
            request.get_header("User-agent"), routing_service.USER_AGENT
        )

    def test_timeout(self):
        timeout_cases = (
            TimeoutError("timed out"),
            urllib.error.URLError(TimeoutError("timed out")),
        )
        for exc in timeout_cases:
            with self.subTest(exc=repr(exc)):
                with mock.patch(
                    "routing.services.routing.urllib.request.urlopen",
                    side_effect=exc,
                ):
                    with self.assertRaises(routing_service.RoutingTimeout):
                        self._get_route()

    def test_http_error(self):
        http_error = urllib.error.HTTPError(
            "https://router.project-osrm.org/route/v1/driving",
            500,
            "Internal Server Error",
            {},
            None,
        )
        with mock.patch(
            "routing.services.routing.urllib.request.urlopen",
            side_effect=http_error,
        ):
            with self.assertRaises(routing_service.RoutingHTTPError):
                self._get_route()

    def test_invalid_response(self):
        invalid_bodies = (
            "this is not json",  # unparseable body
            '{"code": "Ok"}',  # missing routes
            '{"code": "Ok", "routes": []}',  # empty routes
            # missing distance:
            '{"code": "Ok", "routes": [{"duration": 600.0, "geometry": '
            '{"type": "LineString", "coordinates": [[0, 0]]}}]}',
            # missing duration:
            '{"code": "Ok", "routes": [{"distance": 100.0, "geometry": '
            '{"type": "LineString", "coordinates": [[0, 0]]}}]}',
            # missing geometry:
            '{"code": "Ok", "routes": [{"distance": 100.0, "duration": 600.0}]}',
            # geometry without coordinates:
            '{"code": "Ok", "routes": [{"distance": 100.0, "duration": 600.0, '
            '"geometry": {"type": "LineString"}}]}',
            # empty geometry coordinates:
            '{"code": "Ok", "routes": [{"distance": 100.0, "duration": 600.0, '
            '"geometry": {"type": "LineString", "coordinates": []}}]}',
            # non-numeric distance:
            '{"code": "Ok", "routes": [{"distance": "far", "duration": 600.0, '
            '"geometry": {"type": "LineString", "coordinates": [[0, 0]]}}]}',
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                with mock.patch(
                    "routing.services.routing.urllib.request.urlopen",
                    return_value=self._fake_response(body),
                ):
                    with self.assertRaises(routing_service.RoutingInvalidResponse):
                        self._get_route()

    def test_cached_route(self):
        with mock.patch(
            "routing.services.routing.urllib.request.urlopen",
            return_value=self._fake_response(self.ROUTE_BODY),
        ):
            first = self._get_route()
            second = self._get_route()

        self.assertEqual(first, second)
        self.assertAlmostEqual(second["distance_miles"], 10.0, places=9)
        self.assertEqual(
            second["geometry"]["coordinates"],
            [[-73.9857, 40.7484], [-74.006, 40.7128]],
        )

    def test_cached_request_does_not_call_api(self):
        with mock.patch(
            "routing.services.routing.urllib.request.urlopen",
            return_value=self._fake_response(self.ROUTE_BODY),
        ) as urlopen:
            # Identical coordinates -> second request served from cache.
            self._get_route()
            self._get_route()
            self.assertEqual(urlopen.call_count, 1)
            # A different coordinate pair has a different cache key,
            # so exactly one more call is made for it.
            routing_service.RoutingService().get_route(
                *self.FINISH, *self.START
            )
            self.assertEqual(urlopen.call_count, 2)


class RouteStationsTests(TestCase):
    """Section 5: fuel stations along a route.

    Fully local computation — no routing API, no geocoding API, no new
    route. The route is a straight 1-degree north-south LineString at
    longitude -75.0 (latitude 40.0 -> 41.0); expectations are computed
    from the same spherical geometry the service documents.
    """

    ROUTE = {
        "type": "LineString",
        "coordinates": [[-75.0, 40.0], [-75.0, 41.0]],
    }

    def _make_station(self, opis_id, latitude, longitude, price="3.499"):
        return FuelStation.objects.create(
            opis_truckstop_id=opis_id,
            truckstop_name=f"STATION {opis_id}",
            address="US-1",
            city="Testville",
            state="PA",
            rack_id="100",
            retail_price=Decimal(price),
            latitude=latitude,
            longitude=longitude,
        )

    def test_station_directly_on_route(self):
        station = self._make_station("901", 40.5, -75.0)

        candidates = route_stations.find_stations_along_route(self.ROUTE)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(
            set(candidate),
            {
                "station_id",
                "station_name",
                "address",
                "city",
                "state",
                "latitude",
                "longitude",
                "price_per_gallon",
                "distance_from_route_miles",
                "distance_from_route_start_miles",
            },
        )
        self.assertEqual(candidate["station_id"], station.id)
        self.assertEqual(candidate["station_name"], "STATION 901")
        self.assertEqual(candidate["address"], "US-1")
        self.assertEqual(candidate["city"], "Testville")
        self.assertEqual(candidate["state"], "PA")
        self.assertEqual(candidate["price_per_gallon"], Decimal("3.499"))
        # On the line: distance from route is numerically zero.
        self.assertAlmostEqual(candidate["distance_from_route_miles"], 0.0, places=6)
        # Halfway along the 1-degree route, measured from the start.
        expected_start_miles = (6371008.8 * math.radians(0.5)) / 1609.344
        self.assertAlmostEqual(
            candidate["distance_from_route_start_miles"],
            expected_start_miles,
            places=3,
        )

    def test_station_within_10_miles(self):
        # 0.1 degrees east of the route at the route's midpoint latitude
        # (~5.25 miles) -> inside the 10-mile deviation band.
        station = self._make_station("902", 40.5, -74.9)

        candidates = route_stations.find_stations_along_route(self.ROUTE)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["station_id"], station.id)
        expected_miles = (
            6371008.8 * math.cos(math.radians(40.5)) * math.radians(0.1)
        ) / 1609.344
        self.assertAlmostEqual(
            candidate["distance_from_route_miles"], expected_miles, places=3
        )
        self.assertLess(candidate["distance_from_route_miles"], 10)

    def test_station_more_than_10_miles_away(self):
        # 0.2 degrees east ~= 10.5 miles at this latitude -> excluded.
        self._make_station("903", 40.5, -74.8)

        candidates = route_stations.find_stations_along_route(self.ROUTE)

        self.assertEqual(candidates, [])

    def test_multiple_stations_ordered_by_route_position(self):
        # Created in a deliberately scrambled order.
        north = self._make_station("912", 40.8, -74.995)
        south = self._make_station("910", 40.2, -74.99)
        middle = self._make_station("911", 40.4, -75.01)

        candidates = route_stations.find_stations_along_route(self.ROUTE)

        self.assertEqual(
            [candidate["station_id"] for candidate in candidates],
            [south.id, middle.id, north.id],
        )
        positions = [
            candidate["distance_from_route_start_miles"] for candidate in candidates
        ]
        self.assertEqual(positions, sorted(positions))

    def test_station_before_the_start(self):
        # ~3.5 miles south of the route's first vertex.
        station = self._make_station("920", 39.95, -75.0)

        candidates = route_stations.find_stations_along_route(self.ROUTE)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["station_id"], station.id)
        self.assertLess(candidate["distance_from_route_miles"], 10)
        # Position is measured from the start: before it, clamped to 0.
        self.assertEqual(candidate["distance_from_route_start_miles"], 0.0)

    def test_station_after_the_destination(self):
        # ~3.5 miles north of the route's last vertex.
        station = self._make_station("921", 41.05, -75.0)

        candidates = route_stations.find_stations_along_route(self.ROUTE)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["station_id"], station.id)
        self.assertLess(candidate["distance_from_route_miles"], 10)
        # Position clamps to the route's full length.
        route_length_miles = (6371008.8 * math.radians(1.0)) / 1609.344
        self.assertAlmostEqual(
            candidate["distance_from_route_start_miles"],
            route_length_miles,
            places=3,
        )


class FuelOptimizerTests(TestCase):
    """Section 6: fuel-stop optimization (the 13 required cases).

    Purely local and deterministic — no API calls, no database use.
    Inputs are Section 5 candidate dicts (only the three keys the
    optimizer consumes are built here). Fixed vehicle: 500-mile range,
    10 MPG, 50-gallon tank.
    """

    @staticmethod
    def candidate(station_id, position, price):
        """Build the Section 5 candidate keys the optimizer reads."""
        return {
            "station_id": station_id,
            "distance_from_route_start_miles": position,
            "price_per_gallon": price,
        }

    # 1. Route shorter than the 500-mile range: the full tank covers it.
    def test_1_route_under_max_range_has_no_stops(self):
        result = fuel_optimizer.optimize_fuel_stops(
            450,
            [self.candidate(1, 100, "3.50"), self.candidate(2, 300, "3.50")],
        )

        self.assertEqual(result["fuel_stops"], [])
        self.assertEqual(result["total_fuel_cost"], Decimal("0.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("45"))

    # 2. Exactly 500 miles: the tank just covers it — still no stop.
    def test_2_route_exactly_max_range_has_no_stops(self):
        result = fuel_optimizer.optimize_fuel_stops(
            500, [self.candidate(1, 200, "3.50")]
        )

        self.assertEqual(result["fuel_stops"], [])
        self.assertEqual(result["total_fuel_cost"], Decimal("0.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("50"))

    # 3. Beyond 500 miles: at least one fuel purchase is required.
    def test_3_route_beyond_max_range_requires_a_stop(self):
        result = fuel_optimizer.optimize_fuel_stops(
            600,
            [self.candidate(1, 100, "3.50"), self.candidate(2, 300, "3.50")],
        )

        self.assertEqual(len(result["fuel_stops"]), 1)
        stop = result["fuel_stops"][0]
        self.assertEqual(stop["station_id"], 1)
        self.assertEqual(stop["gallons_purchased"], Decimal("10"))
        self.assertEqual(stop["cost"], Decimal("35.00"))
        self.assertEqual(result["total_fuel_cost"], Decimal("35.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("60"))

    # 4. A long route needs several stops, always in route order.
    def test_4_multiple_stops_in_route_order(self):
        result = fuel_optimizer.optimize_fuel_stops(
            2000,
            [
                self.candidate(1, 100, "3.50"),
                self.candidate(2, 600, "3.50"),
                self.candidate(3, 1100, "3.50"),
                self.candidate(4, 1600, "3.50"),
            ],
        )

        stops = result["fuel_stops"]
        self.assertEqual([s["station_id"] for s in stops], [1, 2, 3, 4])
        self.assertEqual(
            [s["distance_from_route_start_miles"] for s in stops],
            [Decimal(100), Decimal(600), Decimal(1100), Decimal(1600)],
        )
        self.assertEqual(
            [s["gallons_purchased"] for s in stops],
            [Decimal(10), Decimal(50), Decimal(50), Decimal(40)],
        )
        self.assertEqual(result["total_fuel_cost"], Decimal("525.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("200"))

    # 5. A cheap station within range is targeted; the expensive one
    #    needs no purchase (the tank already covers the leg to it).
    def test_5_reachable_cheap_station_is_targeted(self):
        result = fuel_optimizer.optimize_fuel_stops(
            1000,
            [
                self.candidate(1, 200, "3.80"),   # expensive
                self.candidate(2, 450, "3.00"),   # cheap, reachable
                self.candidate(3, 950, "3.20"),
            ],
        )

        stops = result["fuel_stops"]
        self.assertEqual([s["station_id"] for s in stops], [2, 3])
        self.assertEqual(stops[0]["price_per_gallon"], Decimal("3.00"))
        self.assertEqual(stops[0]["gallons_purchased"], Decimal("45"))
        self.assertEqual(stops[0]["cost"], Decimal("135.00"))
        self.assertEqual(result["total_fuel_cost"], Decimal("151.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("100"))

    # 6. The globally cheapest station is never chosen when it cannot
    #    be reached (out of the 500-mile window / beyond the destination).
    def test_6_cheap_station_out_of_range_is_not_selected(self):
        # (a) At station 1 (mile 200) the range window ends at mile 700,
        #     so the 2.99 station at mile 750 cannot be a candidate — the
        #     plan fuels at the expensive 3.90 station instead.
        result = fuel_optimizer.optimize_fuel_stops(
            1300,
            [
                self.candidate(1, 200, "3.90"),
                self.candidate(2, 600, "3.95"),
                self.candidate(3, 750, "2.99"),   # cheapest, out of range
                self.candidate(4, 1150, "3.80"),
            ],
        )
        stops = result["fuel_stops"]
        self.assertEqual(stops[0]["station_id"], 1)
        self.assertEqual(stops[0]["price_per_gallon"], Decimal("3.90"))
        self.assertGreater(stops[0]["price_per_gallon"], Decimal("2.99"))
        self.assertEqual(result["total_fuel_cost"], Decimal("266.25"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("130"))

        # (b) The cheapest station lies beyond the destination: it is
        #     never used even though 2.99 is the lowest price on offer.
        result = fuel_optimizer.optimize_fuel_stops(
            900,
            [
                self.candidate(1, 300, "3.90"),
                self.candidate(2, 600, "3.85"),
                self.candidate(3, 950, "2.99"),   # cheapest, off-route
            ],
        )
        stops = result["fuel_stops"]
        self.assertEqual([s["station_id"] for s in stops], [1, 2])
        self.assertNotIn(3, [s["station_id"] for s in stops])
        self.assertEqual(result["total_fuel_cost"], Decimal("154.50"))

    # 7. Expensive now, cheaper ahead: buy ONLY enough to reach the
    #    cheaper station (5 gal), not a full-tank fill (10 gal).
    def test_7_expensive_to_cheap_buys_only_enough(self):
        result = fuel_optimizer.optimize_fuel_stops(
            1200,
            [
                self.candidate(1, 100, "4.00"),
                self.candidate(2, 550, "3.00"),   # cheaper ahead
                self.candidate(3, 1050, "3.20"),
            ],
        )

        stops = result["fuel_stops"]
        self.assertEqual([s["station_id"] for s in stops], [1, 2, 3])
        first = stops[0]
        self.assertEqual(first["gallons_purchased"], Decimal("5"))
        # A tank fill here would be 10 gal (40 -> 50): proof of "only
        # enough to reach the cheaper station".
        self.assertLess(first["gallons_purchased"], Decimal("10"))
        self.assertEqual(first["cost"], Decimal("20.00"))
        self.assertEqual(stops[1]["cost"], Decimal("150.00"))
        self.assertEqual(result["total_fuel_cost"], Decimal("218.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("120"))

    # 8. No cheaper station ahead: fill the tank at the current station.
    def test_8_no_cheaper_station_ahead_fills_the_tank(self):
        result = fuel_optimizer.optimize_fuel_stops(
            1200,
            [
                self.candidate(1, 100, "3.50"),
                self.candidate(2, 600, "3.60"),   # more expensive
                self.candidate(3, 1100, "3.55"),  # more expensive
            ],
        )

        stops = result["fuel_stops"]
        # At station 1 nothing cheaper is in range (station 2 is 3.60),
        # so the purchase tops the tank up 40 -> 50 gallons.
        first = stops[0]
        self.assertEqual(first["station_id"], 1)
        self.assertEqual(first["gallons_purchased"], Decimal("10"))
        self.assertEqual(first["cost"], Decimal("35.00"))
        self.assertEqual(result["total_fuel_cost"], Decimal("250.50"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("120"))

    # 9. Once the destination is within range, it is driven to directly:
    #    the station at mile 450 (before the destination) is never used.
    def test_9_destination_reachable_without_further_stop(self):
        result = fuel_optimizer.optimize_fuel_stops(
            600,
            [
                self.candidate(1, 100, "3.50"),
                self.candidate(2, 450, "3.70"),   # before destination
            ],
        )

        stops = result["fuel_stops"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["station_id"], 1)
        self.assertNotIn(2, [s["station_id"] for s in stops])
        self.assertEqual(result["total_fuel_cost"], Decimal("35.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("60"))

    # 10. No feasible station: controlled ROUTE_NOT_FEASIBLE error,
    #     never a fabricated plan.
    def test_10_infeasible_route_raises_route_not_feasible(self):
        cases = (
            (700, []),  # no stations at all
            # Station exists but lies beyond the 500-mile window.
            (1400, [self.candidate(1, 650, "3.50")]),
        )
        for route_distance, stations in cases:
            with self.subTest(route=route_distance, stations=stations):
                with self.assertRaises(
                    fuel_optimizer.RouteNotFeasibleError
                ) as ctx:
                    fuel_optimizer.optimize_fuel_stops(
                        route_distance, stations
                    )
                self.assertEqual(
                    ctx.exception.code, "ROUTE_NOT_FEASIBLE"
                )
                self.assertEqual(str(ctx.exception), "ROUTE_NOT_FEASIBLE")

    # 11. Boundary: a station exactly at mile 500 is reachable from the
    #     start (inclusive); 0.1 mile further is out of range.
    def test_11_station_exactly_at_500_mile_boundary(self):
        # Inclusive boundary: station at exactly 500 miles is usable.
        result = fuel_optimizer.optimize_fuel_stops(
            700, [self.candidate(1, 500, "3.50")]
        )
        stops = result["fuel_stops"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["station_id"], 1)
        self.assertEqual(
            stops[0]["distance_from_route_start_miles"], Decimal(500)
        )
        self.assertEqual(stops[0]["gallons_purchased"], Decimal("20"))
        self.assertEqual(stops[0]["cost"], Decimal("70.00"))

        # Exclusive side: just past the window nothing is reachable.
        with self.assertRaises(fuel_optimizer.RouteNotFeasibleError):
            fuel_optimizer.optimize_fuel_stops(
                700, [self.candidate(1, 500.1, "3.50")]
            )

    # 12. Tank capacity boundary: a needed fill larger than 50 gallons
    #     is clamped to the tank; no purchase ever exceeds 50 gallons.
    def test_12_tank_capacity_caps_the_purchase(self):
        result = fuel_optimizer.optimize_fuel_stops(
            1600,
            [
                self.candidate(1, 100, "3.50"),
                self.candidate(2, 600, "3.50"),
                self.candidate(3, 1100, "3.50"),
            ],
        )

        stops = result["fuel_stops"]
        self.assertEqual(
            [s["gallons_purchased"] for s in stops],
            [Decimal(10), Decimal(50), Decimal(50)],
        )
        # Station 2 needs 100 gal for the remaining route, but the tank
        # caps the purchase at 50 gallons.
        self.assertEqual(stops[1]["gallons_purchased"], Decimal(50))
        self.assertTrue(
            all(s["gallons_purchased"] <= 50 for s in stops)
        )
        self.assertEqual(result["total_fuel_cost"], Decimal("385.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("160"))

    # 13. Money is exact Decimal arithmetic: float inputs are normalized
    #     (str -> Decimal) and costs are ROUND_HALF_UP to 2 decimals —
    #     float math would give 19.994999... -> 19.99 instead of 20.00.
    def test_13_decimal_cost_calculation(self):
        result = fuel_optimizer.optimize_fuel_stops(
            1200.0,  # float route distance
            [
                self.candidate(1, 100, 3.999),      # float price
                self.candidate(2, 550, "3.00"),
                self.candidate(3, 1050, "3.20"),
            ],
        )

        stops = result["fuel_stops"]
        first = stops[0]
        self.assertEqual(first["price_per_gallon"], Decimal("3.999"))
        self.assertEqual(first["gallons_purchased"], Decimal(5))
        # 5 x 3.999 = 19.995 -> 20.00 (ROUND_HALF_UP), not 19.99.
        self.assertEqual(first["cost"], Decimal("20.00"))
        for stop in stops:
            self.assertIsInstance(stop["cost"], Decimal)
            self.assertEqual(stop["cost"].as_tuple().exponent, -2)
        self.assertIsInstance(result["total_fuel_cost"], Decimal)
        self.assertEqual(result["total_fuel_cost"].as_tuple().exponent, -2)
        self.assertEqual(
            result["total_fuel_cost"],
            sum((s["cost"] for s in stops), Decimal("0.00")),
        )
        self.assertEqual(result["total_fuel_cost"], Decimal("218.00"))
        self.assertEqual(result["total_gallons_consumed"], Decimal("120"))


class RouteEndpointTests(TestCase):
    """Section 7: POST /api/v1/routes/ — the 9 required cases.

    External providers are always mocked: a single urlopen dispatcher
    answers Nominatim (geocoding) and OSRM (routing) requests. No test
    ever performs a real network call.
    """

    GEOCODE = {
        "New York, NY": {
            "place_id": 100,
            "lat": "40.7128",
            "lon": "-74.006",
            "display_name": "New York, New York County, New York, United States",
        },
        "Chicago, IL": {
            "place_id": 200,
            "lat": "41.8781",
            "lon": "-87.6298",
            "display_name": "Chicago, Cook County, Illinois, United States",
        },
    }

    def setUp(self):
        cache.clear()  # isolate geocode + route caches between tests
        geocoding._last_request_at = 0.0
        # No throttle sleeps in tests (the 1 req/s policy stays intact
        # in production; the mocked provider never sees a real request).
        self._throttle = mock.patch.object(geocoding, "_throttle")
        self._throttle.start()
        self.addCleanup(self._throttle.stop)
        self.external_urls = []

    # --- Helpers -----------------------------------------------------------
    @staticmethod
    def _route_body(distance_miles, duration_minutes, coordinates):
        """An OSRM 'Ok' response with the given route metrics."""
        return json.dumps(
            {
                "code": "Ok",
                "routes": [
                    {
                        "distance": distance_miles * 1609.344,
                        "duration": duration_minutes * 60.0,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coordinates,
                        },
                    }
                ],
            }
        )

    @staticmethod
    def _fake_response(body):
        """A mocked urllib response object supporting the `with` block."""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body.encode("utf-8")
        return response

    def fake_urlopen(self, route_body=None, route_error=None):
        """Patch urlopen for BOTH provider modules with one dispatcher.

        Nominatim requests are answered from GEOCODE by query; OSRM
        requests answer route_body or raise route_error. Every external
        URL is recorded in self.external_urls for flow assertions.
        """

        def side_effect(request, timeout=None):
            url = request.full_url
            self.external_urls.append(url)
            if "nominatim.openstreetmap.org" in url:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                address = query["q"][0]
                if address not in self.GEOCODE:
                    raise AssertionError(f"unexpected geocode query: {address}")
                return self._fake_response(json.dumps([self.GEOCODE[address]]))
            if "router.project-osrm.org" in url:
                if route_error is not None:
                    raise route_error
                if route_body is None:
                    raise AssertionError("unexpected routing call")
                return self._fake_response(route_body)
            raise AssertionError(f"unexpected external URL: {url}")

        return mock.patch("urllib.request.urlopen", side_effect=side_effect)

    def post_route(self, payload):
        return self.client.post(
            "/api/v1/routes/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def assert_error(self, response, status, code, contains=None):
        """Assert the exact error envelope: {"error": {code, message}}."""
        self.assertEqual(response.status_code, status)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message"})
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)
        if contains is not None:
            self.assertIn(contains, body["error"]["message"])

    # --- 1. Valid request ---------------------------------------------------
    def test_1_valid_request_returns_exact_response_structure(self):
        route_body = self._route_body(
            400, 400, [[-74.006, 40.7128], [-87.6298, 41.8781]]
        )
        with self.fake_urlopen(route_body=route_body):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body),
            {"start", "finish", "route", "vehicle", "fuel_stops", "fuel_summary"},
        )

        start = body["start"]
        self.assertEqual(set(start), {"name", "latitude", "longitude"})
        self.assertEqual(
            start["name"], "New York, New York County, New York, United States"
        )
        self.assertEqual(start["latitude"], 40.7128)
        self.assertEqual(start["longitude"], -74.006)

        finish = body["finish"]
        self.assertEqual(set(finish), {"name", "latitude", "longitude"})
        self.assertEqual(
            finish["name"], "Chicago, Cook County, Illinois, United States"
        )
        self.assertEqual(finish["latitude"], 41.8781)
        self.assertEqual(finish["longitude"], -87.6298)

        route = body["route"]
        self.assertEqual(
            set(route), {"distance_miles", "duration_minutes", "geometry"}
        )
        self.assertAlmostEqual(route["distance_miles"], 400.0, places=6)
        self.assertAlmostEqual(route["duration_minutes"], 400.0, places=6)
        self.assertEqual(
            route["geometry"],
            {
                "type": "LineString",
                "coordinates": [[-74.006, 40.7128], [-87.6298, 41.8781]],
            },
        )

        self.assertEqual(
            body["vehicle"],
            {"mpg": 10, "max_range_miles": 500, "tank_capacity_gallons": 50},
        )
        self.assertEqual(body["fuel_stops"], [])

        summary = body["fuel_summary"]
        self.assertEqual(
            set(summary),
            {"total_distance_miles", "total_gallons", "total_fuel_cost"},
        )
        self.assertAlmostEqual(summary["total_distance_miles"], 400.0, places=6)
        self.assertAlmostEqual(summary["total_gallons"], 40.0, places=6)
        self.assertEqual(summary["total_fuel_cost"], 0.0)

    # --- 2. Missing start ---------------------------------------------------
    def test_2_missing_start_returns_400(self):
        with self.fake_urlopen() as urlopen:
            response = self.post_route({"finish": "Chicago, IL"})

        self.assert_error(response, 400, "INVALID_REQUEST", contains="start")
        self.assertEqual(urlopen.call_count, 0)  # no external calls made

    # --- 3. Missing finish --------------------------------------------------
    def test_3_missing_finish_returns_400(self):
        with self.fake_urlopen() as urlopen:
            response = self.post_route({"start": "New York, NY"})

        self.assert_error(response, 400, "INVALID_REQUEST", contains="finish")
        self.assertEqual(urlopen.call_count, 0)

    # --- 4. Empty start -----------------------------------------------------
    def test_4_empty_start_returns_400(self):
        with self.fake_urlopen() as urlopen:
            response = self.post_route(
                {"start": "", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 400, "INVALID_REQUEST", contains="empty")
        self.assertEqual(urlopen.call_count, 0)

    # --- 5. Empty finish ----------------------------------------------------
    def test_5_empty_finish_returns_400(self):
        with self.fake_urlopen() as urlopen:
            response = self.post_route(
                {"start": "New York, NY", "finish": ""}
            )

        self.assert_error(response, 400, "INVALID_REQUEST", contains="empty")
        self.assertEqual(urlopen.call_count, 0)

    # --- 6. Successful route ------------------------------------------------
    def test_6_successful_route_runs_the_full_flow(self):
        # On-route station: halfway along the north-south 13-degree line
        # (its projected position is ~449 mi of the mocked 900-mi route,
        # inside both the 500-mile start range and the finish window).
        station = FuelStation.objects.create(
            opis_truckstop_id="701",
            truckstop_name="ROUTE STATION",
            address="US-1",
            city="Midway",
            state="PA",
            rack_id="500",
            retail_price=Decimal("3.499"),
            latitude=41.5,
            longitude=-80.0,
        )
        route_body = self._route_body(
            900, 600, [[-80.0, 35.0], [-80.0, 48.0]]
        )
        with self.fake_urlopen(route_body=route_body):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assertEqual(response.status_code, 200)

        # Exact flow: geocode start -> geocode finish -> driving route.
        self.assertEqual(len(self.external_urls), 3)
        for geocode_url in self.external_urls[:2]:
            self.assertIn("nominatim.openstreetmap.org", geocode_url)
            # USA restriction enforced at the provider.
            self.assertIn("countrycodes=us", geocode_url)
        route_url = self.external_urls[2]
        self.assertIn("router.project-osrm.org", route_url)
        # The geocoded coordinates feed the routing request (lon,lat).
        self.assertIn("driving/-74.006,40.7128;-87.6298,41.8781", route_url)

        body = response.json()
        self.assertAlmostEqual(body["route"]["distance_miles"], 900.0, places=6)
        self.assertAlmostEqual(body["route"]["duration_minutes"], 600.0, places=6)

        stops = body["fuel_stops"]
        self.assertEqual(len(stops), 1)
        stop = stops[0]
        self.assertEqual(
            set(stop),
            {
                "station_id",
                "station_name",
                "address",
                "city",
                "state",
                "latitude",
                "longitude",
                "price_per_gallon",
                "distance_from_start_miles",
                "gallons_purchased",
                "fuel_cost",
            },
        )
        self.assertEqual(stop["station_id"], str(station.pk))  # string id
        self.assertEqual(stop["station_name"], "ROUTE STATION")
        self.assertEqual(stop["address"], "US-1")
        self.assertEqual(stop["city"], "Midway")
        self.assertEqual(stop["state"], "PA")
        self.assertEqual(stop["latitude"], 41.5)
        self.assertEqual(stop["longitude"], -80.0)
        self.assertEqual(stop["price_per_gallon"], 3.499)
        # Spherical position of the midpoint (same geometry as Section 5).
        expected_position = (6371008.8 * math.radians(6.5)) / 1609.344
        self.assertAlmostEqual(
            stop["distance_from_start_miles"], expected_position, places=3
        )
        # 40 gallons = exactly what remains for the rest of the route
        # (the tank cap would allow 44.9); 40 x 3.499 = 139.96.
        self.assertAlmostEqual(stop["gallons_purchased"], 40.0, places=6)
        self.assertAlmostEqual(stop["fuel_cost"], 139.96, places=6)

        summary = body["fuel_summary"]
        self.assertAlmostEqual(summary["total_distance_miles"], 900.0, places=6)
        self.assertAlmostEqual(summary["total_gallons"], 90.0, places=6)
        self.assertAlmostEqual(summary["total_fuel_cost"], 139.96, places=6)

    # --- 7. No route --------------------------------------------------------
    def test_7_no_route_returns_422(self):
        no_route_error = urllib.error.HTTPError(
            "https://router.project-osrm.org/route/v1/driving",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"code": "NoRoute", "message": "No route found"}'),
        )
        with self.fake_urlopen(route_error=no_route_error):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 422, "NO_ROUTE")

    # --- 8. No feasible fuel plan -------------------------------------------
    def test_8_no_feasible_fuel_plan_returns_422(self):
        # 700-mile route (beyond the 500-mile range) with no stations.
        route_body = self._route_body(
            700, 700, [[-80.0, 35.0], [-80.0, 40.0]]
        )
        with self.fake_urlopen(route_body=route_body):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 422, "ROUTE_NOT_FEASIBLE")

    # --- 9. External service failure ----------------------------------------
    def test_9_external_service_failure_returns_502(self):
        cases = {
            # Geocoding provider times out.
            "geocoding": mock.patch(
                "urllib.request.urlopen", side_effect=TimeoutError("timed out")
            ),
            # Routing provider returns HTTP 500 (no provider code).
            "routing": self.fake_urlopen(
                route_error=urllib.error.HTTPError(
                    "https://router.project-osrm.org/route/v1/driving",
                    500,
                    "Internal Server Error",
                    {},
                    None,
                )
            ),
        }
        for provider, patcher in cases.items():
            with self.subTest(provider=provider):
                cache.clear()  # isolate caches between cases
                with patcher:
                    response = self.post_route(
                        {"start": "New York, NY", "finish": "Chicago, IL"}
                    )
                self.assert_error(response, 502, "EXTERNAL_SERVICE_ERROR")


class MapPageTests(TestCase):
    """Section 8: the Leaflet map page (GET /map/).

    The page must expose the required controls, load Leaflet, call ONLY
    POST /api/v1/routes/, and never reference a routing/geocoding
    provider directly.
    """

    def test_map_page_renders_required_controls(self):
        response = self.client.get("/map/")

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        # Required inputs and button.
        self.assertIn('id="start"', content)
        self.assertIn('id="finish"', content)
        self.assertIn("Calculate Route", content)
        # Leaflet (CDN) is loaded.
        self.assertIn("leaflet@1.9.4", content)
        # The only API call the page makes is the route endpoint.
        self.assertIn("/api/v1/routes/", content)
        # Providers are never referenced from the frontend.
        self.assertNotIn("router.project-osrm.org", content)
        self.assertNotIn("nominatim", content)
        # Required summary labels are present.
        for label in (
            "Total distance",
            "Total gallons",
            "Total fuel cost",
            "Number of fuel stops",
            "Vehicle MPG",
            "Maximum range",
        ):
            with self.subTest(label=label):
                self.assertIn(label, content)


class PerformanceTests(TestCase):
    """Section 9: performance / external-API requirements.

    Required tests:
    1. A cached route does not call the routing API (and cached geocoding
       results are not re-requested either).
    2. Fuel station data does not read the CSV during an API request.

    External providers are always mocked (same harness as
    RouteEndpointTests); no test performs a real network call.
    """

    GEOCODE = {
        "New York, NY": {
            "place_id": 100,
            "lat": "40.7128",
            "lon": "-74.006",
            "display_name": "New York, New York County, New York, United States",
        },
        "Chicago, IL": {
            "place_id": 200,
            "lat": "41.8781",
            "lon": "-87.6298",
            "display_name": "Chicago, Cook County, Illinois, United States",
        },
    }

    def setUp(self):
        cache.clear()  # isolate geocode + route caches between tests
        geocoding._last_request_at = 0.0
        self._throttle = mock.patch.object(geocoding, "_throttle")
        self._throttle.start()
        self.addCleanup(self._throttle.stop)
        self.external_urls = []

    @staticmethod
    def _route_body(distance_miles, duration_minutes, coordinates):
        return json.dumps(
            {
                "code": "Ok",
                "routes": [
                    {
                        "distance": distance_miles * 1609.344,
                        "duration": duration_minutes * 60.0,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coordinates,
                        },
                    }
                ],
            }
        )

    @staticmethod
    def _fake_response(body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body.encode("utf-8")
        return response

    def fake_urlopen(self, route_body):
        """Patch urlopen for both providers; record every external URL."""

        def side_effect(request, timeout=None):
            url = request.full_url
            self.external_urls.append(url)
            if "nominatim.openstreetmap.org" in url:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                address = query["q"][0]
                return self._fake_response(json.dumps([self.GEOCODE[address]]))
            if "router.project-osrm.org" in url:
                return self._fake_response(route_body)
            raise AssertionError(f"unexpected external URL: {url}")

        return mock.patch("urllib.request.urlopen", side_effect=side_effect)

    def post_route(self, payload):
        return self.client.post(
            "/api/v1/routes/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    # --- 1. Cached route does not call the routing API ----------------------
    def test_1_cached_route_does_not_call_routing_api(self):
        route_body = self._route_body(
            400, 400, [[-74.006, 40.7128], [-87.6298, 41.8781]]
        )
        with self.fake_urlopen(route_body):
            first = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )
            with self.assertLogs("routing.services.routing", level="INFO") as logs:
                second = self.post_route(
                    {"start": "New York, NY", "finish": "Chicago, IL"}
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        routing_calls = [
            url for url in self.external_urls if "router.project-osrm.org" in url
        ]
        geocoding_calls = [
            url for url in self.external_urls if "nominatim.openstreetmap.org" in url
        ]
        # ONE routing API call total: the identical second request is
        # served entirely from the route cache — never per station or
        # per segment, and never recalculated.
        self.assertEqual(len(routing_calls), 1)
        # Start/finish geocoding results are cached as well: exactly
        # one geocode per location across both requests.
        self.assertEqual(len(geocoding_calls), 2)
        # The second request logged the route cache hit (Section 9 logs).
        self.assertTrue(
            any("route cache hit" in message for message in logs.output)
        )

    # --- 2. No CSV read during the API request ------------------------------
    def test_2_fuel_station_data_does_not_read_csv_during_api_request(self):
        # Station data lives in the database (Section 2 import).
        station = FuelStation.objects.create(
            opis_truckstop_id="901",
            truckstop_name="DB STATION",
            address="US-1",
            city="Midway",
            state="PA",
            rack_id="500",
            retail_price=Decimal("3.499"),
            latitude=41.5,
            longitude=-80.0,
        )
        route_body = self._route_body(
            900, 600, [[-80.0, 35.0], [-80.0, 48.0]]
        )

        opened_paths = []
        real_open = builtins.open

        def tracking_open(file, *args, **kwargs):
            opened_paths.append(str(file))
            return real_open(file, *args, **kwargs)

        # Any attempt to open the dataset during the request fails loudly.
        with mock.patch("builtins.open", tracking_open):
            with self.fake_urlopen(route_body):
                response = self.post_route(
                    {"start": "New York, NY", "finish": "Chicago, IL"}
                )

        self.assertEqual(response.status_code, 200)
        csv_reads = [path for path in opened_paths if path.endswith(".csv")]
        self.assertEqual(csv_reads, [])
        # The fuel stop proves the station came from the database.
        stops = response.json()["fuel_stops"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["station_id"], str(station.pk))


class ErrorHandlingTests(TestCase):
    """Section 10: validation and error handling.

    Case-to-test mapping (cases 1-4 and 12 are covered by
    RouteEndpointTests; case 10 by its external-failure test):

    5  invalid JSON              -> 400
    6  non-USA location          -> 400 (geocoder restricted to the US)
    7  start cannot be geocoded  -> 400
    8  finish cannot be geocoded -> 400
    9  routing timeout           -> 502
    11 routing invalid response  -> 502
    13 no stations near route    -> 422
    14 no feasible fuel plan     -> 422
    15 invalid fuel price        -> 500 (clean envelope; traceback to log)
    16 missing coordinates       -> 200 (station skipped, request succeeds)

    Every error response uses exactly {"error": {"code", "message"}} and
    never contains stack traces, file paths or credentials. External
    providers are always mocked; no test performs a real network call.
    """

    GEOCODE = {
        "New York, NY": {
            "place_id": 100,
            "lat": "40.7128",
            "lon": "-74.006",
            "display_name": "New York, New York County, New York, United States",
        },
        "Chicago, IL": {
            "place_id": 200,
            "lat": "41.8781",
            "lon": "-87.6298",
            "display_name": "Chicago, Cook County, Illinois, United States",
        },
    }

    def setUp(self):
        cache.clear()  # isolate geocode + route caches between tests
        geocoding._last_request_at = 0.0
        self._throttle = mock.patch.object(geocoding, "_throttle")
        self._throttle.start()
        self.addCleanup(self._throttle.stop)
        self.external_urls = []

    # --- Harness (same as RouteEndpointTests/PerformanceTests) --------------
    @staticmethod
    def _route_body(distance_miles, duration_minutes, coordinates):
        return json.dumps(
            {
                "code": "Ok",
                "routes": [
                    {
                        "distance": distance_miles * 1609.344,
                        "duration": duration_minutes * 60.0,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coordinates,
                        },
                    }
                ],
            }
        )

    @staticmethod
    def _fake_response(body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body.encode("utf-8")
        return response

    def fake_urlopen(self, route_body=None, route_error=None, no_match=()):
        """Patch urlopen for both providers.

        Addresses in ``no_match`` get an empty geocoder result (what
        countrycodes=us returns for a non-USA or unknown location).
        """
        no_match = set(no_match)

        def side_effect(request, timeout=None):
            url = request.full_url
            self.external_urls.append(url)
            if "nominatim.openstreetmap.org" in url:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                address = query["q"][0]
                if address in no_match:
                    return self._fake_response("[]")  # provider: no match
                if address not in self.GEOCODE:
                    raise AssertionError(f"unexpected geocode query: {address}")
                return self._fake_response(json.dumps([self.GEOCODE[address]]))
            if "router.project-osrm.org" in url:
                if route_error is not None:
                    raise route_error
                if route_body is None:
                    raise AssertionError("unexpected routing call")
                return self._fake_response(route_body)
            raise AssertionError(f"unexpected external URL: {url}")

        return mock.patch("urllib.request.urlopen", side_effect=side_effect)

    def post_route(self, payload):
        return self.client.post(
            "/api/v1/routes/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def assert_error(self, response, status, code, contains=None):
        """Exact envelope, and never a stack trace or internal path."""
        self.assertEqual(response.status_code, status)
        text = response.content.decode("utf-8")
        body = json.loads(text)
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message"})
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("File \"", text)
        if contains is not None:
            self.assertIn(contains, body["error"]["message"])

    @staticmethod
    def _line_route(distance_miles, duration_minutes=600):
        """The standard north-south 13-degree test line (~900 mi)."""
        return ErrorHandlingTests._route_body(
            distance_miles, duration_minutes, [[-80.0, 35.0], [-80.0, 48.0]]
        )

    # --- Case 5: invalid JSON ----------------------------------------------
    def test_case_5_invalid_json_returns_400(self):
        response = self.client.post(
            "/api/v1/routes/",
            data='{"start": "New York, NY",',
            content_type="application/json",
        )

        self.assert_error(response, 400, "INVALID_REQUEST", contains="JSON")

    # --- Case 6: non-USA location ------------------------------------------
    def test_case_6_non_usa_location_returns_400(self):
        # countrycodes=us: the provider reports no match for London.
        with self.fake_urlopen(no_match={"London, UK"}):
            response = self.post_route(
                {"start": "London, UK", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 400, "INVALID_REQUEST", contains="USA")
        routing_calls = [
            url for url in self.external_urls if "router.project-osrm.org" in url
        ]
        self.assertEqual(routing_calls, [])  # fails before any routing call

    # --- Case 7: start cannot be geocoded ----------------------------------
    def test_case_7_start_cannot_be_geocoded_returns_400(self):
        with self.fake_urlopen(no_match={"Nowhereville"}):
            response = self.post_route(
                {"start": "Nowhereville", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 400, "INVALID_REQUEST", contains="start")

    # --- Case 8: finish cannot be geocoded ---------------------------------
    def test_case_8_finish_cannot_be_geocoded_returns_400(self):
        with self.fake_urlopen(no_match={"Nowheresville"}):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Nowheresville"}
            )

        self.assert_error(response, 400, "INVALID_REQUEST", contains="finish")

    # --- Case 9: routing API timeout ---------------------------------------
    def test_case_9_routing_timeout_returns_502(self):
        with self.fake_urlopen(route_error=TimeoutError("timed out")):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 502, "EXTERNAL_SERVICE_ERROR")

    # --- Case 11: routing API invalid response ------------------------------
    def test_case_11_routing_invalid_response_returns_502(self):
        with self.fake_urlopen(route_body="this is not json"):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 502, "EXTERNAL_SERVICE_ERROR")

    # --- Case 13: no fuel stations near the route ---------------------------
    def test_case_13_no_fuel_stations_near_route_returns_422(self):
        # Station exists with coordinates, but ~600 miles east of the
        # route: outside the 10-mile band, so there are no candidates
        # and the 900-mile route cannot be fueled.
        FuelStation.objects.create(
            opis_truckstop_id="911",
            truckstop_name="FAR FROM ROUTE",
            address="US-1",
            city="Coastville",
            state="NC",
            rack_id="500",
            retail_price=Decimal("3.499"),
            latitude=41.5,
            longitude=-70.0,
        )
        with self.fake_urlopen(route_body=self._line_route(900)):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 422, "ROUTE_NOT_FEASIBLE")

    # --- Case 14: no feasible fuel stop plan --------------------------------
    def test_case_14_no_feasible_fuel_stop_plan_returns_422(self):
        # Station is right on the route (~449 mi) but the mocked route is
        # 1400 miles: the 500-mile range cannot bridge the remaining gap.
        FuelStation.objects.create(
            opis_truckstop_id="912",
            truckstop_name="LONELY STATION",
            address="US-1",
            city="Midway",
            state="PA",
            rack_id="500",
            retail_price=Decimal("3.499"),
            latitude=41.5,
            longitude=-80.0,
        )
        with self.fake_urlopen(route_body=self._line_route(1400)):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        self.assert_error(response, 422, "ROUTE_NOT_FEASIBLE")

    # --- Case 15: invalid fuel price ----------------------------------------
    def test_case_15_invalid_fuel_price_returns_500(self):
        FuelStation.objects.create(
            opis_truckstop_id="913",
            truckstop_name="CORRUPT PRICE",
            address="US-1",
            city="Midway",
            state="PA",
            rack_id="500",
            retail_price=Decimal("3.499"),
            latitude=41.5,
            longitude=-80.0,
        )
        # Corrupt the stored price with raw SQL, bypassing model
        # validation (as a bad row from an external loader would look).
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE routing_fuelstation SET retail_price = %s "
                "WHERE opis_truckstop_id = %s",
                ["corrupt-price", "913"],
            )

        with self.assertLogs("routing.views", level="ERROR") as logs:
            with self.fake_urlopen(route_body=self._line_route(900)):
                response = self.post_route(
                    {"start": "New York, NY", "finish": "Chicago, IL"}
                )

        # Client gets the clean envelope; no stack trace or file paths.
        self.assert_error(response, 500, "INTERNAL_ERROR")
        body = response.json()
        self.assertEqual(
            body["error"]["message"], "An unexpected internal error occurred"
        )
        # The traceback IS recorded in the server log for operators.
        self.assertTrue(
            any("Traceback" in message for message in logs.output)
        )

    # --- Case 16: missing station coordinates -------------------------------
    def test_case_16_missing_station_coordinates_is_skipped(self):
        usable = FuelStation.objects.create(
            opis_truckstop_id="914",
            truckstop_name="USABLE STATION",
            address="US-1",
            city="Midway",
            state="PA",
            rack_id="500",
            retail_price=Decimal("3.499"),
            latitude=41.5,
            longitude=-80.0,
        )
        no_coordinates = FuelStation.objects.create(
            opis_truckstop_id="915",
            truckstop_name="NO COORDINATES",
            address="US-2",
            city="Lostville",
            state="PA",
            rack_id="501",
            retail_price=Decimal("3.199"),
            latitude=None,
            longitude=None,
        )
        with self.fake_urlopen(route_body=self._line_route(900)):
            response = self.post_route(
                {"start": "New York, NY", "finish": "Chicago, IL"}
            )

        # Handled gracefully: normal success response, station skipped.
        self.assertEqual(response.status_code, 200)
        stops = response.json()["fuel_stops"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["station_id"], str(usable.pk))
        self.assertNotIn(
            str(no_coordinates.pk),
            [stop["station_id"] for stop in stops],
        )
