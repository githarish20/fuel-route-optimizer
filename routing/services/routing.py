"""Routing service (Section 4).

Calculates a driving route between two coordinates and returns normalized
internal data only::

    {
        "distance_miles": float,
        "duration_minutes": float,
        "geometry": {"type": "LineString", "coordinates": [[lon, lat], ...]},
    }

Provider-specific structures never leave this module.

---------------------------------------------------------------------------
Provider: OSRM demo server (OpenStreetMap) — ONE provider, no fallbacks.
Verified 2026-10-05 against the current official documentation (v5.24.0,
project-osrm.org/docs) and one live request, not a tutorial:

* Endpoint   : GET https://router.project-osrm.org/route/v1/driving/
               {lon},{lat};{lon},{lat}?overview=full&geometries=geojson
               Coordinates are {longitude},{latitude} pairs (lon first);
               profile "driving" = driving directions (verified with a USA
               route: HTTP 200, code "Ok").
* Response   : {"code": "Ok", "routes": [{"distance": <meters>,
               "duration": <seconds>, "geometry": {"type": "LineString",
               "coordinates": [[lon, lat], ...]}}]} — errors arrive as HTTP
               4xx with a provider code (NoSegment, NoRoute, TooBig, ...).
* Cost       : free, no API key, no account — so there are no credentials
               to expose.
* Limits     : the demo server is a best-effort service for testing;
               the maintainers state its usage policy forbids heavy usage
               (community.openstreetmap.org, OSRM maintainers, 2024).
               This service makes exactly ONE call per uncached route
               request, caches every result by coordinates, and is never
               called for fuel stations or route segments.
* Attribution: data © OpenStreetMap contributors (ODbL).
---------------------------------------------------------------------------
"""

import hashlib
import json
import logging
import urllib.error
import urllib.request

from django.core.cache import cache

logger = logging.getLogger(__name__)

OSRM_ENDPOINT = "https://router.project-osrm.org/route/v1/driving"
USER_AGENT = "fuel-route-optimizer/1.0 (assessment project; Django)"
REQUEST_TIMEOUT_SECONDS = 15

METERS_PER_MILE = 1609.344
SECONDS_PER_MINUTE = 60.0

ROUTE_CACHE_PREFIX = "route:"


# --- Errors -----------------------------------------------------------------
class RoutingError(Exception):
    """Base class for routing failures (including provider error codes)."""


class RoutingTimeout(RoutingError):
    """The provider did not answer within the timeout."""


class RoutingHTTPError(RoutingError):
    """The provider returned an HTTP error status."""


class RoutingInvalidResponse(RoutingError):
    """Invalid JSON/shape, or missing distance/duration/geometry."""


class NoRouteError(RoutingError):
    """The provider found no driving route between the coordinates.

    Distinct from external failures: OSRM reports "NoRoute" as an
    application-level condition (Section 7 maps it to HTTP 422 while
    transport/provider failures map to HTTP 502).
    """


class RoutingService:
    """Provider-agnostic driving-route calculation."""

    def get_route(
        self,
        start_latitude,
        start_longitude,
        finish_latitude,
        finish_longitude,
    ):
        """Return the driving route between two coordinates.

        Returns the normalized structure documented at the top of this
        module. Makes exactly one routing API call for an uncached
        request; identical coordinates are served from the Django cache
        (deterministic key from start/finish coordinates).

        Raises RoutingTimeout, RoutingHTTPError, RoutingInvalidResponse,
        NoRouteError (provider code "NoRoute") or RoutingError (other
        provider status codes).
        """
        start_latitude = float(start_latitude)
        start_longitude = float(start_longitude)
        finish_latitude = float(finish_latitude)
        finish_longitude = float(finish_longitude)

        cache_key = self._cache_key(
            start_latitude, start_longitude, finish_latitude, finish_longitude
        )
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info(
                "route cache hit (start=%s,%s finish=%s,%s)",
                start_latitude,
                start_longitude,
                finish_latitude,
                finish_longitude,
            )
            return cached
        logger.info(
            "route cache miss (start=%s,%s finish=%s,%s)",
            start_latitude,
            start_longitude,
            finish_latitude,
            finish_longitude,
        )

        url = self._build_url(
            start_latitude, start_longitude, finish_latitude, finish_longitude
        )
        # The provider is keyless, so the URL carries no credentials.
        logger.info("routing API request: %s", url)
        body = _http_get(url)
        result = self._parse_route(body)
        cache.set(cache_key, result)
        return result

    @staticmethod
    def _cache_key(start_latitude, start_longitude, finish_latitude, finish_longitude):
        """Deterministic key from the start and finish coordinates."""
        raw = ",".join(
            repr(float(value))
            for value in (
                start_latitude,
                start_longitude,
                finish_latitude,
                finish_longitude,
            )
        )
        return ROUTE_CACHE_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_url(start_latitude, start_longitude, finish_latitude, finish_longitude):
        # OSRM expects {longitude},{latitude} pairs.
        coordinates = (
            f"{start_longitude},{start_latitude};"
            f"{finish_longitude},{finish_latitude}"
        )
        return f"{OSRM_ENDPOINT}/{coordinates}?overview=full&geometries=geojson"

    @staticmethod
    def _parse_route(body):
        """Parse the provider response into the normalized structure."""
        try:
            data = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise RoutingInvalidResponse("Routing provider returned invalid JSON") from exc

        if not isinstance(data, dict):
            raise RoutingInvalidResponse("Routing provider response is not an object")

        code = data.get("code")
        if code is None:
            raise RoutingInvalidResponse("Routing provider response is missing 'code'")
        if code != "Ok":
            if code == "NoRoute":
                raise NoRouteError(
                    "Routing provider found no route between the coordinates"
                )
            raise RoutingError(f"Routing provider returned code '{code}'")

        routes = data.get("routes")
        if not isinstance(routes, list) or not routes:
            raise RoutingInvalidResponse("Routing provider response contains no routes")
        route = routes[0]
        if not isinstance(route, dict):
            raise RoutingInvalidResponse("Routing route entry is not an object")

        distance_meters = _require_number(route.get("distance"), "distance")
        duration_seconds = _require_number(route.get("duration"), "duration")

        geometry = route.get("geometry")
        if not isinstance(geometry, dict):
            raise RoutingInvalidResponse("Routing response is missing geometry")
        if geometry.get("type") != "LineString":
            raise RoutingInvalidResponse("Routing geometry is not a LineString")
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or not coordinates:
            raise RoutingInvalidResponse("Routing geometry has no coordinates")

        return {
            "distance_miles": distance_meters / METERS_PER_MILE,
            "duration_minutes": duration_seconds / SECONDS_PER_MINUTE,
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates,
            },
        }


def _require_number(value, name):
    if value is None:
        raise RoutingInvalidResponse(f"Routing response is missing {name}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingInvalidResponse(f"Routing response has invalid {name}")
    return float(value)


def _provider_error_code(exc):
    """Best-effort read of the provider's JSON ``code`` from an HTTP error.

    Returns None when the body is missing or unparseable — the failure is
    not swallowed: the caller then raises the generic RoutingHTTPError
    carrying the HTTP status.
    """
    try:
        data = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return None
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, str) else None


def _http_get(url):
    """Perform one HTTP GET and return the response body."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # OSRM reports application errors as HTTP 4xx with a JSON body
        # (e.g. {"code": "NoRoute"}); extract the provider code so
        # "no route" stays distinguishable from transport failures.
        if _provider_error_code(exc) == "NoRoute":
            raise NoRouteError(
                "Routing provider found no route between the coordinates"
            ) from exc
        raise RoutingHTTPError(f"HTTP {exc.code} from routing provider") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise RoutingTimeout(f"Routing request timed out: {exc.reason}") from exc
        raise RoutingError(f"Routing request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RoutingTimeout(f"Routing request timed out: {exc}") from exc

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RoutingInvalidResponse("Routing response was not valid UTF-8") from exc
