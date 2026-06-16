"""The POI / city / trip store, backed by Postgres (scale-up phase 1).

Replaces the per-city JSON files: a POI now lives in the `pois` table scoped by
`city_slug`, so switching city is a query — not a file or process swap. This is
still the only module that knows *how* persistence works; callers pass a `city`
slug and a SQLAlchemy `Session` (FastAPI's `Depends(get_session)`, or
`SessionLocal()` outside a request).
"""

import math
import re

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from . import models_db as m
from .models import POI, POICreate


# --- POIs --------------------------------------------------------------------

def _to_poi(row: m.POI) -> POI:
    """ORM row -> API model (the Pydantic POI other modules already speak)."""
    return POI(
        id=row.id, name=row.name, lat=row.lat, lon=row.lon,
        dwell_min=row.dwell_min, importance=row.importance,
        hours=row.hours, tags=row.tags or [], notes=row.notes, status=row.status,
    )


def load_pois(city: str, db: Session) -> dict[str, POI]:
    """Id-keyed POIs for one city, in insertion order (stable for the solver/UI)."""
    rows = db.scalars(
        select(m.POI).where(m.POI.city_slug == city).order_by(m.POI.created_at, m.POI.id)
    ).all()
    return {r.id: _to_poi(r) for r in rows}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "poi"


def _unique_id(name: str, existing) -> str:
    """A filesystem-friendly id from the name, de-duped with -2/-3 suffixes
    (unchanged from the JSON store; ids stay unique *within* a city)."""
    base = _slugify(name)
    existing = set(existing)
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def add_poi(city: str, create: POICreate, db: Session) -> POI:
    """Assign a city-unique id, persist, and return the new POI."""
    existing = set(db.scalars(select(m.POI.id).where(m.POI.city_slug == city)).all())
    poi = create.to_poi(_unique_id(create.name, existing))
    fields = poi.model_dump(mode="json", exclude={"id"})   # name/lat/lon/.../hours/tags
    db.add(m.POI(city_slug=city, id=poi.id, **fields))
    db.commit()
    return poi


def delete_poi(city: str, poi_id: str, db: Session) -> bool:
    """Remove a POI by (city, id); return whether it existed."""
    row = db.get(m.POI, {"city_slug": city, "id": poi_id})
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


# --- Cities (the registry the UI + geocoding bias read) ----------------------

def list_cities(db: Session) -> list[m.City]:
    return list(db.scalars(select(m.City).order_by(m.City.label)).all())


def get_city(city: str, db: Session) -> m.City | None:
    return db.get(m.City, city)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def add_city(name: str, base_lat: float, base_lon: float, region: str | None,
             db: Session, *, base_name: str | None = None,
             has_transit: bool = False) -> m.City:
    """Create (or reuse) a lightweight place from a free-base search — the structural
    parent a POI library / trips hang off (FK to cities.slug). Re-searching the same
    spot is idempotent: if a city already holds this slug within ~1 km of the base, it
    is reused (returning a catalog city when you land back on one); a slug taken by a
    *different* place gets a -2/-3 suffix (like _unique_id for POIs)."""
    slug = _slugify(name)
    existing = db.get(m.City, slug)
    if existing is not None and _haversine_km(
            existing.base_lat, existing.base_lon, base_lat, base_lon) <= 1.0:
        return existing
    if existing is not None:                     # same name, different place -> new slug
        all_slugs = set(db.scalars(select(m.City.slug)).all())
        slug = _unique_id(name, all_slugs)
    city = m.City(
        slug=slug, label=name, base_lat=base_lat, base_lon=base_lon,
        base_name=base_name or name, region=region,
        has_transit=has_transit, user_created=True,
    )
    db.add(city)
    db.commit()
    db.refresh(city)
    return city


def delete_city(slug: str, db: Session) -> bool:
    """Remove a place by slug; its POIs/trips (and their stops) cascade via the FK
    ondelete=CASCADE. Returns whether it existed. (Caller guards catalog cities.)"""
    city = db.get(m.City, slug)
    if city is None:
        return False
    db.delete(city)
    db.commit()
    return True


# --- Trips (saved itineraries: normalized stops + raw result snapshot) -------

def _add_stops(trip: m.Trip, result: dict, db: Session) -> None:
    """Decompose a solved `_run` result into trip_stops rows (snapshotting the POI
    essentials, so the trip still renders if a library POI is edited/deleted)."""
    for di, day in enumerate(result.get("days", [])):
        for si, s in enumerate(day.get("stops", [])):
            db.add(m.TripStop(
                trip_id=trip.id, day_index=di, seq=si,
                poi_id=s.get("poi_id"), name=s.get("name", ""),
                lat=s.get("lat", 0.0), lon=s.get("lon", 0.0),
                dwell_min=int(s.get("dwell", 0)),
                arrival_min=int(s.get("arrival", 0)),
                departure_min=int(s.get("departure", 0)),
                travel_in_min=int(s.get("travel_in", 0)),
            ))


def save_trip(city: str, meta: dict, locks: list, result: dict, db: Session,
              *, anchors: list[dict] | None = None,
              poi_refs: list[tuple[str, str]] | None = None) -> m.Trip:
    """Persist a trip + its stops in one transaction. `meta` carries the column fields
    (title/status/notes/start_date/num_days/day_*_min/profile/balance/mode/base_*). A
    route trip (HYL-68) also passes `anchors` (per-day start/end dicts) and `poi_refs`
    ((city_slug, poi_id) candidate pool)."""
    trip = m.Trip(city_slug=city, locks=locks, result=result,
                  total_travel_min=result.get("total_travel_min"),
                  feasible=bool(result.get("feasible", True)), **meta)
    db.add(trip)
    db.flush()                      # assign trip.id before the child rows
    _add_stops(trip, result, db)
    if anchors:
        save_day_anchors(trip.id, anchors, db)
    if poi_refs is not None:
        set_trip_pois(trip.id, poi_refs, db)
    db.commit()
    db.refresh(trip)
    return trip


def list_trips(city: str, db: Session, status: str | None = None) -> list[m.Trip]:
    q = select(m.Trip).where(m.Trip.city_slug == city).order_by(m.Trip.updated_at.desc())
    if status:
        q = q.where(m.Trip.status == status)
    return list(db.scalars(q).all())


def trip_stop_counts(db: Session, trip_ids: list[int]) -> dict[int, int]:
    """trip_id -> number of scheduled stops (one grouped query, no N+1)."""
    if not trip_ids:
        return {}
    rows = db.execute(
        select(m.TripStop.trip_id, func.count())
        .where(m.TripStop.trip_id.in_(trip_ids)).group_by(m.TripStop.trip_id)
    ).all()
    return dict(rows)


def get_trip(trip_id: int, db: Session) -> m.Trip | None:
    return db.get(m.Trip, trip_id)


def trip_stops(trip_id: int, db: Session) -> list[m.TripStop]:
    """The trip's visits in itinerary order."""
    return list(db.scalars(
        select(m.TripStop).where(m.TripStop.trip_id == trip_id)
        .order_by(m.TripStop.day_index, m.TripStop.seq)
    ).all())


def update_trip_meta(trip_id: int, fields: dict, db: Session) -> m.Trip | None:
    """Patch metadata (title/status/notes/start_date); returns None if no such trip."""
    trip = db.get(m.Trip, trip_id)
    if trip is None:
        return None
    for k, v in fields.items():
        setattr(trip, k, v)
    db.commit()
    db.refresh(trip)
    return trip


def update_trip(trip: m.Trip, meta: dict, locks: list, result: dict, db: Session,
                *, anchors: list[dict] | None = None,
                poi_refs: list[tuple[str, str]] | None = None) -> m.Trip:
    """Replace everything about a saved trip from the current session (in-place PUT):
    metadata + solve-param columns + locks + result snapshot + stops (+ route anchors/pool)."""
    for k, v in meta.items():
        setattr(trip, k, v)
    trip.locks = locks
    db.execute(delete(m.TripStop).where(m.TripStop.trip_id == trip.id))
    trip.result = result
    trip.total_travel_min = result.get("total_travel_min")
    trip.feasible = bool(result.get("feasible", True))
    _add_stops(trip, result, db)
    if anchors is not None:
        save_day_anchors(trip.id, anchors, db)
    if poi_refs is not None:
        set_trip_pois(trip.id, poi_refs, db)
    db.commit()
    db.refresh(trip)
    return trip


def replace_trip_result(trip: m.Trip, result: dict, db: Session) -> m.Trip:
    """Swap in a fresh solve (re-optimize): replace the stops + result snapshot."""
    db.execute(delete(m.TripStop).where(m.TripStop.trip_id == trip.id))
    trip.result = result
    trip.total_travel_min = result.get("total_travel_min")
    trip.feasible = bool(result.get("feasible", True))
    _add_stops(trip, result, db)
    db.commit()
    db.refresh(trip)
    return trip


def delete_trip(trip_id: int, db: Session) -> bool:
    trip = db.get(m.Trip, trip_id)
    if trip is None:
        return False
    db.delete(trip)                 # stops / anchors / pool go via FK ON DELETE CASCADE
    db.commit()
    return True


# --- Route trips: per-day anchors + candidate POI pool (HYL-68) ---------------

def save_day_anchors(trip_id: int, anchors: list[dict], db: Session) -> None:
    """Replace a route trip's per-day (start, end) anchors. `anchors` is ordered by day;
    each is a dict of start_lat/start_lon/start_name + end_lat/end_lon/end_name."""
    db.execute(delete(m.TripDayAnchor).where(m.TripDayAnchor.trip_id == trip_id))
    for i, a in enumerate(anchors):
        db.add(m.TripDayAnchor(trip_id=trip_id, day_index=i, **a))


def load_day_anchors(trip_id: int, db: Session) -> list[m.TripDayAnchor]:
    """A route trip's anchors in day order (empty for a base trip)."""
    return list(db.scalars(
        select(m.TripDayAnchor).where(m.TripDayAnchor.trip_id == trip_id)
        .order_by(m.TripDayAnchor.day_index)
    ).all())


def set_trip_pois(trip_id: int, refs: list[tuple[str, str]], db: Session) -> None:
    """Replace a trip's candidate POI pool with the given (city_slug, poi_id) refs."""
    db.execute(delete(m.TripPoi).where(m.TripPoi.trip_id == trip_id))
    for city_slug, poi_id in refs:
        db.add(m.TripPoi(trip_id=trip_id, city_slug=city_slug, poi_id=poi_id))


def add_trip_poi(trip_id: int, city_slug: str, poi_id: str, db: Session) -> None:
    """Add one POI to a trip's pool (idempotent — re-adding the same ref is a no-op)."""
    if db.get(m.TripPoi, {"trip_id": trip_id, "city_slug": city_slug, "poi_id": poi_id}) is None:
        db.add(m.TripPoi(trip_id=trip_id, city_slug=city_slug, poi_id=poi_id))


def pool_poi_id(city_slug: str, poi_id: str) -> str:
    """The identity of a POI *inside a route pool*. Library ids are unique only within a
    city (see `_unique_id`), but a route pool spans several towns, so two cities can each
    hold a "museum"/"downtown". Qualify with the city ("city:id") so pooled POIs stay
    distinct — and so the solver's stops/dropped and the user's locks reference one POI,
    not two. Deterministic, so a lock keeps matching across re-solves. (City slugs and POI
    ids are `[a-z0-9-]` only, so the ":" separator is unambiguous.)"""
    return f"{city_slug}:{poi_id}"


def load_pois_by_refs(refs: list[tuple[str, str]], db: Session) -> list[POI]:
    """Fetch library POIs by their (city_slug, id) refs in one query (skips any missing) —
    the pool for a route trip spans multiple towns/places. Returned POIs carry a city-
    qualified `id` (see `pool_poi_id`) so a slug shared across cities can't collide."""
    if not refs:
        return []
    rows = db.scalars(
        select(m.POI).where(tuple_(m.POI.city_slug, m.POI.id).in_([(c, p) for c, p in refs]))
        .order_by(m.POI.created_at, m.POI.id)
    ).all()
    return [_to_poi(r).model_copy(update={"id": pool_poi_id(r.city_slug, r.id)}) for r in rows]


def load_trip_pool(trip_id: int, db: Session) -> list[POI]:
    """The trip's candidate POIs (spans towns/places), skipping any since-deleted."""
    refs = db.execute(
        select(m.TripPoi.city_slug, m.TripPoi.poi_id).where(m.TripPoi.trip_id == trip_id)
    ).all()
    return load_pois_by_refs([(c, p) for c, p in refs], db)
