# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A personal, time-aware trip planner: an LLM proposes places, the app *disposes* — it
produces a feasibility-checked schedule (opening hours + dwell + road travel time) and
the user stays in control via locks and re-optimizing, not re-prompting. FastAPI +
OR-Tools backend, vanilla-JS + Leaflet frontend, Postgres store, self-hosted routing.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
```

The web app and MCP server require **Postgres** and a **routing engine** (Valhalla by
default). The standalone scripts (`table_matrix.py`, `plan.py`) only need the routing
engine.

## Running

```bash
# --- Database (required by the web app / MCP server) ---
docker compose up -d db                 # Postgres on :5432 (tripplanner/tripplanner)
alembic upgrade head                    # create schema (reads DATABASE_URL, default localhost)
.venv/bin/python scripts/migrate_json_to_db.py   # seed cities + POIs from data/*.json (idempotent)

# --- Web app + map UI at http://localhost:8000 ---
bash scripts/serve.sh                   # legacy single-city default (Kyoto, Valhalla :8002)
bash scripts/serve-city.sh denver       # an onboarded catalog city (points env at its engine)
ROUTING_ENGINE=osrm bash scripts/serve.sh        # use the OSRM fallback engine instead

# Raw uvicorn (what the scripts exec):
uvicorn --app-dir backend app.main:app --port 8000 --reload

# --- Full stack in containers (Postgres + 4 regional Valhalla + api) ---
docker compose up -d --build            # see docker-compose.yml header for the topology

# --- MCP server (stdio; from the backend/ dir because app uses relative imports) ---
cd backend && ../.venv/bin/python -m app.mcp_server
bash scripts/mcp-server.sh [city]       # launcher that also resolves a city's engine/base
```

## Routing engines

```bash
bash scripts/build_valhalla.sh                   # Kyoto/Kansai tileset -> Valhalla :8002
python scripts/onboard_city.py <city> [--rebuild]  # build+serve a catalog city (data/cities.json)
python scripts/build_engine.py <region> [--rebuild] # build a US census-region tileset (data/regions.json)
python scripts/table_matrix.py                   # smoke test: print the travel-time matrix
python scripts/plan.py --days 2                  # smoke test: solve an itinerary from the CLI
```

Onboarding builds are heavy and occasional: build **one region/city at a time** and
watch `docker stats`. See `scripts/build_engine.py` for why a single merged regional
PBF is used instead of multiple per-state extracts (multi-PBF crashes on border tiles,
valhalla/valhalla#3925).

Onboarding **inputs must match the tile footprint.** Geofabrik downloads go through
`curl` (follows redirects + retries) and an extract is **rejected if < 1 MB** — a wrong
region slug 302-redirects to an HTML page that otherwise fails cryptically deep in the
build. A **GTFS feed must stay inside the OSM extract** or tile-building segfaults on
out-of-footprint stops; hence the optional `route_types` trim on `cities.json` feeds
(`onboard_city.py:_trim`) and the preference for single-operator metros / whole-region
extracts.

**Two Valhalla images, on purpose** (don't "unify" them): `build_engine.py` (regional
engines) runs the **official `valhalla/valhalla` 3.7.0** (pinned digest) driven by
`scripts/valhalla_build.sh`, because the **gis-ops image** that `onboard_city.py` uses is
frozen at **3.5.1 and core-dumps building dense-metro tiles**. Transit must be built
`ingest_transit → convert_transit → build_tiles` (convert *before* build_tiles) or the
level-3 transit tiles end up empty.

## Migrations

```bash
alembic revision --autogenerate -m "msg"   # env.py reads metadata from app.models_db
alembic upgrade head
```

There is a **pure-unit `pytest` suite** (`backend/tests/`; dev deps in
`backend/requirements-dev.txt`) and **no linter configured**. The unit tests need no
Postgres / routing engine / network — every I/O seam is monkeypatched — and cover the
solver (TTDP + the four lock types + graceful infeasibility), the matrix cache key +
unreachable-arc repair, grounding, LLM-output parsing, and US-region resolution. The two
smoke scripts above remain the only thing exercising a *live* routing engine.

### Iterating + checking (no build step, no linter)
- Run the unit tests: `pytest` (or `.venv/bin/pytest`) from the repo root (config in
  `pyproject.toml`). They gate two ways: the Docker `test` stage
  (`docker build -f backend/Dockerfile --target test .`) and a local **pre-push** hook
  (install once per clone: `bash scripts/install-hooks.sh`). The default image build /
  `docker compose up -d --build api` does **not** run them — `runtime` is a separate lean
  stage with no pytest.
- Frontend edits are **live** (compose bind-mounts `frontend/` read-only). **Backend
  changes need `docker compose up -d --build api`** — which re-runs `alembic upgrade head`
  and **wipes the in-container `data/matrix_cache.json`** (it's `.dockerignore`d, not
  baked), i.e. a recreate forces a fresh travel-time matrix.
- Syntax-check the frontend (no host `node`): `docker run --rm -v "$PWD/frontend":/f:ro node:20-alpine node --check /f/app.js`.
- Quick backend check without the stack: `PYTHONPATH=backend .venv/bin/python -c "from app import main, store"` (engine/DB are lazy — imports don't connect). Full verification = curl the running api (no browser automation here).

## Architecture

### The grounding invariant (the core design rule)
"AI proposes, the app disposes." The LLM is an *idea generator that returns place names
only* — it must never supply coordinates or opening hours. This is enforced structurally,
not by convention: `llm.py` returns `ProposedPOI` (names + soft metadata, no hours),
`candidates.ground()` geocodes each name via Nominatim to get real coordinates (flagging
anything that doesn't resolve), and hours stay null until OSM/curation fills them. The
same invariant governs all three "add a place" front doors: the web `/suggest` flow, the
MCP `add_poi` tool (which refuses to accept hours at all), and manual add. When touching
this path, preserve it — don't let model output become a source of truth.

### Module seams (don't bypass these)
- **`engine.py`** is the *only* place that picks a routing backend (`ROUTING_ENGINE`:
  valhalla default | osrm). Everything else imports `table_durations`/`base_url` from
  `engine`, never from `valhalla.py`/`osrm.py` directly.
- **`store.py`** is the *only* module that knows how persistence works (Postgres via
  SQLAlchemy). Every other module passes a `city` slug + a `Session` (FastAPI
  `Depends(get_session)`, or `SessionLocal()` outside a request). Pydantic API models
  live in `models.py`; ORM models in `models_db.py`.
- **`main._solve()`** is the shared "solve + shape for the map" core; `_run` (base mode —
  one hotel) and `_run_route` (HYL-68 route mode — per-day start/end anchors) wrap it, behind
  `/plan`, `/replan`, `/plan-route`, and trip create/update/reoptimize. Changes to response
  shape go here. A route whose anchors/POIs cross regions is rejected via
  `places.region_for_points` (one regional engine can't route across regions yet).

### Solver (`solver.py`)
OR-Tools Tourist Trip Design Problem. Days are vehicles; each day has its **own start and
end anchor** (HYL-68) — `plan_trip(pois, matrix, day_anchors, …)` where `day_anchors` is a
`(start_node, end_node)` index pair per day (a single base is the special case where every
anchor shares the base's coords). Matrix layout: anchor/depot nodes `0..A-1`, then POIs;
co-located anchors are kept as distinct nodes (OR-Tools requires unique start/end indices).
Opening hours → time windows, dwell → service time, importance → drop-penalty (so low-value
POIs are shed when a day can't hold everything), `balance` → a count dimension that evens
stops across days. User edits are **locks** turned into hard constraints: `exclude` (drop),
`include` (must-visit, any day), `day` (must be on day N), `pin` (fixed arrival time).
Infeasible locks return a graceful `feasible:false` reason rather than throwing.

### Travel-time matrix (`matrix.py`)
Builds the integer-minute matrix the solver consumes (anchor/depot nodes first, then POIs).
Cached on disk at `data/matrix_cache.json`, keyed by `sha1(coords + profile + base_url)`
so a different region/engine can't collide. For symmetric modes (foot/bicycle) a
one-directional unreachable arc is mirrored from its reverse so one bad OSM edge doesn't
strand a POI. Transit is special: Valhalla's matrix endpoint can't do multimodal, so the
matrix is assembled from per-pair `/route` calls at one representative departure time.

### Multi-city + regional engines
POIs/trips are scoped by `city_slug` in Postgres (composite PK `(city_slug, id)`), so
switching city is a query, not a process swap. Each city row carries a `region` that
selects a regional Valhalla URL (`data/regions.json`, or `VALHALLA_REGION_URL_TEMPLATE`
in compose); an unknown/None region falls back to `VALHALLA_URL` (the legacy/non-US
engine, e.g. Kyoto). Endpoints take a `?city=` param defaulting to `DEFAULT_CITY`.

### Saved trips (`trips` / `trip_stops`)
A solved itinerary can be saved as a **trip**: metadata (title, status, `start_date`) +
solve params + locks + the raw `_run` result, with each visit normalized into a
`trip_stops` row (composite PK `(trip_id, day_index, seq)`). Stops **snapshot**
name/lat/lon and keep `poi_id` as a *soft* reference (no FK), so a trip still renders
after a library POI is edited or deleted. A **route trip** (HYL-68, `mode="route"`) also
persists its per-day anchors (`trip_day_anchors`) + candidate pool (`trip_pois`, soft
`(city_slug, poi_id)` refs spanning towns) and solves through `_run_route`; a base trip
keeps its single `base_*` and solves through `_run`. Because library ids are unique only
*within* a city but a route pool spans towns, `store.load_pois_by_refs` hands the solver
**city-qualified ids** (`"city:id"`, `store.pool_poi_id`) so a slug shared across cities
can't collide — route-mode stops/dropped and locks all speak that qualified id. API: `POST/GET/PUT/PATCH/DELETE /trips`
+ `POST /trips/{id}/reoptimize`; MCP adds `save_trip`/`save_route_trip`/`list_trips`/`get_trip`
plus `add_trip_poi`/`set_trip_pois`/`reoptimize_trip`. The frontend "Saved trips" panel loads
a trip back into the live planner (base or route — restoring controls, anchors, and locks).

### Frontend (`frontend/app.js`)
Single-file vanilla JS + Leaflet, no build step (compose mounts it read-only for live
edits). Edits are staged **locally** as locks and only sent to the server on
Re-optimize — `lastPlan` is re-rendered without a solve in between. All POI/LLM/Nominatim
strings are untrusted; route every interpolated value through `esc()` (or `textContent`)
before it reaches `innerHTML`/popups.

## Known inconsistencies (state of the migration)
- The store moved from per-city JSON files to Postgres, but `README.md` and some scripts
  (notably `scripts/plan.py`) still reference `data/pois.json` and predate the DB. The
  running web app and MCP server use `store.py` (Postgres); treat the DB as authoritative.
- `models_db.MatrixCache` (table `matrix_cache`) is defined and migrated but **unused** —
  the live cache is the `data/matrix_cache.json` file. Wiring it through the DB (which
  would also fix the file cache's read-modify-write race) is an open task.

## Config (env; `.env` is gitignored and auto-sourced by the serve scripts)
- `DATABASE_URL` — Postgres DSN (default `…@localhost:5432/tripplanner`).
- `ROUTING_ENGINE` (valhalla|osrm), `ROUTE_PROFILE` (default mode, foot).
- `VALHALLA_URL`, `VALHALLA_REGION_HOST`/`VALHALLA_REGION_URL_TEMPLATE`,
  `VALHALLA_SNAP_RADIUS`, `VALHALLA_DEPART` (transit departure).
- `LLM_PROVIDER` (openai|anthropic), `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL` — one
  httpx client drives any OpenAI-compatible endpoint (OpenAI/Groq/Together/Ollama) plus
  Anthropic. No key → `/suggest` returns a clear 503; everything else works.
- `DEFAULT_CITY`, `BASE_LAT/BASE_LON/BASE_NAME`, `CITY_LABEL` — the active-city bootstrap
  for the un-parameterized endpoints (set by `serve-city.sh`).
