"""Provider-agnostic geocoding service (Section 3).

The application only ever sees the neutral result shape returned by
``geocode_address``::

    {"latitude": float, "longitude": float, "display_name": str}

Provider-specific request/response handling stays inside this module.

---------------------------------------------------------------------------
Provider: Nominatim (OpenStreetMap) — verified 2026-10-05 against the
current official documentation (not a tutorial):* Endpoint   : GET https://nominatim.openstreetmap.org/search
               (the legacy /search.php URL is deprecated)
* Request   : q=<free-form address>&format=jsonv2&limit=1&countrycodes=us
               The public instance redirects to the Web UI when ``format``
               is omitted, so it is always sent explicitly. ``countrycodes``
               is a hard filter on ISO 3166-1 alpha-2 codes (Nominatim
               5.3.2 manual, "Result restriction") and enforces Section 7's
               requirement that route locations are within the USA: a
               non-USA address simply finds no match (returns None).
* Response   : JSON array of places; ``lat`` and ``lon`` are STRINGS,
               ``display_name`` is a string; an empty array means no match.
               (Source: "Place Output Formats", Nominatim 5.3.2 Manual.)
* Cost       : free, no API key, no account.
* Rate limits: absolute maximum 1 request/second; single thread, single
               machine; scripts running longer than a day or run at regular
               intervals are restricted to 4 requests/minute; results must
               be cached. (_throttle() enforces >= 1 s between requests.)
* Attribution: data is ODbL — "Data (c) OpenStreetMap contributors" must be
               clearly displayed (exposed as OSM_ATTRIBUTION).
* Policy     : https://operations.osmfoundation.org/policies/nominatim/
               One-time bulk tasks "may be permissible" when small,
               single-threaded and cached; systematic/grid queries,
               autocomplete, scraping and reselling are forbidden.
---------------------------------------------------------------------------
"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from django.core.cache import cache

# --- Verified provider configuration ---------------------------------------
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
# Must identify the application (stock library User-Agents are rejected by
# policy). Add a contact address here before any production deployment.
USER_AGENT = "fuel-route-optimizer/1.0 (assessment project; Django)"
MIN_REQUEST_INTERVAL_SECONDS = 1.0  # policy: absolute maximum 1 req/s
REQUEST_TIMEOUT_SECONDS = 10
OSM_ATTRIBUTION = "Data \u00a9 OpenStreetMap contributors, ODbL 1.0"

# Cache key prefix for successful geocoding results.
GEOCODE_CACHE_PREFIX = "geocode:"


# --- Errors -----------------------------------------------------------------
class GeocodingError(Exception):
    """Base class for geocoding failures."""


class GeocodingHTTPError(GeocodingError):
    """The provider returned an HTTP error status."""


class GeocodingTimeout(GeocodingError):
    """The provider did not answer within the timeout."""


class GeocodingInvalidResponse(GeocodingError):
    """The provider returned a response that could not be parsed."""


# Module-level throttle state (single-threaded per the provider's policy).
_last_request_at = 0.0


def geocode_address(address):
    """Geocode a free-form address.

    Returns {"latitude": float, "longitude": float, "display_name": str}
    on success, or None when the provider found no match.

    Raises GeocodingError subclasses on HTTP failure, timeout or an
    invalid/unparseable response. Both matches and no-match responses
    are cached with Django's cache (the provider's usage policy requires
    responses to be cached, so retries of an unmatched address never
    repeat a live request); transient failures are never cached.
    """
    if not address or not address.strip():
        return None

    cache_key = _cache_key(address)
    cached = cache.get(cache_key)
    if cached is not None:
        # False is the cached no-match sentinel (a cache miss is None).
        return None if cached is False else cached

    body = _http_get(_build_url(address))
    result = _parse_response(body)
    cache.set(cache_key, False if result is None else result)
    return result


def geocode_route_locations(start_address, finish_address):
    """Geocode user-provided start and finish locations for a route request.

    This is the method route handling uses for user input (there is no
    separate public geocoding endpoint). Results go through
    geocode_address(), so they are cached. Returns a dict with "start" and
    "finish" keys; a value is None when the address could not be found.
    Failures (HTTP/timeout/invalid response) propagate to the caller.
    """
    return {
        "start": geocode_address(start_address),
        "finish": geocode_address(finish_address),
    }


# --- Internals --------------------------------------------------------------
def _cache_key(address):
    normalized = " ".join(address.split()).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return GEOCODE_CACHE_PREFIX + digest


def _build_url(address):
    # countrycodes=us: results are restricted to the USA (Section 7).
    query = urllib.parse.urlencode(
        {"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "us"}
    )
    return f"{NOMINATIM_ENDPOINT}?{query}"


def _throttle():
    """Enforce >= 1 request per second (provider rate limit)."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _http_get(url):
    """Perform one throttled HTTP GET and return the response body."""
    _throttle()
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
        raise GeocodingHTTPError(f"HTTP {exc.code} from geocoding provider") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise GeocodingTimeout(f"Geocoding request timed out: {exc.reason}") from exc
        raise GeocodingError(f"Geocoding request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GeocodingTimeout(f"Geocoding request timed out: {exc}") from exc

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GeocodingInvalidResponse("Response was not valid UTF-8") from exc


def _parse_response(body):
    """Parse a provider response into the neutral result shape.

    Isolates all provider-specific structure from the rest of the app.
    """
    try:
        data = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise GeocodingInvalidResponse("Geocoding provider returned invalid JSON") from exc

    if not isinstance(data, list):
        raise GeocodingInvalidResponse("Geocoding provider response is not a result list")
    if not data:
        return None  # valid response, no matching location

    first = data[0]
    try:
        return {
            "latitude": float(first["lat"]),
            "longitude": float(first["lon"]),
            "display_name": str(first["display_name"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodingInvalidResponse(
            "Geocoding provider response is missing lat/lon/display_name"
        ) from exc
