"""FastAPI app: travel-time matrix, itinerary planning, lock-aware re-planning,
and the static web UI.

Scale-up phase 1: POIs/plans live in Postgres (see store.py), scoped by `city`.
Endpoints take a `city` query param (defaulting to DEFAULT_CITY) so the same
process serves every city — the phase 3 picker will pass it explicitly.

Run:  bash scripts/serve.sh   (→ http://localhost:8000)
"""

import os
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import places, store
from .candidates import ground
from .db import get_session
from .engine import DEFAULT_PROFILE, base_url as engine_base_url, table_durations, to_minutes
from .geocode import reverse as geocode_reverse, search as geocode_search
from .llm import LLMNotConfigured, propose_candidates
from .matrix import get_matrix_min
from .models import Lock, POICreate, SuggestRequest
from .solver import hhmm_to_min, min_to_hhmm, plan_trip

app = FastAPI(title="Trip Planner")

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "data" / "matrix_cache.json"
FRONTEND = ROOT / "frontend"

# Active-city base — kept (env, set by serve-city.sh) for the frontend bootstrap
# until the phase-3 city picker reads it from the cities registry instead.
BASE_LAT = float(os.environ.get("BASE_LAT", 34.9858))
BASE_LON = float(os.environ.get("BASE_LON", 135.7588))
BASE_NAME = os.environ.get("BASE_NAME", "Kyoto Station")
CITY_LABEL = os.environ.get("CITY_LABEL", "")
# City the un-parameterized endpoints fall back to (phase-3 UI passes ?city=).
DEFAULT_CITY = os.environ.get("DEFAULT_CITY", "denver")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/config")
def config() -> dict:
    """UI bootstrap: the served city's base, so the sidebar + map default to the
    right place instead of the Kyoto fallback."""
    return {"base": {"lat": BASE_LAT, "lon": BASE_LON, "name": BASE_NAME},
            "city": CITY_LABEL, "default_city": DEFAULT_CITY}


def _city_out(c) -> dict:
    """A city/place as the picker consumes it (base + transit + region/origin flags)."""
    return {
        "slug": c.slug, "label": c.label,
        "base": {"lat": c.base_lat, "lon": c.base_lon, "name": c.base_name},
        "has_transit": c.has_transit, "transit_operator": c.transit_operator,
        "default_depart": c.default_depart,
        "region": c.region, "user_created": c.user_created,
    }


@app.get("/cities")
def cities(db: Session = Depends(get_session)) -> dict:
    """The place registry the picker lists — curated catalog cities plus the user's
    own free-base places (user_created)."""
    return {"cities": [_city_out(c) for c in store.list_cities(db)]}


class PlaceCreate(BaseModel):
    """A geocoded place (from /geocode) to set as a trip base."""
    name: str
    lat: float
    lon: float


@app.post("/cities", status_code=201)
def create_city(body: PlaceCreate, db: Session = Depends(get_session)) -> dict:
    """Set any place as a trip base: resolve its US region from the coordinates, then
    create (or reuse) a lightweight place with its own POI library + trips. 422 when
    the point isn't in supported US coverage (the engines cover the contiguous US)."""
    try:
        region = places.region_for_point(body.lat, body.lon)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Geocoder unreachable: {exc}") from exc
    if region is None:
        raise HTTPException(
            status_code=422,
            detail=("Outside supported coverage — routing covers the contiguous US "
                    "only (Alaska, Hawaii, and non-US places aren't routable yet)."),
        )
    return _city_out(store.add_city(body.name, body.lat, body.lon, region, db))


@app.delete("/cities/{slug}")
def remove_city(slug: str, db: Session = Depends(get_session)) -> dict:
    """Delete a user-created place (its POIs/trips cascade). Catalog cities are
    protected so the seed registry can't be deleted from the UI."""
    c = store.get_city(slug, db)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Unknown place '{slug}'")
    if not c.user_created:
        raise HTTPException(status_code=403, detail=f"'{slug}' is a catalog city — not removable")
    store.delete_city(slug, db)
    return {"ok": True, "deleted": slug}


def _engine_url(city: str, db: Session) -> str:
    """The regional Valhalla engine URL for a city, resolved from its census region."""
    c = store.get_city(city, db)
    return engine_base_url(region=getattr(c, "region", None))


@app.get("/matrix")
def matrix(city: str = DEFAULT_CITY, profile: str | None = None,
           db: Session = Depends(get_session)) -> dict:
    """Travel-time matrix (minutes) between a city's POIs."""
    pois = store.load_pois(city, db)
    ids = list(pois.keys())
    coords = [(pois[i].lat, pois[i].lon) for i in ids]
    try:
        kwargs = {"profile": profile} if profile else {}
        durations = table_durations(coords, base_url=_engine_url(city, db), **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not reach the routing engine: {exc}. Start it with "
                "`docker compose up -d` (see README)."
            ),
        ) from exc
    return {
        "ids": ids,
        "names": [pois[i].name for i in ids],
        "durations_min": to_minutes(durations),
    }


def _run(city, db, days, start, end, base_lat, base_lon, balance, time_limit, profile, locks) -> dict:
    """Solve and shape the response with coordinates + HH:MM times for the map."""
    pois = list(store.load_pois(city, db).values())
    by_id = {p.id: p for p in pois}
    coords = [(base_lat, base_lon)] + [(p.lat, p.lon) for p in pois]
    try:
        matrix = get_matrix_min(coords, profile=profile or DEFAULT_PROFILE,
                                cache_path=str(CACHE_PATH), base_url=_engine_url(city, db))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Routing engine unreachable: {exc}") from exc

    ds, de = hhmm_to_min(start), hhmm_to_min(end)
    res = plan_trip(pois, matrix, days, ds, de, time_limit, balance=balance, locks=locks)

    def stop_out(s: dict) -> dict:
        p = by_id[s["poi_id"]]
        return {
            **s, "lat": p.lat, "lon": p.lon,
            "arrival_hhmm": min_to_hhmm(s["arrival"]),
            "departure_hhmm": min_to_hhmm(s["departure"]),
        }

    return {
        "feasible": res.get("feasible", True),
        "reason": res.get("reason"),
        "base": {"lat": base_lat, "lon": base_lon},
        "day_start": min_to_hhmm(ds),
        "day_end": min_to_hhmm(de),
        "days": [
            {
                "stops": [stop_out(s) for s in d["stops"]],
                "return_hhmm": min_to_hhmm(d["return_min"]),
                "travel_min": d["travel_min"],
            }
            for d in res["days"]
        ],
        "dropped": [
            {"poi_id": pid, "name": by_id[pid].name, "lat": by_id[pid].lat,
             "lon": by_id[pid].lon, "importance": by_id[pid].importance}
            for pid in (res["dropped"] + res["auto_dropped"])
        ],
        "total_travel_min": res["total_travel_min"],
    }


@app.get("/plan")
def plan(
    city: str = DEFAULT_CITY,
    days: int = 2, start: str = "09:00", end: str = "19:00",
    base_lat: float = BASE_LAT, base_lon: float = BASE_LON,
    balance: int = 5, time_limit: int = 3, profile: str | None = None,
    db: Session = Depends(get_session),
) -> dict:
    """A fresh itinerary, no locks."""
    return _run(city, db, days, start, end, base_lat, base_lon, balance, time_limit, profile, [])


class ReplanRequest(BaseModel):
    city: str = DEFAULT_CITY
    days: int = 2
    start: str = "09:00"
    end: str = "19:00"
    base_lat: float = BASE_LAT
    base_lon: float = BASE_LON
    balance: int = 5
    time_limit: int = 3
    profile: str | None = None
    locks: list[Lock] = []


@app.post("/replan")
def replan(req: ReplanRequest, db: Session = Depends(get_session)) -> dict:
    """Re-optimize honoring the user's locks — the 'you dispose' step."""
    return _run(req.city, db, req.days, req.start, req.end, req.base_lat, req.base_lon,
                req.balance, req.time_limit, req.profile, req.locks)


# --- POI library: list / create / delete (the write path behind "add a POI") --

@app.get("/pois")
def list_pois(city: str = DEFAULT_CITY, db: Session = Depends(get_session)) -> dict:
    """Every POI in the city's library, planned or not — the map's 'known places'."""
    pois = store.load_pois(city, db)
    return {"pois": [p.model_dump(mode="json") for p in pois.values()]}


@app.post("/pois", status_code=201)
def create_poi(body: POICreate, city: str = DEFAULT_CITY,
               db: Session = Depends(get_session)) -> dict:
    """Add a POI. It becomes a solver candidate on the next solve (governed by
    its importance); we deliberately don't auto-replan here."""
    return store.add_poi(city, body, db).model_dump(mode="json")


@app.delete("/pois/{poi_id}")
def remove_poi(poi_id: str, city: str = DEFAULT_CITY,
               db: Session = Depends(get_session)) -> dict:
    if not store.delete_poi(city, poi_id, db):
        raise HTTPException(status_code=404, detail=f"No POI with id {poi_id!r} in {city!r}")
    return {"ok": True, "deleted": poi_id}


# --- Geocoding: Nominatim proxy for search-to-add and click-to-add ------------

@app.get("/geocode")
def geocode(q: str, limit: int = 5) -> dict:
    """Forward geocode for the search box."""
    q = q.strip()
    if not q:
        return {"results": []}
    try:
        return {"results": geocode_search(q, limit=limit)}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Geocoder unreachable: {exc}") from exc


@app.get("/reverse")
def reverse(lat: float, lon: float) -> dict:
    """Reverse geocode a clicked point to prefill the add form's name."""
    try:
        return geocode_reverse(lat, lon)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Geocoder unreachable: {exc}") from exc


# --- Step 5: LLM candidate generation (propose names → geocode → stage) --------

@app.post("/suggest")
def suggest(req: SuggestRequest, city: str = DEFAULT_CITY,
            db: Session = Depends(get_session)) -> dict:
    """A trip brief → grounded candidate POIs. The model proposes names only; we
    geocode them (hours are never taken from the model). Returns staged candidates
    for the user to accept — nothing is added to the library or solved here."""
    existing = store.load_pois(city, db)
    try:
        proposed = propose_candidates(
            req.prompt, area=req.area, count=req.count,
            existing_names=[p.name for p in existing.values()],
        )
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {exc}") from exc
    candidates = ground(proposed, req.area, existing)
    return {"candidates": [c.model_dump(mode="json") for c in candidates]}


@app.post("/pois/batch", status_code=201)
def create_pois(bodies: list[POICreate], city: str = DEFAULT_CITY,
                db: Session = Depends(get_session)) -> dict:
    """Accept a batch of candidates into the library (reuses the add_poi write-path)."""
    created = [store.add_poi(city, b, db) for b in bodies]
    return {"created": [p.model_dump(mode="json") for p in created]}


# --- Trips (save / review / re-plan complete itineraries) ---------------------

class TripCreate(BaseModel):
    city: str = DEFAULT_CITY
    title: str
    status: str = "draft"              # draft | upcoming | completed (convention)
    notes: str | None = None
    start_date: date | None = None     # calendar anchor; day i = start_date + i
    days: int = 2
    start: str = "09:00"
    end: str = "19:00"
    base_lat: float | None = None      # default: the city's base
    base_lon: float | None = None
    balance: int = 5
    profile: str = "foot"
    time_limit: int = 3                # saved trips deserve a better solve than the UI's 1s
    locks: list[Lock] = []
    result: dict | None = None         # omit -> the server solves (authoritative)


class TripPatch(BaseModel):
    title: str | None = None
    status: str | None = None
    notes: str | None = None
    start_date: date | None = None


def _trip_summary(t, stop_count: int) -> dict:
    return {
        "id": t.id, "city": t.city_slug, "title": t.title, "status": t.status,
        "start_date": t.start_date.isoformat() if t.start_date else None,
        "num_days": t.num_days, "profile": t.profile, "stops": stop_count,
        "total_travel_min": t.total_travel_min, "feasible": t.feasible,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _trip_out(t, stops) -> dict:
    """The complete trip: metadata + per-day dated, ordered stops, with dropped/locks
    read from the raw result snapshot."""
    result = t.result or {}
    res_days = result.get("days", [])
    days = []
    for di in range(t.num_days):
        day_stops = [s for s in stops if s.day_index == di]   # already itinerary-ordered
        days.append({
            "day_index": di,
            "date": (t.start_date + timedelta(days=di)).isoformat() if t.start_date else None,
            "stops": [{
                "poi_id": s.poi_id, "name": s.name, "lat": s.lat, "lon": s.lon,
                "dwell": s.dwell_min, "travel_in": s.travel_in_min,
                "arrival": s.arrival_min, "departure": s.departure_min,
                "arrival_hhmm": min_to_hhmm(s.arrival_min),
                "departure_hhmm": min_to_hhmm(s.departure_min),
            } for s in day_stops],
            "travel_min": res_days[di].get("travel_min") if di < len(res_days) else None,
            "return_hhmm": res_days[di].get("return_hhmm") if di < len(res_days) else None,
        })
    return {
        **_trip_summary(t, sum(len(d["stops"]) for d in days)),
        "notes": t.notes,
        "day_start": min_to_hhmm(t.day_start_min), "day_end": min_to_hhmm(t.day_end_min),
        "balance": t.balance,
        "base": {"lat": t.base_lat, "lon": t.base_lon},
        "days": days,
        "dropped": result.get("dropped", []),
        "locks": t.locks or [],
    }


@app.post("/trips", status_code=201)
def create_trip(req: TripCreate, db: Session = Depends(get_session)) -> dict:
    base_lat, base_lon = req.base_lat, req.base_lon
    if base_lat is None or base_lon is None:
        c = store.get_city(req.city, db)
        if c is None:
            raise HTTPException(status_code=404, detail=f"Unknown city '{req.city}'")
        base_lat = c.base_lat if base_lat is None else base_lat
        base_lon = c.base_lon if base_lon is None else base_lon
    result = req.result
    if result is None:   # solve now — the server's plan is the source of truth
        result = _run(req.city, db, req.days, req.start, req.end, base_lat, base_lon,
                      req.balance, req.time_limit, req.profile, req.locks)
        if not result.get("feasible", True):
            raise HTTPException(status_code=422,
                                detail=f"Solve infeasible — nothing saved: {result.get('reason')}")
    meta = dict(title=req.title, status=req.status, notes=req.notes, start_date=req.start_date,
                num_days=req.days, day_start_min=hhmm_to_min(req.start),
                day_end_min=hhmm_to_min(req.end), profile=req.profile, balance=req.balance,
                base_lat=base_lat, base_lon=base_lon)
    trip = store.save_trip(req.city, meta, [lk.model_dump() for lk in req.locks], result, db)
    return _trip_out(trip, store.trip_stops(trip.id, db))


@app.get("/trips")
def list_trips(city: str = DEFAULT_CITY, status: str | None = None,
               db: Session = Depends(get_session)) -> dict:
    trips = store.list_trips(city, db, status)
    counts = store.trip_stop_counts(db, [t.id for t in trips])
    return {"trips": [_trip_summary(t, counts.get(t.id, 0)) for t in trips]}


@app.get("/trips/{trip_id}")
def get_trip(trip_id: int, db: Session = Depends(get_session)) -> dict:
    t = store.get_trip(trip_id, db)
    if t is None:
        raise HTTPException(status_code=404, detail=f"No trip {trip_id}")
    return _trip_out(t, store.trip_stops(trip_id, db))


@app.patch("/trips/{trip_id}")
def patch_trip(trip_id: int, req: TripPatch, db: Session = Depends(get_session)) -> dict:
    t = store.update_trip_meta(trip_id, req.model_dump(exclude_unset=True), db)
    if t is None:
        raise HTTPException(status_code=404, detail=f"No trip {trip_id}")
    return _trip_out(t, store.trip_stops(trip_id, db))


@app.put("/trips/{trip_id}")
def update_trip(trip_id: int, req: TripCreate, db: Session = Depends(get_session)) -> dict:
    """Replace a saved trip's itinerary from the current session (params + locks +
    optional result). Like POST but in place — used by the UI's 'Save' on a loaded trip."""
    t = store.get_trip(trip_id, db)
    if t is None:
        raise HTTPException(status_code=404, detail=f"No trip {trip_id}")
    base_lat = t.base_lat if req.base_lat is None else req.base_lat
    base_lon = t.base_lon if req.base_lon is None else req.base_lon
    result = req.result
    if result is None:
        result = _run(t.city_slug, db, req.days, req.start, req.end, base_lat, base_lon,
                      req.balance, req.time_limit, req.profile, req.locks)
        if not result.get("feasible", True):
            raise HTTPException(status_code=422,
                                detail=f"Solve infeasible — trip unchanged: {result.get('reason')}")
    meta = dict(title=req.title, status=req.status, notes=req.notes, start_date=req.start_date,
                num_days=req.days, day_start_min=hhmm_to_min(req.start),
                day_end_min=hhmm_to_min(req.end), profile=req.profile, balance=req.balance,
                base_lat=base_lat, base_lon=base_lon)
    store.update_trip(t, meta, [lk.model_dump() for lk in req.locks], result, db)
    return _trip_out(t, store.trip_stops(trip_id, db))


@app.post("/trips/{trip_id}/reoptimize")
def reoptimize_trip(trip_id: int, time_limit: int = 3,
                    db: Session = Depends(get_session)) -> dict:
    """Re-solve from the trip's stored parameters + locks and replace its itinerary
    (e.g. after the POI library or routing data changed)."""
    t = store.get_trip(trip_id, db)
    if t is None:
        raise HTTPException(status_code=404, detail=f"No trip {trip_id}")
    locks = [Lock(**lk) for lk in (t.locks or [])]
    result = _run(t.city_slug, db, t.num_days, min_to_hhmm(t.day_start_min),
                  min_to_hhmm(t.day_end_min), t.base_lat, t.base_lon,
                  t.balance, time_limit, t.profile, locks)
    if not result.get("feasible", True):
        raise HTTPException(status_code=422,
                            detail=f"Re-solve infeasible — trip unchanged: {result.get('reason')}")
    store.replace_trip_result(t, result, db)
    return _trip_out(t, store.trip_stops(trip_id, db))


@app.delete("/trips/{trip_id}")
def remove_trip(trip_id: int, db: Session = Depends(get_session)) -> dict:
    if not store.delete_trip(trip_id, db):
        raise HTTPException(status_code=404, detail=f"No trip {trip_id}")
    return {"ok": True, "deleted": trip_id}


# Static frontend (declared last so it doesn't shadow the API routes above).
app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
