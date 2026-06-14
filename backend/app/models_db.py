"""SQLAlchemy ORM models (scale-up phase 1) — the durable schema behind the
Pydantic API models in models.py.

POIs and trips are **city-scoped**: a POI is identified by (city_slug, id), so the
same slug (e.g. "union-station") can exist in two cities. Single-user for now, so
there is no users table — a later user_id FK is purely additive.
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .db import Base


class City(Base):
    __tablename__ = "cities"

    slug: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    base_lat: Mapped[float] = mapped_column(Float)
    base_lon: Mapped[float] = mapped_column(Float)
    base_name: Mapped[str] = mapped_column(String)
    # [min_lon, min_lat, max_lon, max_lat] — geocoding bias + map fit.
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    has_transit: Mapped[bool] = mapped_column(Boolean, default=False)
    transit_operator: Mapped[str | None] = mapped_column(String, nullable=True)
    default_depart: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which regional engine serves this city (phase 2). Keys into data/regions.json.
    region: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


class POI(Base):
    __tablename__ = "pois"

    # Composite PK: id is unique *within* a city (matches the old per-city JSON file).
    city_slug: Mapped[str] = mapped_column(
        ForeignKey("cities.slug", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    dwell_min: Mapped[int] = mapped_column(Integer, default=60)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="idea")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Trip(Base):
    """A saved trip: solve parameters + metadata; the itinerary lives in trip_stops
    (normalized, queryable) with the raw solver output kept in `result` for fidelity
    (the `dropped` list is read from there)."""

    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_slug: Mapped[str] = mapped_column(
        ForeignKey("cities.slug", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | upcoming | completed
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # day i = start_date + i
    # Solve parameters — enough to re-optimize the trip later (with `locks`).
    num_days: Mapped[int] = mapped_column(Integer)
    day_start_min: Mapped[int] = mapped_column(Integer)   # minutes from midnight
    day_end_min: Mapped[int] = mapped_column(Integer)
    profile: Mapped[str] = mapped_column(String)          # foot | car | bicycle | transit
    balance: Mapped[int] = mapped_column(Integer, default=0)
    base_lat: Mapped[float] = mapped_column(Float)
    base_lon: Mapped[float] = mapped_column(Float)
    locks: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # raw _run output snapshot
    total_travel_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feasible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TripStop(Base):
    """One scheduled visit. `poi_id` is a SOFT reference (no FK) into the trip's
    city's POI library; name/lat/lon are snapshotted so the trip still renders if
    the library POI is later edited or deleted."""

    __tablename__ = "trip_stops"

    trip_id: Mapped[int] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True
    )
    day_index: Mapped[int] = mapped_column(Integer, primary_key=True)  # 0-based
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)        # order within the day
    poi_id: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    dwell_min: Mapped[int] = mapped_column(Integer)
    arrival_min: Mapped[int] = mapped_column(Integer)     # minutes from midnight
    departure_min: Mapped[int] = mapped_column(Integer)
    travel_in_min: Mapped[int] = mapped_column(Integer)   # leg from previous stop/base


class MatrixCache(Base):
    __tablename__ = "matrix_cache"

    key: Mapped[str] = mapped_column(String, primary_key=True)   # cf. matrix.py:_key (sha1)
    city_slug: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    profile: Mapped[str | None] = mapped_column(String, nullable=True)
    matrix: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
