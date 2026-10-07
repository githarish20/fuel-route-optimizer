"""High-level route planning service (Section 7).

`RoutePlanningService` coordinates the four component services behind
``POST /api/v1/routes/``; the Django view stays thin and only maps the
typed exceptions raised here to the specified HTTP statuses.

Component mapping (existing, unchanged services):

* GeocodingService   -> routing.services.geocoding (geocode_route_locations)
* RoutingService     -> routing.services.routing.RoutingService
* FuelStationMatcher -> routing.services.route_stations
* FuelOptimizer      -> routing.services.fuel_optimizer

The endpoint executes exactly the assessment's 11-step flow, with no
additional processing:

  1. Validate request            7. Order stations by route position
  2. Geocode start               8. Run fuel optimization
  3. Geocode finish              9. Calculate total fuel consumed
  4. Calculate driving route    10. Calculate total fuel cost
  5. Obtain route geometry      11. Return route and fuel information
  6. Fuel stations within 10 miles

(Steps 6 and 7 are both served by find_stations_along_route, which
returns stations within settings.MAX_STATION_DEVIATION_MILES sorted by
distance from the route start.)

Error contract (translated to HTTP by the view — no stack traces):

* InvalidRequestError                     -> 400 INVALID_REQUEST
* routing.NoRouteError                    -> 422 NO_ROUTE
* fuel_optimizer.RouteNotFeasibleError    -> 422 (code ROUTE_NOT_FEASIBLE)
* geocoding.GeocodingError / RoutingError -> 502 EXTERNAL_SERVICE_ERROR
"""

import logging
import time

from routing.services import fuel_optimizer
from routing.services import geocoding
from routing.services import route_stations
from routing.services.routing import RoutingService

logger = logging.getLogger(__name__)


class InvalidRequestError(Exception):
    """Request validation failure (HTTP 400, code INVALID_REQUEST)."""

    code = "INVALID_REQUEST"


class RoutePlanningService:
    """Plan a USA route end to end: geocode -> route -> stations -> fuel."""

    def plan(self, payload):
        """Execute the 11-step flow for one route request.

        Returns the exact response structure required by Section 7.
        Raises InvalidRequestError (400), NoRouteError /
        RouteNotFeasibleError (422) or GeocodingError / RoutingError
        subclasses (502) — all handled by the view.
        """
        started_at = time.monotonic()
        start_address, finish_address = self._validate(payload)          # 1
        locations = geocoding.geocode_route_locations(                   # 2, 3
            start_address, finish_address
        )
        start = self._require_location("start", locations["start"])      # 2
        finish = self._require_location("finish", locations["finish"])   # 3
        route = RoutingService().get_route(                              # 4
            start["latitude"],
            start["longitude"],
            finish["latitude"],
            finish["longitude"],
        )
        geometry = route["geometry"]                                     # 5
        stations = route_stations.find_stations_along_route(geometry)    # 6, 7
        logger.info("candidate stations: %d", len(stations))
        fuel_plan = fuel_optimizer.optimize_fuel_stops(                  # 8
            route["distance_miles"], stations
        )
        logger.info(
            "fuel optimization executed: %d stop(s) planned",
            len(fuel_plan["fuel_stops"]),
        )
        total_gallons = fuel_plan["total_gallons_consumed"]              # 9
        total_cost = fuel_plan["total_fuel_cost"]                        # 10
        result = self._build_response(                                   # 11
            start, finish, route, stations, fuel_plan, total_gallons, total_cost
        )
        logger.info(
            "route request duration: %.1f ms",
            (time.monotonic() - started_at) * 1000,
        )
        return result

    # --- Step 1: request validation ----------------------------------------
    @staticmethod
    def _validate(payload):
        """Validate the JSON payload; returns (start, finish) strings."""
        if not isinstance(payload, dict):
            raise InvalidRequestError("Request body must be a JSON object")
        addresses = {}
        for field in ("start", "finish"):
            if field not in payload:
                raise InvalidRequestError(f"'{field}' is required")
            value = payload[field]
            if not isinstance(value, str):
                raise InvalidRequestError(f"'{field}' must be a string")
            if not value.strip():
                raise InvalidRequestError(f"'{field}' must not be empty")
            addresses[field] = value.strip()
        return addresses["start"], addresses["finish"]

    @staticmethod
    def _require_location(field, location):
        """A geocoder no-match (unknown or non-USA) is an invalid request."""
        if location is None:
            raise InvalidRequestError(
                f"'{field}' does not resolve to a location in the USA"
            )
        return location

    # --- Step 11: response building ----------------------------------------
    @staticmethod
    def _build_response(
        start, finish, route, stations, fuel_plan, total_gallons, total_cost
    ):
        """Assemble the exact Section 7 response structure."""
        stations_by_id = {
            station["station_id"]: station for station in stations
        }

        fuel_stops = []
        for stop in fuel_plan["fuel_stops"]:
            station = stations_by_id[stop["station_id"]]
            fuel_stops.append(
                {
                    "station_id": str(stop["station_id"]),
                    "station_name": station["station_name"],
                    "address": station["address"],
                    "city": station["city"],
                    "state": station["state"],
                    "latitude": float(station["latitude"]),
                    "longitude": float(station["longitude"]),
                    "price_per_gallon": float(station["price_per_gallon"]),
                    "distance_from_start_miles": float(
                        stop["distance_from_route_start_miles"]
                    ),
                    "gallons_purchased": float(stop["gallons_purchased"]),
                    "fuel_cost": float(stop["cost"]),
                }
            )

        return {
            "start": {
                "name": start["display_name"],
                "latitude": float(start["latitude"]),
                "longitude": float(start["longitude"]),
            },
            "finish": {
                "name": finish["display_name"],
                "latitude": float(finish["latitude"]),
                "longitude": float(finish["longitude"]),
            },
            "route": {
                "distance_miles": float(route["distance_miles"]),
                "duration_minutes": float(route["duration_minutes"]),
                "geometry": route["geometry"],
            },
            "vehicle": {
                "mpg": fuel_optimizer.MPG,
                "max_range_miles": fuel_optimizer.MAX_RANGE_MILES,
                "tank_capacity_gallons": fuel_optimizer.TANK_CAPACITY_GALLONS,
            },
            "fuel_stops": fuel_stops,
            "fuel_summary": {
                "total_distance_miles": float(route["distance_miles"]),
                "total_gallons": float(total_gallons),
                "total_fuel_cost": float(total_cost),
            },
        }
