# Trip Planner

A personal, time-aware trip planner: AI proposes, **you dispose**. The plan is a
real, feasibility-checked schedule (opening hours + dwell + travel time), and
you stay in control by dragging, locking, and re-optimizing — not re-prompting.

Built for myself first, on a free data stack: OpenStreetMap data, self-hosted
**Valhalla** routing (OSRM kept as a fallback), and a Postgres store.

## Layout

```
trip-planner/
├── data/
│   ├── cities.json            # the city catalog (base, port, GTFS feeds, region)
│   ├── regions.json           # US census region → regional Valhalla engine/port
│   ├── pois.<city>.json       # per-city POI seeds (loaded into Postgres by the seed script)
│   └── valhalla/ regions/ osrm/   # routing tilesets, gitignored
├── backend/app/
│   ├── models.py models_db.py # Pydantic API models / SQLAlchemy ORM tables
│   ├── db.py store.py         # Postgres engine+session / the only persistence-aware module
│   ├── engine.py              # routing-backend selector (Valhalla default | OSRM)
│   ├── valhalla.py osrm.py    # concrete routing clients → travel-time matrix (seconds)
│   ├── matrix.py solver.py    # cached minute-matrix / OR-Tools TOPTW scheduler
│   ├── candidates.py llm.py geocode.py   # LLM-proposes-names → geocode-grounds pipeline
│   ├── main.py                # FastAPI: REST API + serves the frontend
│   └── mcp_server.py          # the same grounded tools over MCP
├── alembic/                   # database migrations
├── frontend/                  # vanilla JS + Leaflet map UI (no build step)
└── scripts/                   # build engines, onboard cities, serve, seed the DB
```

**The founding spike (done):** before any UI, the bet that kills indie trip planners had
to be proven — **can we get a reliable travel-time matrix for a handful of POIs, for
free?** — alongside the data-model decision that POIs are a *persistent, id-keyed store
with tags*. That store is now Postgres (the "scale-up" below); the matrix runs on
self-hosted Valhalla.

## Quick start

```bash
cd ~/trip-planner
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
```

### Database (Postgres)

The web app and MCP server keep cities, POIs and trips in Postgres. Start it, create the
schema, and seed the catalog:

```bash
docker compose up -d db                          # Postgres on :5432 (tripplanner/tripplanner)
alembic upgrade head                             # create the schema (reads DATABASE_URL)
.venv/bin/python scripts/migrate_json_to_db.py   # seed cities + per-city POIs (idempotent)
```

The full `docker compose up` stack auto-runs `alembic upgrade head` on the api container
but never seeds — run the seed script once regardless. It loads `data/pois.<city>.json`
per city, so the catalog ships **Boston** and **Denver** POI libraries; Kyoto is a
routing engine only (no seeded POIs).

### Path A — self-hosted Valhalla (default)

The planner routes on a self-hosted **Valhalla**, built from the Kansai extract (a
few hundred MB, not the whole planet). **One** Valhalla instance serves every mode —
walking, driving, … — via a `costing` parameter (no instance-per-mode). Needs Docker
(Docker Desktop → Settings → Resources → WSL Integration, toggle on this distro,
`Apply & Restart`):

```bash
bash scripts/build_valhalla.sh   # fetches the Kansai extract + brings Valhalla up (:8002)
docker compose logs -f valhalla  # watch the one-time tile build (a few minutes)
curl -s http://localhost:8002/status            # sanity-check the engine is serving
```

Pick **walking** or **driving** in the sidebar's Mode control. (Public transit is the
next profile — see the roadmap.)

### Path B — OSRM fallback, or the no-Docker demo

The previous engine, [OSRM](https://github.com/Project-OSRM/osrm-backend), is kept as
a fallback (one instance per profile). Build its graphs and select it with
`ROUTING_ENGINE=osrm`:

```bash
bash scripts/build_osrm.sh                       # foot graph
PROFILE=car bash scripts/build_osrm.sh           # car graph
docker compose -f docker-compose.osrm.yml up -d  # foot -> :5000, car -> :5001
ROUTING_ENGINE=osrm bash scripts/serve.sh        # run the app on OSRM
```

No Docker at all? Point OSRM at the public demo (driving only) for a quick check:

```bash
curl "https://router.project-osrm.org/table/v1/driving/135.7588,34.9858;135.6586,34.9881?annotations=duration"
```

## Adding a city (onboarding pipeline)

Production routing is a **curated catalog of cities**, each pre-built into a Valhalla
bundle (walk / drive / transit) on its own port. To onboard one, add an entry to
`data/cities.json` (OSM extract URL + GTFS feed(s) + base + port) and run:

```bash
python scripts/onboard_city.py denver   # downloads Colorado + RTD, builds tiles, serves :8004
bash scripts/serve-city.sh denver        # run the app against it -> http://localhost:8000
```

Re-running just serves the existing tiles (load ≠ build); `--rebuild` forces a clean
build. Lessons baked in: prefer **single-operator metros** (a GTFS feed that sprawls
past the street extract crashes the build — set `route_types` in the catalog to trim
it), and the build is CPU-capped to avoid out-of-memory. Each city can ship a demo POI
set at `data/pois.<city>.json`, seeded into Postgres by `scripts/migrate_json_to_db.py`.
Onboarding a city doesn't disturb the default app (`scripts/serve.sh`).

## What success looks like (the spike)

The original step-1 check (via `scripts/table_matrix.py` — now legacy, see the note
under CLI demo): a printed N×N matrix of travel minutes between the 6 sample Kyoto POIs.
**Arashiyama** (far west) and **Fushimi Inari** (south) should read noticeably farther
from the central cluster (Gion / Nishiki / Kiyomizu). If that holds, the data path is
proven.

## CLI demo (step 2)

> **Legacy / pre-Postgres.** `scripts/plan.py` and `scripts/table_matrix.py` are the
> original spike/demo scripts: they call the old `load_pois(path)` and read the JSON
> sample store, so they need a small refresh to run against the Postgres store. The live
> ways to plan are the web app (below) and the REST/MCP APIs.

```bash
source .venv/bin/activate
# routes on the self-hosted Valhalla engine (see Path A);
# fall back to OSRM with:  export ROUTING_ENGINE=osrm

python scripts/plan.py --days 2                 # balanced multi-day itinerary
python scripts/plan.py --days 1 --end 14:00     # tight day -> importance-aware drops
```

Flags: `--days`, `--start/--end` (day window), `--base-lat/--base-lon` (hotel),
`--balance` (day-evenness weight, 0 = off). The travel matrix is cached under
`data/` after the first run, so re-solving is offline.

## Web UI (steps 3–4)

```bash
bash scripts/serve-city.sh denver   # a catalog city on its own engine -> http://localhost:8000
bash scripts/serve.sh               # bare default launcher (uses DEFAULT_CITY, default denver)
```

The sidebar's **city picker** switches among the seeded catalog cities; every POI/plan
call is scoped to the selected one. Open the page, tweak days / window / base / mode in
the sidebar, and hit **Plan**.
Markers are numbered per day and colored by day; dashed lines are each day's
route; dropped POIs show as grey dots. (Legs are straight lines for now — real
road polylines via OSRM `/route` are an easy later upgrade.)

**Control (step 4):** on each stop you can move it to another day, 🔒 lock it
there, or ✕ remove it; ＋ pulls a dropped POI back in as must-visit. Every edit
becomes a hard constraint and re-optimizes around your choices (`POST /replan`).
A fixed arrival time (a "pin", e.g. a reservation) is supported by the API. If a
lock can't fit, you get a clear notice and the last good plan stays put.

**Add places (step 4.5):** use the search box at the top of the sidebar, or click
anywhere on the map, to drop a pin and add a place to your **library**. A click's
name is prefilled by reverse-geocoding; drag the 📍 pin to fine-tune. Set
importance / dwell / hours / tags, then **Add to library** — it persists to the Postgres
POI library (scoped to the selected city) and appears as a hollow blue marker. Adding doesn't re-solve (that
would be sluggish on every edit); hit **Re-optimize** to fold new places into the
itinerary. Geocoding is OpenStreetMap **Nominatim**, proxied by the backend
(`/geocode`, `/reverse`). Remove a place from its map-marker popup.

## AI suggestions (step 5)

Describe a trip in the sidebar — *"4 days in Kyoto in winter with kids, love
nature and food, hate crowds"* — and hit **✨ Suggest places**. The planner asks an
LLM for ideas, but the model only ever proposes **names**: every suggestion is
**geocoded** (Nominatim) for real coordinates, opening hours are **never** taken
from the model, and anything that doesn't resolve is flagged rather than trusted.
Review the candidates (orange pins), tweak importance/dwell, **Add selected to
library**, then **Re-optimize** to fold them into the schedule.

Bring your own key — set a provider in a gitignored `.env` (auto-loaded by
`scripts/serve.sh`):

| Provider | `.env` |
|----------|--------|
| **Groq** (free tier) | `LLM_PROVIDER=openai`<br>`LLM_BASE_URL=https://api.groq.com/openai/v1`<br>`LLM_MODEL=llama-3.3-70b-versatile`<br>`LLM_API_KEY=gsk_…` |
| **OpenAI** | `LLM_PROVIDER=openai`<br>`LLM_MODEL=gpt-4o-mini`<br>`LLM_API_KEY=sk-…` |
| **Anthropic (Claude)** | `LLM_PROVIDER=anthropic`<br>`LLM_MODEL=claude-haiku-4-5-20251001`<br>`LLM_API_KEY=sk-ant-…` |
| **Ollama** (local, free, offline) | `LLM_PROVIDER=openai`<br>`LLM_BASE_URL=http://localhost:11434/v1`<br>`LLM_MODEL=llama3.1` |

Candidate generation is a few hundred tokens a handful of times per trip — so on a
small model it's fractions of a cent (or free on Groq/Ollama). Without a key the
**Suggest** button just returns a clear "configure a provider" message; everything
else works unchanged.

## Drive it from your own AI (MCP)

The planner is also an **MCP server**, so any MCP client (Claude Desktop/Code,
Cursor) can drive it through the same grounded tools — *"any AI proposes, the app
disposes."* Tools: `search_places` (ground a name → coordinates), `list_pois`,
`add_poi` (opening hours are **not** accepted — the agent can't fabricate them),
`delete_poi`, and `plan_trip` (feasibility-checked itinerary, walking or driving,
honoring locks). Prereqs: **Postgres up and seeded** (the POI library + trip store), the
routing engine running (Valhalla, Path A) for `plan_trip`, and network for
`search_places`. Point your client at it — Claude
Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "trip-planner": {
      "command": "/home/hylin/trip-planner/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/home/hylin/trip-planner/backend"
    }
  }
}
```

Then ask your agent to *"find three quiet temples in Kyoto, add them, and plan a
2-day walking trip"* — it grounds each via `search_places`, stores them, and gets a
real schedule back.

## Roadmap

1. **Data-path spike** ✓ — OSRM `/table` returns a road-routed matrix.
2. **Solver core** ✓ — OR-Tools (TOPTW): days-as-vehicles, hours → time windows,
   dwell → service time, importance → drop-penalty, count-based day balancing.
3. **Read-only UI** ✓ — map + day timeline (`scripts/serve.sh`), per-day routes, dropped flags.
4. **Control** ✓ — move-to-day / lock / remove / must-visit + pin (API), auto re-optimize around locks. Drag-and-drop is the React upgrade.
   - **4.5 · Add a POI** ✓ — search (Nominatim) or click the map to add places to a persistent library (now Postgres, scoped per city); they become solver candidates on the next Re-optimize. The store's write-path is the seam Step 6 grows from.
5. **LLM candidate generation** ✓ — a trip brief → LLM-proposed names, **geocoded** for real coords (hours never from the model), staged for review, then accepted into the library. Bring-your-own-key (hosted or local Ollama). Also drivable from any MCP client (Claude Desktop/Code) via `app.mcp_server`.
6. *(later)* **Personal POI library** — stash, tag, filter your own finds across seasons.

**Scale-up (in progress):** the per-city JSON store became **Postgres** (cities / POIs /
trips, scoped by city slug), a **multi-city catalog** with an onboarding pipeline
(`data/cities.json`), and **regional Valhalla engines** per US census region
(`data/regions.json`) — all composable on one host (`docker compose up`).
