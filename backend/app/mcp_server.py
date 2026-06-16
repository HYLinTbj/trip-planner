"""MCP server — drive the trip planner from any MCP client (Claude Desktop/Code, …).

"AI proposes, the app disposes" over MCP: the external agent proposes place *names*;
these tools ground and dispose. `search_places` resolves a name to real coordinates
(our Nominatim proxy); `add_poi` stores a grounded place and deliberately accepts NO
opening hours — the agent can't fabricate them, they come from OSM/curation; and
`plan_trip` returns a feasibility-checked itinerary from the same solver + routing
engine the web app uses. Same grounding, different front door.

Run over stdio:   python -m app.mcp_server      (from the backend/ directory)
"""

from mcp.server.fastmcp import FastMCP

from . import geocode, main, places, store
from .db import SessionLocal
from .models import Lock, POICreate

mcp = FastMCP("trip-planner")

# One server serves every city: each tool takes a per-call `city` argument that
# scopes the POI library, saved plans, and (via main._run) which regional routing
# engine is queried. `CITY` is just the default (the launcher's DEFAULT_CITY), so
# existing single-city callers keep working unchanged.
CITY = main.DEFAULT_CITY


@mcp.tool()
def search_places(query: str, limit: int = 5) -> list[dict]:
    """Resolve a place name to real coordinates via OpenStreetMap (Nominatim).
    Use this to GROUND a place you're proposing before adding it — never invent
    coordinates yourself. Returns up to `limit` candidates: {name, display_name,
    lat, lon}."""
    return geocode.search(query, limit=limit)


@mcp.tool()
def list_places() -> list[dict]:
    """List every place you can plan in (slug, label, base, region, user_created) —
    curated catalog cities plus bases you've created with create_place. Pass a slug as
    the `city` argument to the other tools."""
    with SessionLocal() as db:
        return [main._city_out(c) for c in store.list_cities(db)]


@mcp.tool()
def create_place(name: str | None = None, lat: float | None = None,
                 lon: float | None = None, query: str | None = None) -> dict:
    """Set any place as a trip base so you can plan there. GROUND it first — pass
    `lat`/`lon` from `search_places`, OR pass a `query` and this tool geocodes it
    (never invent coordinates). Resolves the place's US region and creates (or reuses)
    a place with its own POI library; returns {slug, label, base, region}. Then call
    add_poi(city=slug) and plan_trip(city=slug). Errors if the place is outside the
    contiguous-US coverage (Alaska, Hawaii, and non-US aren't routable yet)."""
    try:
        if lat is None or lon is None:
            if not query:
                return {"error": "Provide lat/lon (from search_places) or a query to geocode."}
            hits = geocode.search(query, limit=1)
            if not hits:
                return {"error": f"Couldn't geocode {query!r}."}
            name, lat, lon = name or hits[0]["name"], hits[0]["lat"], hits[0]["lon"]
        if not name:
            return {"error": "A place name is required."}
        region = places.region_for_point(lat, lon)
        if region is None:
            return {"error": "Outside supported coverage — contiguous US only "
                             "(Alaska, Hawaii, and non-US aren't routable yet)."}
        with SessionLocal() as db:
            c = store.add_city(name, lat, lon, region, db)
            return {"slug": c.slug, "label": c.label, "region": c.region,
                    "base": {"lat": c.base_lat, "lon": c.base_lon, "name": c.base_name}}
    except Exception as exc:   # geocoder/engine unreachable, etc.
        return {"error": str(exc)}


@mcp.tool()
def list_pois(city: str = CITY) -> list[dict]:
    """List every POI in the user's library (planned or not), with id/name/coords/
    importance/tags."""
    with SessionLocal() as db:
        return [p.model_dump(mode="json") for p in store.load_pois(city, db).values()]


@mcp.tool()
def add_poi(name: str, lat: float, lon: float, importance: float = 0.5,
            dwell_min: int = 60, tags: list[str] | None = None,
            notes: str | None = None, city: str = CITY) -> dict:
    """Add a grounded place to the library. Get `lat`/`lon` from `search_places`
    first. Opening hours are intentionally NOT accepted here — they must come from
    real data, not a model. `importance` (0–1) governs drop order when a day is
    tight; `dwell_min` is the typical visit length in minutes. Returns the stored POI."""
    with SessionLocal() as db:
        poi = store.add_poi(city, POICreate(
            name=name, lat=lat, lon=lon, importance=importance,
            dwell_min=dwell_min, tags=tags or [], notes=notes), db)
    return poi.model_dump(mode="json")


@mcp.tool()
def delete_poi(poi_id: str, city: str = CITY) -> dict:
    """Remove a POI from `city`'s library by its id."""
    with SessionLocal() as db:
        return {"deleted": poi_id, "existed": store.delete_poi(city, poi_id, db)}


@mcp.tool()
def plan_trip(days: int = 2, start: str = "09:00", end: str = "19:00",
              base_lat: float | None = None, base_lon: float | None = None,
              balance: int = 5, profile: str = "foot",
              locks: list[dict] | None = None, city: str = CITY) -> dict:
    """Build a feasibility-checked itinerary over the library (respecting opening
    hours, dwell time, and real travel time). `profile` is "foot" or "car". `locks`
    are the user's dispositions — each {poi_id, type, day?, time?} with
    type ∈ exclude | include | day | pin — pass them to re-optimize around fixed
    choices. Returns days→stops (with arrival/departure times) plus dropped POIs;
    check `feasible`/`reason` for infeasible locks."""
    lock_objs = [Lock(**lk) for lk in (locks or [])]
    try:
        with SessionLocal() as db:
            if base_lat is None or base_lon is None:   # default to the city's base
                c = store.get_city(city, db)
                if c:
                    base_lat = c.base_lat if base_lat is None else base_lat
                    base_lon = c.base_lon if base_lon is None else base_lon
            return main._run(city, db, days, start, end, base_lat, base_lon,
                             balance, 3, profile, lock_objs)
    except Exception as exc:  # routing engine unreachable, bad lock, etc.
        return {"error": str(exc)}


@mcp.tool()
def plan_route(day_anchors: list[dict], poi_refs: list[dict] | None = None,
               start: str = "09:00", end: str = "19:00", balance: int = 5,
               profile: str = "car", locks: list[dict] | None = None) -> dict:
    """Plan a multi-leg trip (HYL-68): each day has its OWN start and end location, and the
    solver picks the best POIs for each leg — for road trips / changing hotels / airport
    starts, no single base. GROUND anchors first (coords from search_places or create_place;
    never invent them). `day_anchors`: one per day, each {start_lat, start_lon, start_name?,
    end_lat, end_lon, end_name?}. `poi_refs`: the candidate pool as {city, id} refs (from
    list_pois/add_poi across the towns on the route). Returns per-day legs (start → stops →
    end) + dropped POIs. Errors if the route crosses regions (one regional engine can't route
    it yet) — keep anchors/POIs within one US region for now."""
    try:
        req = main.RoutePlanRequest(
            day_anchors=[main.DayAnchor(**a) for a in day_anchors],
            poi_refs=[main.POIRef(**r) for r in (poi_refs or [])],
            start=start, end=end, balance=balance, profile=profile,
            locks=[Lock(**lk) for lk in (locks or [])],
        )
        with SessionLocal() as db:
            return main.plan_route(req, db)
    except Exception as exc:  # region guard (422), engine unreachable, bad anchor, etc.
        return {"error": str(exc)}


@mcp.tool()
def save_route_trip(title: str, day_anchors: list[dict], poi_refs: list[dict] | None = None,
                    start: str = "09:00", end: str = "19:00", balance: int = 5,
                    profile: str = "car", locks: list[dict] | None = None,
                    start_date: str | None = None, notes: str | None = None,
                    city: str = CITY) -> dict:
    """Solve and SAVE a road trip (HYL-68): per-day start/end `day_anchors` over a (city,id)
    `poi_refs` pool — like plan_route but persisted. Returns the saved trip (with its id).
    Fails without saving if infeasible or the route crosses regions."""
    try:
        req = main.TripCreate(
            city=city, title=title, mode="route",
            day_anchors=[main.DayAnchor(**a) for a in day_anchors],
            poi_refs=[main.POIRef(**r) for r in (poi_refs or [])],
            start=start, end=end, balance=balance, profile=profile,
            locks=[Lock(**lk) for lk in (locks or [])], start_date=start_date, notes=notes,
        )
        with SessionLocal() as db:
            return main.create_trip(req, db)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def add_trip_poi(trip_id: int, poi_id: str, city: str = CITY) -> dict:
    """Add one library POI to a saved trip's candidate pool (idempotent). Call
    reoptimize_trip afterwards to fold it into the route."""
    try:
        with SessionLocal() as db:
            if store.get_trip(trip_id, db) is None:
                return {"error": f"No trip {trip_id}"}
            store.add_trip_poi(trip_id, city, poi_id, db)
            db.commit()
            return {"trip_id": trip_id, "added": {"city": city, "id": poi_id}}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def set_trip_pois(trip_id: int, poi_refs: list[dict]) -> dict:
    """Replace a saved trip's whole candidate pool with these {city, id} refs. Call
    reoptimize_trip afterwards to re-solve around the new pool."""
    try:
        with SessionLocal() as db:
            if store.get_trip(trip_id, db) is None:
                return {"error": f"No trip {trip_id}"}
            store.set_trip_pois(trip_id, [(r["city"], r["id"]) for r in poi_refs], db)
            db.commit()
            return {"trip_id": trip_id, "pool_size": len(poi_refs)}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def reoptimize_trip(trip_id: int) -> dict:
    """Re-solve a saved trip from its stored parameters + locks (route trips re-use their
    anchors + pool) and replace its itinerary. Use after add_trip_poi / set_trip_pois, or
    when the POI library changed."""
    try:
        with SessionLocal() as db:
            return main.reoptimize_trip(trip_id, db=db)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def save_trip(title: str, days: int = 2, start: str = "09:00", end: str = "19:00",
              balance: int = 5, profile: str = "foot", locks: list[dict] | None = None,
              start_date: str | None = None, notes: str | None = None,
              city: str = CITY) -> dict:
    """Solve and SAVE a named trip over `city`'s library so the user can review it
    later. `start_date` (YYYY-MM-DD) anchors day 1 on the calendar. The server
    solves with the same engine as plan_trip and persists the itinerary; returns
    the saved trip (with its `id`). Fails without saving if the solve is infeasible."""
    try:
        with SessionLocal() as db:
            req = main.TripCreate(city=city, title=title, days=days, start=start, end=end,
                                  balance=balance, profile=profile, locks=locks or [],
                                  start_date=start_date, notes=notes)
            return main.create_trip(req, db)
    except Exception as exc:  # infeasible solve (422), engine down, bad lock, …
        return {"error": str(exc)}


@mcp.tool()
def list_trips(city: str = CITY) -> list[dict]:
    """List `city`'s saved trips (id, title, status, start_date, days, stop count)."""
    with SessionLocal() as db:
        trips = store.list_trips(city, db)
        counts = store.trip_stop_counts(db, [t.id for t in trips])
        return [main._trip_summary(t, counts.get(t.id, 0)) for t in trips]


@mcp.tool()
def get_trip(trip_id: int) -> dict:
    """Retrieve a complete saved trip: per-day dated stops in visit order with
    arrival/departure times, plus dropped POIs and the locks it was solved with."""
    with SessionLocal() as db:
        t = store.get_trip(trip_id, db)
        if t is None:
            return {"error": f"No trip {trip_id}"}
        return main._trip_out(t, store.trip_stops(trip_id, db))


if __name__ == "__main__":
    mcp.run()
