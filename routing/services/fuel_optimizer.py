"""Fuel-route optimization (Section 6).

Minimizes total fuel expenditure while guaranteeing the vehicle can
complete the route. Purely local and deterministic — no routing or
geocoding API calls, and no user-configurable vehicle parameters.

Fixed assessment vehicle:
    MAX_RANGE_MILES = 500   (= 50 gallons x 10 MPG, full tank at start)
    MPG = 10
    TANK_CAPACITY_GALLONS = 50

Greedy algorithm (stations processed in route order; the destination is
a final endpoint):

1. At each location, if the destination is within the current range,
   drive there and finish — no fuel is purchased at or after the
   destination (fuel the route cannot burn would be wasted money).
2. Otherwise consider candidate stations that are strictly ahead, before
   the destination, and directly reachable from here with a full tank
   (position + MAX_RANGE_MILES). Unreachable stations are ignored — the
   globally cheapest station is never chosen if it cannot be reached.
3. If a candidate is cheaper than the current location's price, it
   becomes the next target and we purchase ONLY enough fuel to reach it.
4. If no cheaper candidate exists, fill up — bounded by both the tank
   capacity and the remaining route — then continue toward the farthest
   reachable station.
5. Fuel is clamped to [0, TANK_CAPACITY_GALLONS] at all times.

At the route start the tank is full and there is no station, so no
purchase can occur there (the "first reachable station" is simply the
first waypoint).

Every purchase is recorded as a stop with its station, position, price,
gallons and cost. Price, gallons and costs use Decimal; monetary outputs
are rounded to 2 decimal places (ROUND_HALF_UP).

If neither the destination nor any station is reachable, the controlled
application-level error RouteNotFeasibleError is raised with message and
code "ROUTE_NOT_FEASIBLE" — never a fabricated solution.
"""

from decimal import Decimal, ROUND_HALF_UP

# Fixed vehicle parameters for this assessment (not user-configurable).
MAX_RANGE_MILES = 500
MPG = 10
TANK_CAPACITY_GALLONS = 50


class RouteNotFeasibleError(Exception):
    """Controlled application-level error (ROUTE_NOT_FEASIBLE)."""

    code = "ROUTE_NOT_FEASIBLE"

    def __init__(self, message="ROUTE_NOT_FEASIBLE"):
        super().__init__(message)


def optimize_fuel_stops(route_distance_miles, stations):
    """Plan fuel purchases for a route.

    ``route_distance_miles`` is the total route distance. ``stations`` is
    the ordered list of candidate stations (Section 5 output dicts); each
    needs ``station_id``, ``distance_from_route_start_miles`` and
    ``price_per_gallon``.

    Returns::

        {
            "fuel_stops": [
                {
                    "station_id": ...,
                    "distance_from_route_start_miles": ...,
                    "price_per_gallon": ...,
                    "gallons_purchased": ...,
                    "cost": ...,           # 2 decimal places
                },
                ...
            ],
            "total_gallons_consumed": ...,
            "total_fuel_cost": ...,        # 2 decimal places
        }

    Deterministic: equal inputs always produce the equal output.
    Raises RouteNotFeasibleError when the route cannot be completed.
    """
    route_distance = _to_decimal(route_distance_miles)
    tank = Decimal(TANK_CAPACITY_GALLONS)

    # (position, price, station) in route order; the stable sort keeps the
    # caller's order for equal positions, so results stay deterministic.
    normalized = sorted(
        (
            (
                _to_decimal(station["distance_from_route_start_miles"]),
                _to_decimal(station["price_per_gallon"]),
                station,
            )
            for station in stations
        ),
        key=lambda item: item[0],
    )

    position = Decimal(0)
    fuel = tank  # starting condition: full 50-gallon tank (500-mile range)
    current_price = None  # no station at the route start
    current_station = None
    stops = []

    while True:
        remaining = route_distance - position

        # Destination reachable with the current fuel -> finish (step 8).
        if fuel * MPG >= remaining:
            fuel -= remaining / MPG
            break

        # Candidate stations: strictly ahead, before the destination, and
        # directly reachable from here with a full tank (steps 2-3).
        candidates = [
            item
            for item in normalized
            if position < item[0] < route_distance
            and item[0] <= position + MAX_RANGE_MILES
        ]

        # Step 4: first reachable station cheaper than the current one.
        # At the route start (no current price) the first reachable
        # station is the next waypoint; the tank is full, so buy 0.
        if current_price is None:
            target = candidates[0] if candidates else None
        else:
            target = next(
                (item for item in candidates if item[1] < current_price), None
            )

        if target is not None:
            # Step 5: purchase only enough fuel to reach that station.
            needed = (target[0] - position) / MPG
            gallons = min(max(needed - fuel, Decimal(0)), tank - fuel)
        else:
            # Step 6: fill up, bounded by the tank AND the remaining route.
            # The destination is not reachable yet, so needed_total > fuel.
            needed_total = remaining / MPG
            gallons = min(tank - fuel, needed_total - fuel)

        if gallons > 0:
            # Purchases happen at the current location, which is a station
            # (at the route start the tank is full, so gallons == 0).
            stop_cost = _money(gallons * current_price)
            stops.append(
                {
                    "station_id": current_station[2]["station_id"],
                    "distance_from_route_start_miles": position,
                    "price_per_gallon": current_price,
                    "gallons_purchased": gallons,
                    "cost": stop_cost,
                }
            )
            fuel = min(fuel + gallons, tank)  # never above 50 gallons

        # Decide where to drive next.
        if target is not None:
            next_item = target
        else:
            # Filled up: the destination may now be reachable (the loop
            # top will finish); otherwise head for the farthest station
            # within the new range.
            if fuel * MPG >= route_distance - position:
                continue
            reachable = [
                item
                for item in normalized
                if position < item[0] < route_distance
                and item[0] <= position + fuel * MPG
            ]
            if not reachable:
                raise RouteNotFeasibleError()
            next_item = max(reachable, key=lambda item: item[0])

        # Step 9: drive the segment and consume fuel.
        distance = next_item[0] - position
        fuel -= distance / MPG
        if fuel < 0:  # defensive invariant guard; never negative
            raise RouteNotFeasibleError()
        position = next_item[0]
        current_price = next_item[1]
        current_station = next_item

    total_cost = _money(sum((stop["cost"] for stop in stops), Decimal("0.00")))
    return {
        "fuel_stops": stops,
        "total_gallons_consumed": route_distance / MPG,
        "total_fuel_cost": total_cost,
    }


def _to_decimal(value):
    """Normalize a numeric input to Decimal (floats via str: no drift)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def _money(value):
    """Round a monetary Decimal to two decimal places."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
