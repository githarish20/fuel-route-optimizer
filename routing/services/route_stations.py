"""Fuel stations along a calculated route (Section 5).

Purely local computation: no routing API, no geocoding API, no new route —
everything uses the already calculated route LineString, the stations'
existing coordinates and their stored fuel prices.

Geometry
--------
Shapely does the geometry work (per spec). Lat/lon degrees are converted
appropriately first: all coordinates are projected into a local
azimuthal-ish plane (spherical **equirectangular** projection centred on
the route's mean latitude/longitude) using Earth's mean radius, so
distances come out in metres instead of raw degree-Euclidean units:

    x = R * cos(phi0) * radians(lon - lon0)
    y = R * radians(lat - phi0)

This is accurate to well under 1 % over the scale of a driving route
(unlike plain lat/lon Euclidean math or uncorrected Web Mercator, which
is off by 1/cos(latitude) — up to ~30 % at US latitudes).

Two measurements per station:

* ``distance_from_route_miles`` — perpendicular distance from the station
  to the route LineString (Shapely ``LineString.distance``).
* ``distance_from_route_start_miles`` — position along the route measured
  from the starting point (Shapely ``LineString.project``); stations
  before the start clamp to 0 and past the destination clamp to the
  route's full length.

Candidates = stations within ``settings.MAX_STATION_DEVIATION_MILES``
(default 10 miles), sorted by ``distance_from_route_start_miles``.
"""

import math

from django.conf import settings
from shapely.geometry import LineString, Point

from routing.models import FuelStation

EARTH_RADIUS_METERS = 6371008.8  # IUGG mean Earth radius
METERS_PER_MILE = 1609.344

# Output keys for each candidate station.
CANDIDATE_FIELDS = (
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
)


def find_stations_along_route(route_geometry):
    """Return candidate stations near an already calculated route.

    ``route_geometry`` is the route's GeoJSON LineString::

        {"type": "LineString", "coordinates": [[lon, lat], ...]}

    Returns a list of candidate dicts (see CANDIDATE_FIELDS) sorted by
    distance from the route start, ascending. Stations without
    coordinates are skipped. The deviation limit comes from
    ``settings.MAX_STATION_DEVIATION_MILES`` (default 10).
    """
    coordinates = route_geometry["coordinates"]
    if len(coordinates) < 2:
        raise ValueError("route LineString needs at least two coordinates")

    # Reference point for the local projection: mean of the route vertices.
    lon0 = sum(lon for lon, _lat in coordinates) / len(coordinates)
    phi0 = sum(lat for _lon, lat in coordinates) / len(coordinates)

    route_line = LineString([_to_meters(lon, lat, lon0, phi0) for lon, lat in coordinates])

    max_deviation_meters = (
        settings.MAX_STATION_DEVIATION_MILES * METERS_PER_MILE
    )

    candidates = []
    stations = FuelStation.objects.exclude(
        latitude__isnull=True
    ).exclude(longitude__isnull=True)
    for station in stations:
        point = Point(*_to_meters(station.longitude, station.latitude, lon0, phi0))

        distance_from_route_m = route_line.distance(point)
        if distance_from_route_m > max_deviation_meters:
            continue

        distance_along_route_m = route_line.project(point)
        candidates.append(
            {
                "station_id": station.id,
                "station_name": station.truckstop_name,
                "address": station.address,
                "city": station.city,
                "state": station.state,
                "latitude": station.latitude,
                "longitude": station.longitude,
                "price_per_gallon": station.retail_price,
                "distance_from_route_miles": distance_from_route_m / METERS_PER_MILE,
                "distance_from_route_start_miles": (
                    distance_along_route_m / METERS_PER_MILE
                ),
            }
        )

    candidates.sort(key=lambda candidate: candidate["distance_from_route_start_miles"])
    return candidates


def _to_meters(lon, lat, lon0, phi0):
    """Project WGS84 degrees into a local metre plane centred on (lon0, phi0)."""
    x = EARTH_RADIUS_METERS * math.cos(math.radians(phi0)) * math.radians(lon - lon0)
    y = EARTH_RADIUS_METERS * math.radians(lat - phi0)
    return (x, y)
