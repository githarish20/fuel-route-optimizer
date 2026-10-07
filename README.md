# Fuel Route Optimizer

Django backend engineering assessment: plan fuel stops along a USA driving
route from a supplied fuel-price dataset.

## 1. Project Title

Fuel Route Optimizer

## 2. Problem Statement

Given a start address and a finish address in the United States, compute the
driving route between them, find fuel stations near that route with their
current retail prices, and produce a cost-minimizing refueling plan for a
fixed vehicle (500-mile range, 10 MPG, 50-gallon tank). The solution must
serve the supplied fuel-price CSV from a database, call external geocoding
and routing providers with a strictly limited number of API calls, expose the
result as a REST endpoint, and render it on a map page — with a complete
automated test suite in which all external services are mocked.

## 3. Assessment Requirements

Implemented requirements (Sections 1–11):

1. Django project foundation with a health endpoint.
2. Fuel dataset model and import command (CSV never modified).
3. Geocoding service plus one-time fuel-station coordinate preprocessing.
4. Routing service returning normalized distance, duration, and geometry.
5. Fuel stations within 10 miles of the route, using Shapely, ordered by
   route position.
6. Fuel-stop optimization under the fixed 500-mile / 10 MPG / 50-gallon
   vehicle.
7. Main route API endpoint executing the specified 11-step flow.
8. Plain HTML/CSS/JavaScript map page (Leaflet) that only calls the project's
   own API.
9. Performance discipline (one routing call per uncached request, database —
   not CSV — serving requests) and console logging of the required events.
10. Validation and error handling with a single error envelope and no stack
    traces in responses.
11. Complete automated test suite with all external providers mocked.

### Assumptions (required by the implementation)

These decisions are load-bearing; the code behaves exactly as described:

- **Vehicle is fixed and starts with a full tank.** The initial 50 gallons
  are not purchased at a station, so a route of 500 miles or less has no fuel
  stops and a total fuel cost of 0.00. Fuel is bought only at candidate
  stations, never at the route start or at the destination.
- **USA-only input.** Geocoding is sent with `countrycodes=us` (Nominatim
  hard filter). An address that does not resolve inside the USA — or does not
  resolve at all — is treated as an invalid request (HTTP 400
  `INVALID_REQUEST`), not as a provider failure.
- **Error mapping.** OSRM's application-level `NoRoute` → 422 `NO_ROUTE`;
  provider transport/parse failures → 502 `EXTERNAL_SERVICE_ERROR`; an
  infeasible fuel plan → 422 `ROUTE_NOT_FEASIBLE`; unexpected internal errors
  (e.g. corrupt stored data) → 500 `INTERNAL_ERROR` with the traceback logged
  server-side only.
- **Stations with missing coordinates are skipped silently**; the request
  still succeeds with HTTP 200.
- **CSV duplicate rule:** the first row for an `OPIS Truckstop ID` wins;
  later rows for the same ID are counted as duplicates. Skipped rows
  (invalid price or missing required fields) are counted separately.
- **Route distance comes solely from the routing provider** and drives all
  fuel-consumption math; station positions are measured along the returned
  geometry. The 10-mile deviation limit is inclusive (`<= 10`).
- **Money is rounded per stop** (2 decimals, ROUND_HALF_UP) and the total is
  the sum of the listed stop costs, so `fuel_summary.total_fuel_cost` always
  equals the sum of `fuel_stops[].fuel_cost`.
- **Response conventions:** `start.name`/`finish.name` are the geocoder's
  `display_name`; `station_id` is serialized as a string.

## 4. Technology Stack

- Python 3.12
- Django 6.1.1
- Django REST Framework 3.18.1
- Shapely 2.1.2 (geometry: station-to-route distance and position)
- SQLite (development database)
- Django LocMem cache (geocoding and routing results)
- Standard-library logging (console)
- Map page: plain HTML/CSS/JavaScript with Leaflet 1.9.4 and OpenStreetMap
  tiles loaded from public CDNs

`requirements.txt` contains exactly these pinned Python dependencies:

```
Django==6.1.1
djangorestframework==3.18.1
Shapely==2.1.2
```

## 5. Architecture

One Django project (`config`) with one app (`routing`). The route request is
handled by a thin view that delegates to a coordinator service:

```
POST /api/v1/routes/
        │
        ▼
routing.views.routes                (thin: parse body, map exceptions →
        │                            HTTP status + error envelope)
        ▼
RoutePlanningService.plan()         routing/services/route_planning.py
        │  1. validate request
        │  2–3. geocode start/finish      → routing/services/geocoding.py
        │  4. driving route               → routing/services/routing.py
        │  5. route geometry (LineString)
        │  6–7. stations within 10 mi, ordered by route position
        │                                   → routing/services/route_stations.py
        │  8. fuel optimization            → routing/services/fuel_optimizer.py
        │  9. total gallons consumed
        │  10. total fuel cost
        │  11. build response
        ▼
JSON response
```

Component mapping to the assessment: GeocodingService → `geocoding`,
RoutingService → `routing.routing`, FuelStationMatcher → `route_stations`,
FuelOptimizer → `fuel_optimizer`.

Supporting pieces: `routing/models.py` (`FuelStation`),
`routing/management/commands/` (import and geocoding preprocessing),
`routing/templates/routing/map.html` (map page), `routing/tests.py` (test
suite), `routing/urls.py` + `config/urls.py` (routing of URLs).

## 6. Directory Structure

```
fuel_route_optimizer/
├── manage.py
├── fuel-prices-for-be-assessment.csv   # byte-identical copy of the supplied dataset
├── requirements.txt
├── .env.example                        # environment-variable template
├── .gitignore
├── README.md
├── config/                             # project configuration
│   ├── settings.py                     # apps, cache, logging, MAX_STATION_DEVIATION_MILES
│   ├── urls.py                         # /api/v1/ include + /map/
│   ├── wsgi.py / asgi.py
├── routing/                            # the application
│   ├── models.py                       # FuelStation
│   ├── views.py                        # health, routes, map_page
│   ├── urls.py                         # health/, routes/
│   ├── tests.py                        # full test suite (63 tests)
│   ├── services/
│   │   ├── geocoding.py                # Nominatim wrapper (throttled, cached)
│   │   ├── routing.py                  # OSRM wrapper (cached)
│   │   ├── route_stations.py           # Shapely station matching
│   │   ├── fuel_optimizer.py           # greedy fuel-stop planner (Decimal)
│   │   └── route_planning.py           # 11-step coordinator
│   ├── management/commands/
│   │   ├── import_fuel_data.py         # CSV → database
│   │   └── geocode_fuel_stations.py    # one-time station preprocessing
│   ├── templates/routing/map.html      # Leaflet map page
│   └── migrations/                     # 0001 (model), 0002 (geocode fields)
```

## 7. Dataset

`fuel-prices-for-be-assessment.csv` is a **byte-identical copy** of the
supplied file; the original is never modified by any code path. It contains
8,151 data rows with these columns:

```
OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price
```

The dataset contains **no coordinate columns**; latitude/longitude are
obtained later by the geocoding preprocessing step (Section 10) and remain
NULL until then.

## 8. Database Model

`routing.models.FuelStation` — field names follow the CSV columns:

| Field | Type | Notes |
|---|---|---|
| `opis_truckstop_id` | CharField(16), unique | "OPIS Truckstop ID"; uniqueness prevents double imports |
| `truckstop_name` | CharField(255) | "Truckstop Name" |
| `address` | CharField(255) | "Address" |
| `city` | CharField(100) | "City" |
| `state` | CharField(2) | "State" |
| `rack_id` | CharField(16), nullable | "Rack ID" (source information) |
| `retail_price` | **DecimalField(10, 8)** | "Retail Price" — Decimal, never float |
| `latitude` / `longitude` | FloatField, nullable | NULL until geocoded |
| `geocode_failed` | BooleanField | preprocessing: recorded no-match, never retried |
| `geocode_error` | TextField | preprocessing: failure reason |

## 9. Dataset Import Process

`python manage.py import_fuel_data [--path /path/to/file.csv]`

- Reads the bundled CSV by default (`--path` imports an alternate file; the
  tests use this and never read the production file).
- Validates every row: required fields present and non-empty, price matching
  `^\d+(\.\d+)?$`, converted **straight to `Decimal`** from the string.
- Duplicate handling: the first row for an `OPIS Truckstop ID` wins; later
  rows for the same ID (and IDs already in the database) count as
  duplicates. Re-running the command is idempotent.
- Malformed rows never abort the import; they are counted as skipped and the
  first 10 reasons are printed to stderr.
- Uses `bulk_create` in batches of 1,000 inside a single transaction.

Verified output for the supplied dataset:

```
Total rows: 8151
Imported rows: 6738
Skipped rows: 0
Duplicate rows: 1413
Invalid prices: 0
Missing required fields: 0
```

Invariant: `total = imported + duplicates + skipped`.

## 10. Geocoding Process

Provider: **Nominatim (OpenStreetMap)** — `GET
https://nominatim.openstreetmap.org/search` with `q=<address>&format=jsonv2&limit=1&countrycodes=us`.

- `countrycodes=us` is a hard filter: an address outside the USA finds no
  match, which the API treats as an invalid request (400).
- The service enforces the provider's absolute limit of **1 request per
  second** itself (`_throttle`), sends an identifying User-Agent, and shows
  ODbL attribution (`OSM_ATTRIBUTION`).
- `geocode_address(address)` returns `{latitude, longitude, display_name}`
  on success, `None` on a valid no-match, and raises typed
  `GeocodingError` subclasses on HTTP failure, timeout, or an unparseable
  response.
- Successful matches **and no-match responses are cached** (provider policy
  requires responses to be cached); transient failures are never cached.

**Fuel stations are geocoded once as preprocessing, never during route
requests:**

```bash
python manage.py geocode_fuel_stations            # all pending stations
python manage.py geocode_fuel_stations --limit N  # bounded batch
python manage.py geocode_fuel_stations --limit 0  # preview counts, 0 requests
```

The query is built only from dataset fields: `"<name>, <city>, <state>"`
(highway-exit style addresses in the dataset were verified to break
matching). Successes save coordinates; failures are recorded on the row
(`geocode_failed`, `geocode_error`) and never re-attempted; stations with
coordinates are never re-geocoded, so runs are restartable.

Status as of the last preprocessing run (2026-10-05): **22 geocoded,
83 recorded as no-match, 6,633 pending** — a ≈20% success rate on the
processed batch. Unmatched stations are recorded as failures rather than
assigned invented coordinates.

## 11. Routing API

Provider: **OSRM demo server** — `GET
https://router.project-osrm.org/route/v1/driving/{lon},{lat};{lon},{lat}?overview=full&geometries=geojson`
(driving profile, keyless).

`RoutingService().get_route(start_lat, start_lon, finish_lat, finish_lon)`
returns normalized internal data only:

```
{
    "distance_miles": ...,        # OSRM meters / 1609.344
    "duration_minutes": ...,      # OSRM seconds / 60
    "geometry": {"type": "LineString", "coordinates": [[lon, lat], ...]}
}
```

Provider-specific structures never leave the module. Typed failures:
`RoutingTimeout`, `RoutingHTTPError`, `RoutingInvalidResponse`,
`NoRouteError` (provider code `NoRoute`), and a generic `RoutingError` for
other provider status codes. One provider, no fallbacks (per spec).

## 12. External API Call Strategy

| Operation | External calls |
|---|---|
| `POST /api/v1/routes/` (uncached) | 2 Nominatim (start, finish) + 1 OSRM |
| Same request repeated (cached) | 0 |
| Retry of an unmatched address | 0 (no-match cached) |
| `import_fuel_data` | 0 |
| `find_stations_along_route` | 0 (local Shapely math only) |
| `optimize_fuel_stops` | 0 (local Decimal math only) |
| Map page (`/map/`) | 0 to providers; its only API call is `POST /api/v1/routes/` |
| `geocode_fuel_stations` | 1 per pending station — preprocessing only, ≤ 1 req/s |

Fuel stations always come from the **database**; the CSV is read only by
`import_fuel_data`, never during a request. Fuel stations are geocoded only
as preprocessing, never during route requests.

**Logging** (console, INFO, standard library) records the required events:
`route cache hit` / `route cache miss`, `routing API request: <url>`,
`candidate stations: N`, `fuel optimization executed: N stop(s) planned`,
and `route request duration: X ms` (emitted on successful requests). Both
providers are keyless, so no credentials exist to leak into logs.

## 13. Fuel Station Matching

`find_stations_along_route(route_geometry)` — purely local, no API calls:

- All route and station coordinates are projected into a local spherical
  equirectangular metre plane centred on the route (Earth mean radius
  6,371,008.8 m), so Shapely computes real distances instead of raw
  degree-Euclidean ones.
- Per station: perpendicular `distance_from_route_miles` (Shapely
  `LineString.distance`) and route position
  `distance_from_route_start_miles` (Shapely `LineString.project`).
- Candidates are stations with `distance_from_route_miles <=
  settings.MAX_STATION_DEVIATION_MILES` (**10**, fixed in settings, never
  adjusted dynamically), sorted ascending by distance from the route start.
- Stations with NULL coordinates are excluded. Output keys:
  `station_id`, `station_name`, `address`, `city`, `state`, `latitude`,
  `longitude`, `price_per_gallon`, `distance_from_route_miles`,
  `distance_from_route_start_miles`.

## 14. 500-Mile Vehicle Constraint

Fixed assessment vehicle (constants in `fuel_optimizer.py`, not
user-configurable):

```
MAX_RANGE_MILES = 500        # = 50 gallons × 10 MPG on a full tank
TANK_CAPACITY_GALLONS = 50
```

- At any position, a candidate station must lie strictly ahead of the
  current position, before the destination, and within the 500-mile window
  (`position + MAX_RANGE_MILES`) to be eligible — a cheaper station the
  vehicle cannot reach is never chosen.
- Fuel in the tank never exceeds 50 gallons and never goes negative
  (defensive guard).
- If neither the destination nor any station is reachable, the optimizer
  raises the controlled `RouteNotFeasibleError` (code
  `ROUTE_NOT_FEASIBLE`, HTTP 422) instead of fabricating a plan.

## 15. 10 MPG Calculation

```
MPG = 10
```

- Fuel consumed over any segment: `miles / 10`.
- Total fuel consumed for the route: `route_distance_miles / 10`
  (`fuel_summary.total_gallons`).
- A vehicle's remaining range is `gallons × 10`; the destination-reachable
  check is `fuel × 10 >= remaining_miles`.
- Units: provider meters → miles via `/ 1609.344`; seconds → minutes via
  `/ 60`.

## 16. Fuel Optimization Algorithm

`optimize_fuel_stops(route_distance_miles, stations)` — greedy, purely
local, deterministic (equal inputs → equal output), Decimal arithmetic:

1. Start at position 0 with a **full 50-gallon tank** (500-mile range) and
   no station, so no purchase can occur at the route start.
2. If the destination is within current range (`fuel × 10 >= remaining`),
   drive there and finish — fuel is never bought at or after the
   destination.
3. Candidates are stations strictly ahead, before the destination, and
   within the 500-mile window.
4. If a candidate is **cheaper than the current location's price**, target
   it and purchase **only enough** to reach it (bounded by the tank).
5. Otherwise **fill up**, bounded by both the tank capacity and the
   remaining route (never buy fuel the route cannot burn), then drive to
   the farthest reachable station and repeat.
6. If neither the destination nor any station is reachable →
   `RouteNotFeasibleError` (422).

Every purchase is recorded as a stop with its station, position, price,
gallons, and cost. Covered by 13 unit tests (routes below/exactly/above 500
miles, cheap-reachable vs. cheap-unreachable stations, purchase sizing, tank
cap, Decimal rounding, infeasible routes).

## 17. Total Cost Calculation

- Per stop: `cost = quantize(gallons × price_per_gallon, 0.01,
  ROUND_HALF_UP)` — `Decimal` end to end; float inputs are normalized via
  `str()` before any arithmetic.
- `fuel_summary.total_fuel_cost` = sum of the per-stop rounded costs (so it
  always equals the sum of `fuel_stops[].fuel_cost`).
- `fuel_summary.total_gallons` = `route_distance_miles / 10` (total
  consumed, including fuel from the starting full tank).
- Routes ≤ 500 miles: no stops, total cost `0.0` (see Assumptions).

## 18. API Endpoint

Base prefix: `/api/v1/`

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health/` | Health check |
| POST | `/api/v1/routes/` | Route + fuel plan |
| GET | `/map/` | Map page (not an API) |

`POST /api/v1/routes/` request body: JSON object with two required
non-empty strings, `start` and `finish`.

## 19. Request Example

```bash
curl -X POST http://127.0.0.1:8000/api/v1/routes/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": "Chicago, IL"}'
```

Health check:

```bash
curl http://127.0.0.1:8000/api/v1/health/
# {"status": "ok"}   (HTTP 200)
```

## 20. Response Example

Successful response (HTTP 200). Structure is exact; the values below are
illustrative and the `geometry.coordinates` array is abbreviated (the real
response contains the full polyline):

```json
{
  "start": {
    "name": "New York, New York County, New York, United States",
    "latitude": 40.7128,
    "longitude": -74.006
  },
  "finish": {
    "name": "Chicago, Cook County, Illinois, United States",
    "latitude": 41.8781,
    "longitude": -87.6298
  },
  "route": {
    "distance_miles": 742.6,
    "duration_minutes": 690.0,
    "geometry": {
      "type": "LineString",
      "coordinates": [[-74.006, 40.7128], [-87.6298, 41.8781]]
    }
  },
  "vehicle": {
    "mpg": 10,
    "max_range_miles": 500,
    "tank_capacity_gallons": 50
  },
  "fuel_stops": [
    {
      "station_id": "1042",
      "station_name": "Sample Truck Stop",
      "address": "1000 Main St",
      "city": "Sampleton",
      "state": "OH",
      "latitude": 40.1,
      "longitude": -82.9,
      "price_per_gallon": 3.29,
      "distance_from_start_miles": 480.0,
      "gallons_purchased": 16.0,
      "fuel_cost": 52.64
    },
    {
      "station_id": "2087",
      "station_name": "Example Fuel Plaza",
      "address": "2200 Highway Blvd",
      "city": "Sampleburg",
      "state": "IN",
      "latitude": 40.4,
      "longitude": -85.6,
      "price_per_gallon": 3.05,
      "distance_from_start_miles": 660.0,
      "gallons_purchased": 8.26,
      "fuel_cost": 25.19
    }
  ],
  "fuel_summary": {
    "total_distance_miles": 742.6,
    "total_gallons": 74.26,
    "total_fuel_cost": 77.83
  }
}
```

## 21. Error Responses

Every failure returns exactly this envelope — never a stack trace, file
path, credential, or provider payload:

```json
{"error": {"code": "INVALID_REQUEST", "message": "'start' is required"}}
```

| Condition | Status | `code` |
|---|---|---|
| Invalid JSON body, missing/empty/non-string start or finish, address not resolvable in the USA | 400 | `INVALID_REQUEST` |
| OSRM reports no driving route between the coordinates | 422 | `NO_ROUTE` |
| No candidate stations near the route / fuel plan cannot be completed (incl. no feasible plan) | 422 | `ROUTE_NOT_FEASIBLE` |
| Geocoding or routing provider failure (timeout, HTTP error, invalid response) | 502 | `EXTERNAL_SERVICE_ERROR` |
| Unexpected internal error (e.g. corrupt stored fuel price) | 500 | `INTERNAL_ERROR` |

Additional behaviors: a station with **missing coordinates** is skipped and
the request still returns 200; for 500 errors the full traceback is logged
to the server console only (`logger.exception`) and the client receives the
clean envelope.

## 22. Map Usage

```bash
python manage.py runserver
# open http://127.0.0.1:8000/map/
```

- Enter start and finish addresses, click **Calculate Route**.
- The page renders the returned route **polyline**, **start/finish
  markers**, and **fuel-station markers**; a fuel marker's popup shows
  station name, city, state, price per gallon, gallons purchased, and fuel
  cost.
- The **summary panel** shows total distance, total gallons, total fuel
  cost, number of fuel stops, vehicle MPG, and maximum range.
- The frontend performs **no fuel calculations** (values are displayed
  verbatim from the API response; the only count is the number of fuel
  stops) and **never contacts routing/geocoding providers** — its single
  network call is `POST /api/v1/routes/` (asserted by a test).
- Leaflet 1.9.4 and OpenStreetMap tiles load from public CDNs with OSM
  attribution. No login, registration, or other pages.

## 23. Caching

Django's configured cache (LocMem, `CACHES` in `settings.py`; no Redis per
spec):

| Domain | Key | Cached values |
|---|---|---|
| Geocoding | SHA-256 of the normalized (lowercased, whitespace-collapsed) address | match dicts **and** no-match sentinel; transient failures never cached |
| Routing | SHA-256 of the four start/finish coordinates | full normalized route dict |

Entries use Django's default 300-second timeout and are lost on process
restart (per-process, as LocMem). Effect: a repeated identical route request
makes zero external calls; a repeated unmatched address makes zero external
calls.

## 24. Testing

```bash
python manage.py check    # currently: no issues, exit 0
python manage.py test     # currently: 63 tests, OK, exit 0
```

All external providers are mocked (a single `urlopen` dispatcher answers
Nominatim and OSRM requests); **no test performs a real network call**.
Single test module: `routing/tests.py`.

Coverage by required category:

- **Health endpoint** — 200 `{"status": "ok"}`.
- **Import (10 tests)** — all source fields stored, price kept as exact
  Decimal (never float), whitespace stripping, duplicates keep the first
  price, exact duplicates, idempotent second run, missing required values
  skipped without crashing, invalid prices skipped without crashing.
- **Geocoding (6 tests)** — neutral result shape, invalid response raises,
  HTTP failure raises, timeout raises, cached result avoids a second API
  call, **no-match is cached and avoids a second API call**.
- **Routing (6 tests)** — successful route, timeout, HTTP error, invalid
  response, cached route, cached request does not call the API.
- **Station matching (6 tests)** — station on the route, within 10 miles,
  beyond 10 miles, ordering by route position, station before the start,
  station after the destination.
- **Fuel optimizer (13 tests)** — below/exactly/above 500 miles, multiple
  stops, cheap-reachable vs. cheap-unreachable, buy-only-enough, fill-up
  behavior, destination reached without further stops, infeasible route,
  exact 500-mile boundary, 50-gallon tank cap, Decimal cost rounding.
- **Route endpoint (9 tests)** — exact response structure, missing/empty
  start and finish → 400, full multi-call flow with fuel-stop contents,
  no route → 422, infeasible plan → 422, provider failure → 502.
- **Map page (1 test)** — required controls render; page contains no
  provider references.
- **Performance (2 tests)** — a cached route makes exactly one routing call
  with geocodes cached and the cache hit logged; `builtins.open` guarded
  during `POST` proves no `.csv` read happens in the request path.
- **Error handling (10 tests)** — invalid JSON → 400, non-USA → 400,
  unresolvable start/finish → 400, routing timeout/invalid response → 502,
  no stations near route → 422, infeasible plan → 422, corrupt stored price
  → 500 with the traceback asserted present in the server log and absent
  from the response, missing station coordinates → 200 (skipped). Every
  error test asserts the exact envelope and the absence of
  `Traceback`/`File "` in the body.

## 25. Local Installation

```bash
cd fuel_route_optimizer
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 26. Environment Variables

Documented in `.env.example`. Only one variable is used:

| Variable | Required | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | No (local) | Django secret key. `settings.py` falls back to an insecure development-only key when unset, so `check`/`test`/`runserver` work out of the box. |

No API keys exist anywhere: both providers are keyless.

## 27. How to Import Fuel Data

```bash
python manage.py migrate
python manage.py import_fuel_data

# optional: import a different CSV (used by the tests)
python manage.py import_fuel_data --path /path/to/other.csv
```

Then, to (optionally) populate station coordinates — preprocessing, rate
limited to 1 request/second:

```bash
python manage.py geocode_fuel_stations --limit 100
```

## 28. How to Run the Server

```bash
source .venv/bin/activate
python manage.py runserver
```

- Health check: `GET http://127.0.0.1:8000/api/v1/health/`
- Route API: `POST http://127.0.0.1:8000/api/v1/routes/`
- Map page: `http://127.0.0.1:8000/map/`

## 29. Known Limitations

1. **Station geocoding is incomplete.** As of the last preprocessing run:
   22 stations geocoded, 83 recorded no-match, 6,633 pending. Only stations
   with coordinates can appear in a route plan, so most of the dataset is
   currently excluded from matching. Completing preprocessing requires live
   Nominatim calls at 1 request/second (batched via `--limit`).
2. **Geocoding success rate is low (~20% on the processed batch).** Stations
   the provider cannot resolve are recorded as failures and never assigned
   invented coordinates — the tradeoff is fewer usable stations.
3. **Routing depends on the OSRM demo server**: keyless, best-effort, and
   explicitly not for heavy usage by its maintainers. There is one provider
   and no fallback, as specified. An outage surfaces as HTTP 502.
4. **LocMem cache is per-process and volatile.** It is cleared on restart
   and not shared between worker processes; no Redis per the assessment's
   constraints. Repeated requests may therefore re-call providers across
   restarts.
5. **Uncached route requests take at least ~1 second** because geocoding
   must respect Nominatim's absolute 1 request/second limit (two geocodes
   per request).
6. **Development configuration:** `DEBUG = True`, SQLite, no
   authentication, no HTTPS. The fallback secret key must not be used in
   production (`DJANGO_SECRET_KEY` overrides it).
7. **Route results are not persisted.** Each request computes and returns a
   plan; nothing is stored beyond the dataset and geocode cache (no history
   was required by the assessment).
