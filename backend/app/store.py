"""The POI / city / trip store, backed by Postgres (scale-up phase 1).

Replaces the per-city JSON files: a POI now lives in the `pois` table scoped by
`city_slug`, so switching city is a query — not a file or process swap. This is
still the only module that knows *how* persistence works; callers pass a `city`
slug and a SQLAlchemy `Session` (FastAPI's `Depends(get_session)`, or
`SessionLocal()` outside a request).
"""

import re

from sqlalchemy import delete, func, select
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


def save_trip(city: str, meta: dict, locks: list, result: dict, db: Session) -> m.Trip:
    """Persist a trip + its stops in one transaction. `meta` carries the column
    fields (title/status/notes/start_date/num_days/day_*_min/profile/balance/base_*)."""
    trip = m.Trip(city_slug=city, locks=locks, result=result,
                  total_travel_min=result.get("total_travel_min"),
                  feasible=bool(result.get("feasible", True)), **meta)
    db.add(trip)
    db.flush()                      # assign trip.id before the stop rows
    _add_stops(trip, result, db)
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


def update_trip(trip: m.Trip, meta: dict, locks: list, result: dict, db: Session) -> m.Trip:
    """Replace everything about a saved trip from the current session (in-place PUT):
    metadata + solve-param columns + locks + result snapshot + stops."""
    for k, v in meta.items():
        setattr(trip, k, v)
    trip.locks = locks
    db.execute(delete(m.TripStop).where(m.TripStop.trip_id == trip.id))
    trip.result = result
    trip.total_travel_min = result.get("total_travel_min")
    trip.feasible = bool(result.get("feasible", True))
    _add_stops(trip, result, db)
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
    db.delete(trip)                 # trip_stops go via FK ON DELETE CASCADE
    db.commit()
    return True
