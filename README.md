# Fuel Route Optimizer

**Deployed API:** [https://fuel-route-optimizer.up.railway.app](https://fuel-route-optimizer.up.railway.app)

## Overview

A Django backend for the fuel-route planning assessment. Start and finish locations are geocoded within the USA; the API fetches a driving route, matches locally stored fuel stations, and returns cost-aware fuel purchases and total purchase cost. The vehicle uses **10 MPG**, has a **500-mile range**, and starts with a full tank.

## Live API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | [`/api/health/`](https://fuel-route-optimizer.up.railway.app/api/health/) | Health check |
| POST | [`/api/routes/`](https://fuel-route-optimizer.up.railway.app/api/routes/) | Driving route and fuel plan |

See [API Usage](#api-usage) for request and response examples.

## Key Features

- Django REST API with request validation and structured application errors.
- OpenRouteService / HeiGIT geocoding and driving directions.
- Atomic CSV import that preserves duplicate source rows and Decimal prices.
- Offline station coordinate enrichment and local station-to-route matching.
- Fuel optimization with partial purchases and infeasibility detection.
- At most three external provider operations for a cold successful route request; provider-result caching can eliminate calls on repeated requests.
- Automated unit and integration tests, including reference optimality checks.

## Architecture

Runtime:

```text
POST /api/routes/
  -> validate start/finish
  -> geocode start and finish (cached when available)
  -> fetch driving route (cached when available)
  -> for routes >500 miles: match pre-geocoded local stations
  -> optimize fuel purchases
  -> return locations, route geometry, stops and cost
```

Preprocessing:

```text
fuel-price CSV -> import_fuel_prices -> FuelStation table
               -> geocode_fuel_stations -> persisted latitude/longitude
```

Station geocoding runs ahead of time so a route request never needs thousands of external lookups. Routes within the initial 500-mile range need neither station lookup nor matching.

## External API Usage

The provider client uses **OpenRouteService / HeiGIT** at `https://api.heigit.org`:

| Operation | Path |
|---|---|
| Forward geocoding | `/pelias/v1/search` |
| Driving directions | `/openrouteservice/v2/directions/driving-car/geojson` |

On a cold successful request, it performs one geocode for start, one for finish, and one route request: **three provider operations**. Geocoding requests a single best match constrained to the USA. The client uses header authentication and explicit timeouts, without automatic retries or redirects.

Matching and optimization are local. Successful normalized provider results are cached; repeated equivalent requests may make zero provider calls, and partially cached requests fetch only missing results.

## Tech Stack

- Python 3.13 used during development; use Python 3.13 for the setup below.
- Django **6.1.1** and Django REST Framework **3.18.1**.
- HTTPX **0.28.1**.
- SQLite and Django's built-in **LocMemCache**.
- Standard-library CSV, Decimal and geographic math; no GIS or optimization dependencies.

Direct dependencies are pinned in [requirements.txt](requirements.txt).

## Project Structure

```text
config/
  settings.py, environment.py, urls.py
routes/
  models.py, serializers.py, views.py, urls.py
  migrations/
  management/commands/
    import_fuel_prices.py
    geocode_fuel_stations.py
  services/
    routing.py
    cached_routing.py
    route_planner.py
    station_geocoder.py
    station_matcher.py
    fuel_optimizer.py
  tests/
manage.py
requirements.txt
.env.example
```

| Service | Responsibility |
|---|---|
| `routing.py` | Provider HTTP requests, normalized immutable results and sanitized exceptions. |
| `cached_routing.py` | Best-effort caching around runtime provider operations. |
| `route_planner.py` | Orchestration, distance calibration and response formatting. |
| `station_geocoder.py` | Compose station queries, validate geographic evidence and persist accepted coordinates. |
| `station_matcher.py` | Local corridor filtering and route-position projection. |
| `fuel_optimizer.py` | Pure purchase optimization; no ORM, HTTP or provider dependency. |

## Setup

From a fresh clone, in the repository root, ensure `python3` selects Python 3.13:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export DEBUG=true
export OPENROUTESERVICE_API_KEY="your-api-key"

python manage.py migrate
python manage.py import_fuel_prices /path/to/fuel-prices-for-be-assessment.csv --replace

# Optional small provider/coordinate verification:
python manage.py geocode_fuel_stations --limit 3

python manage.py runserver
```

Obtain a provider key with access to geocoding and directions. The project **does not automatically load `.env` files**; export variables in the shell running Django. [.env.example](.env.example) lists the supported settings without real secrets.

Keep the supplied CSV outside the repository. Importing alone does not populate coordinates. Enrich enough stations to cover a long route before expecting a feasible fuel plan; a three-station smoke test does not provide nationwide coverage. Short routes can succeed without any station data.

## Environment Variables

| Variable | Default / behavior |
|---|---|
| `SECRET_KEY` | Trimmed environment value. Missing or whitespace-only values use a development-only fallback when `DEBUG=true`; startup fails when `DEBUG=false`. Supply a private key outside development. |
| `DEBUG` | Defaults to `true`. Accepts `true`/`false` or `1`/`0`, case-insensitive with surrounding whitespace removed. Other values fail at startup. |
| `ALLOWED_HOSTS` | Defaults to `localhost,127.0.0.1,[::1]`. Comma-separated hostnames; whitespace and empty entries are removed. No schemes or ports. |
| `OPENROUTESERVICE_API_KEY` | Empty by default; required for route requests and station enrichment. Not required to run mocked tests or import CSV data. |
| `ROUTING_CACHE_TTL_SECONDS` | Defaults to `86400` (24 hours). Non-negative integer; invalid values fail at startup. `0` bypasses cache reads and writes. |

An existing shell value such as `DEBUG=release` is invalid; explicitly export `DEBUG=true` for local development. The development server and development defaults are not a production deployment configuration.

## Fuel Data Import

```bash
python manage.py import_fuel_prices <csv_path>
python manage.py import_fuel_prices <csv_path> --replace
```

Required columns:

```text
OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price
```

The command validates the file, headers and rows, trims whitespace, and stores finite, strictly positive Decimal prices with up to eight fractional digits. Malformed rows fail with contextual errors rather than being skipped.

Without `--replace`, imports append every valid source row. Duplicate OPIS IDs, differing prices under one ID, and identical source rows are intentionally preserved. Batched inserts run in one transaction; failures roll back the entire import.

The supplied dataset also contains Canadian rows. Import preserves them, while start/finish geocoding and station enrichment are constrained to the USA.

`--replace` deletes existing station rows and reloads in the same transaction. **This also removes previously enriched coordinates** because the CSV contains none.

## Fuel Station Geocoding

```bash
python manage.py geocode_fuel_stations --limit 3
python manage.py geocode_fuel_stations
python manage.py geocode_fuel_stations --force --limit 3
```

- Default selection includes records missing either latitude or longitude; complete records are skipped.
- `--limit N` caps attempted records, including unresolved ones. N must be positive.
- `--force` includes already-geocoded records.
- Each station address combines name, street address, city and state and makes at most one request through the existing provider client.
- Results must provide US/USA country evidence, a matching state and city (locality, or localadmin when locality is absent), and a `venue`, `address` or `street` layer. Administrative, unknown and missing layers are rejected; case, punctuation and whitespace differences are normalized.
- Accepted coordinates are stored as six-place Decimals; only coordinate fields are updated.
- Unresolved stations are reported and processing continues without assigning fake coordinates. Existing coordinates remain intact if a forced lookup is unresolved.
- Configuration or systemic provider failures stop the command. Earlier successful updates remain saved so later runs can resume.

The command reports unresolved records and a final summary. Check provider quotas before large batches; automatic pacing and retry/backoff are not implemented.

### Design decision: preprocessing rather than runtime geocoding

| Approach | Benefits | Tradeoffs |
|---|---|---|
| Geocode stations during route requests | No initial enrichment step | Potentially hundreds or thousands of provider calls, added latency and rate-limit exposure; conflicts with the assessment's roughly 1–3-call goal. |
| Preprocess/backfill station coordinates | Reusable coordinates; fully local matching; predictable provider-call volume | Requires initial enrichment and review; stored locations can become stale. |

This implementation geocodes the user's start and finish at request time, uses stored fuel-station coordinates, and performs matching and optimization locally. A cold successful request uses **two geocodes and one routing request**; a warm cached repeat can use **zero provider requests**.

The dataset's highway/interchange-style addresses are not always reliably resolved by free-text HeiGIT/Pelias geocoding. The enrichment validator checks country/state/locality/layer evidence and leaves uncertain stations unresolved instead of silently saving incorrect coordinates. Matching requires both stored coordinates; there is no separate verification-status field, so coordinates should be populated only through validated enrichment or independently verified location evidence. Numeric coordinates and matching metadata alone do not prove exact station identity.

For production enrichment, structured geocoding, operator-provided coordinates or a more authoritative station-location dataset would be preferable. Importing the CSV does not guarantee that every station can be geocoded successfully.

## API Usage

### Short route: Dallas → Austin

`POST https://fuel-route-optimizer.up.railway.app/api/routes/`

```json
{
  "start": "Dallas, TX",
  "finish": "Austin, TX"
}
```

```bash
curl -X POST https://fuel-route-optimizer.up.railway.app/api/routes/ \
  -H "Content-Type: application/json" \
  -d '{"start":"Dallas, TX","finish":"Austin, TX"}'
```

Both fields are required strings, trimmed, nonblank and at most 255 characters. They must differ after trimming and case-insensitive comparison. Successful requests return HTTP 200.

The verified Dallas–Austin route is approximately **200.46 miles** and **3.2199 hours**. It is within the vehicle's initial 500-mile range, so `fuel.stops` is empty, purchased gallons are `"0.000000"`, and purchase cost is **$0.00**.

### Long route: Dallas → Van Horn

`POST https://fuel-route-optimizer.up.railway.app/api/routes/`

```json
{
  "start": "Dallas, TX",
  "finish": "Van Horn, TX"
}
```

Verified HTTP 200 response, abbreviated below. The `jsonc` comment replaces the full driving polyline for readability; the actual API returns a complete GeoJSON coordinate array.

```jsonc
{
  "start": {
    "query": "Dallas, TX",
    "label": "Dallas, TX, USA",
    "coordinates": {"longitude": -96.784359, "latitude": 32.736212}
  },
  "finish": {
    "query": "Van Horn, TX",
    "label": "Van Horn, TX, USA",
    "coordinates": {"longitude": -104.831363, "latitude": 31.040253}
  },
  "route": {
    "distance_miles": 519.686,
    "duration_hours": 7.7811,
    "geometry": {
      "type": "LineString",
      "coordinates": [ /* 3,031 coordinate pairs omitted */ ]
    }
  },
  "fuel": {
    "vehicle": {"mpg": 10, "max_range_miles": 500, "tank_capacity_gallons": 50},
    "stops": [{
      "opis_truckstop_id": 52674,
      "name": "FLYING J TRAVEL PLAZA #738",
      "address": "I-20/FM707, EXIT 277",
      "city": "Tye",
      "state": "TX",
      "coordinates": {"longitude": -99.872096, "latitude": 32.461115},
      "route_mile": 195.759,
      "distance_from_route_miles": 0.084,
      "price_per_gallon": "3.12400000",
      "gallons_purchased": "1.968579",
      "fuel_cost": "6.15"
    }],
    "summary": {
      "total_gallons_consumed": "51.968579",
      "total_gallons_purchased": "1.968579",
      "total_fuel_cost": "6.15"
    }
  }
}
```

The result is internally consistent: **519.686 miles ÷ 10 MPG ≈ 51.9686 gallons** consumed. The vehicle starts with 50 gallons, leaving approximately **1.9686 gallons** to purchase; **1.968579 × $3.124 ≈ $6.15**. The API calculates quantities from unrounded route distance.

**Demo / verification:** the deployed API was verified end-to-end with **Dallas, TX → Van Horn, TX**, producing the **$6.15** plan above. Results depend on provider routing and the available station dataset; station database IDs are specific to the loaded dataset.

Coordinates use longitude/latitude order in GeoJSON. Route miles and offsets are numeric with up to three decimal places; duration hours has up to four. Fuel prices, gallons and costs are fixed-point JSON **strings** with eight, six and two decimal places respectively. Rounding occurs only for presentation; totals use unrounded calculations, so adding individually rounded stop costs can differ from the rounded total by a cent. Location labels may be null when unavailable.

### Health endpoint

`GET https://fuel-route-optimizer.up.railway.app/api/health/`

Verified HTTP 200 response:

```json
{"status": "ok"}
```

### Error responses

Validation errors use standard DRF **HTTP 400** responses:

| Input problem | Example response |
|---|---|
| Missing `start` | `{"start":["This field is required."]}` |
| Blank `finish` | `{"finish":["This field may not be blank."]}` |

Cross-field errors use `non_field_errors`; malformed JSON uses `detail`.

Application errors use a consistent envelope, for example HTTP 422:

```json
{"error":{"code":"location_not_found","message":"Could not resolve the finish location."}}
```

The same structure applies to these exact status/code/message combinations:

| HTTP | Code | Message |
|---|---|---|
| 422 | `location_not_found` | `Could not resolve the start location.` or `Could not resolve the finish location.` |
| 422 | `route_not_found` | `No driving route could be found.` |
| 422 | `fuel_route_infeasible` | `A feasible fuel plan could not be found for this route.` |
| 502 | `routing_provider_error` | `Routing provider could not complete the request.` |
| 500 | `routing_configuration_error` | `Routing service is not configured.` |

Provider/network timeouts and malformed provider responses use the provider-failure mapping. API keys, headers and raw provider details are not returned. A long route with insufficient enriched stations returns 422 rather than a partial fuel plan.

## Fuel Optimization Algorithm

The API fixes efficiency at **10 MPG**, range at **500 miles**, and usable capacity at **50 gallons**. The initial tank is full and free for purchase accounting. Only fuel bought during the trip contributes to `total_fuel_cost`; consumed gallons include the initial fuel used.

At each candidate position:

1. If a strictly cheaper station is reachable with a full tank, buy only enough to reach the first such station, accounting for fuel already on board.
2. Otherwise, buy enough to maximize useful remaining range, capped by tank capacity and distance to the destination.

The destination requires no purchase. Zero-purchase candidates are omitted. Same-mile candidates prefer lowest price, then lowest database ID, without deleting source records. The entire route is checked for unreachable gaps; failures raise `FuelRouteInfeasibleError`.

This greedy strategy minimizes purchase cost under known deterministic prices, fixed MPG/capacity, and main-route distance only, with no detour fuel or stop penalties. Purchases shift to cheaper reachable stations while maintaining enough fuel to reach them. Equal-cost plans need not minimize the number of stops.

Sorting costs **O(n log n)**. A monotonic stack computes next-cheaper positions in O(n), followed by a linear fueling pass; memory is O(n). Money and gallons use Decimal arithmetic without premature rounding.

## Station-to-Route Matching

Only persisted complete coordinates participate. The matcher expands the route bounding box by the default **5-mile corridor**, filters with the ORM, and checks nearby segment bounds before projecting each candidate onto its nearest segment. The corridor is a service parameter (`max_distance_miles`), not a request field or environment setting.

Segment lengths and cumulative route distances are precomputed. Projection uses a local plane with longitude scaled by midpoint latitude; Haversine distances measure mileage. Results include distance along the route and distance from it, sorted by route position. Duplicate records remain separate.

This lightweight approach uses no PostGIS and assumes short road segments. It is approximate: geometric proximity does not establish road access to a station.

## Distance Calibration

The matcher measures position along the GeoJSON polyline, whose calculated length can differ from the provider's reported road distance. The planner treats **provider distance as authoritative** and proportionally calibrates matched positions onto that distance axis before optimization. Returned geometry and perpendicular offsets stay unchanged. This is an approximation, not per-segment road-distance reconstruction.

## Caching

Runtime geocoding is cached by trimmed, case-folded query; routing is cached by a normalized **directional** coordinate pair. Reversing start/finish uses a different route key. Only successful normalized results are stored; invalid cache entries and failed provider operations are not reused.

The default backend is Django LocMemCache, with a 24-hour TTL from `ROUTING_CACHE_TTL_SECONDS`. Setting it to `0` bypasses reads and writes. Cache read/write failures fall back to provider results without making routing unavailable.

**Final fuel plans are not cached**: current local station prices and coordinates are read for each long-route request. LocMemCache is process-local and temporary, not a distributed production cache; concurrent cold requests can fetch the same result independently.

## Performance Characteristics

- Cold successful routes use at most three provider operations; warm equivalent requests may use zero.
- Routes at or below 500 miles skip FuelStation queries and matching entirely.
- Long routes use one bounding-box FuelStation SELECT; selected-stop metadata comes from matched results, without N+1 queries.
- Bounding boxes reduce detailed projection work. Worst-case matcher work still grows with candidate stations × route segments; optimizer sorting and linear passes handle thousands of candidates without brute-force purchase enumeration.

## Tests

With the virtual environment active and a valid `DEBUG` value exported:

```bash
python manage.py check
python manage.py makemigrations --check
python manage.py test
```

The verified suite contains **153 tests passing**. Provider HTTP is mocked; no real API key or supplied CSV is needed. CSV tests create temporary files; integration tests use Django's test database.

Coverage includes import validation/atomicity/duplicates, nullable coordinates, offline enrichment and station metadata validation, provider parsing/timeouts/errors, cache normalization/failures/TTL, environment parsing, API validation/errors, local matching, optimizer edge cases, full local route/fuel integration, provider call counts and database query counts.

Optimizer tests simulate tank invariants, exercise exact Decimal arithmetic and range boundaries, and compare costs against **729 integer-grid** and **27 fractional-grid** reference scenarios. Regression cases cover carried initial fuel, same-mile determinism, cheap stations just inside/outside reach, and unnecessary purchases. An 8,000-candidate case exercises scale without flaky wall-clock assertions.

## Assumptions and Tradeoffs

1. The vehicle starts full; initial tank fuel is excluded from trip purchase cost.
2. API MPG and range are fixed at 10 and 500 miles, with a 50-gallon usable tank; there is no reserve or vehicle-specific configuration.
3. Fuel prices are treated as deterministic for the request, without live price refresh.
4. Only stations with complete stored coordinates are eligible; these must come from validated enrichment or independent verification. The default corridor is five miles.
5. Main-route distance drives consumption. Off-route distance is reported, but **station detour mileage and cost are not included**. Additional routing to/from every station is intentionally avoided to keep external call volume bounded.
6. SQLite and approximate local geographic math are intentional take-home choices. A nearby station may still require a substantial road detour.
7. LocMemCache is process-local; restarts/eviction lose cached provider results.
8. Duplicate source rows and OPIS IDs are preserved. Optimization only resolves equivalent same-mile purchase choices internally.

## Possible Production Improvements

Not implemented: PostgreSQL/PostGIS for larger spatial datasets, a shared cache, background/rate-controlled enrichment, provider-aware rate limiting and bounded retries, routed station detours, observability/metrics, containerized deployment and CI/CD.
