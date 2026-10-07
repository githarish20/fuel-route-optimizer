"""Views for the routing application (Sections 1, 7, 8 and 10)."""

import logging

from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from routing.services import fuel_optimizer
from routing.services import geocoding
from routing.services import route_planning
from routing.services.routing import NoRouteError, RoutingError

logger = logging.getLogger(__name__)


@api_view(["GET"])
def health(request):
    """Return the service health status: GET /api/v1/health/."""
    return Response({"status": "ok"})


@api_view(["POST"])
def routes(request):
    """Calculate a route and fuel plan: POST /api/v1/routes/ (Section 7).

    Thin by design: validation, the 11-step flow and response building
    live in RoutePlanningService; this view only translates its typed
    exceptions into the specified statuses and error envelope, so stack
    traces never reach the client.
    """
    try:
        payload = request.data
    except ParseError:
        return _error("INVALID_REQUEST", "Request body must be valid JSON", 400)

    try:
        result = route_planning.RoutePlanningService().plan(payload)
    except route_planning.InvalidRequestError as exc:
        return _error(exc.code, str(exc), 400)
    except NoRouteError as exc:  # before RoutingError (subclass)
        return _error("NO_ROUTE", str(exc), 422)
    except fuel_optimizer.RouteNotFeasibleError as exc:
        return _error(exc.code, str(exc), 422)
    except (geocoding.GeocodingError, RoutingError) as exc:
        return _error("EXTERNAL_SERVICE_ERROR", str(exc), 502)
    except Exception:
        # Section 10: unexpected internal errors (e.g. corrupt stored
        # data) return the clean envelope — stack traces, file paths and
        # credentials go to the server log only, never to the client.
        logger.exception("unhandled error during route request")
        return _error(
            "INTERNAL_ERROR",
            "An unexpected internal error occurred",
            500,
        )
    return Response(result)


def _error(code, message, status):
    """The specified error envelope: {"error": {"code", "message"}}."""
    return Response({"error": {"code": code, "message": message}}, status=status)


def map_page(request):
    """Serve the Leaflet map page (Section 8): GET /map/."""
    return render(request, "routing/map.html")
