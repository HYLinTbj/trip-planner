"""Store-layer round-trip tests for the HYL-68 route-trip schema, on in-memory SQLite.

The ORM uses generic JSON columns (not Postgres JSONB), so the schema builds on SQLite —
this verifies the new trip_day_anchors / trip_pois tables + store helpers without Postgres.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import store
from app.db import Base
from app.models import POICreate


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _rec):          # SQLite needs FK enforcement turned on
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _route_meta(**over):
    meta = dict(title="RT", status="draft", notes=None, start_date=None, num_days=1,
                day_start_min=540, day_end_min=1140, profile="car", balance=5,
                mode="route", base_lat=None, base_lon=None)
    meta.update(over)
    return meta


def test_save_and_load_route_trip(db):
    store.add_city("Denver", 39.75, -104.99, "west", db)
    store.add_city("Boulder", 40.01, -105.27, "west", db)
    pa = store.add_poi("denver", POICreate(name="Museum", lat=39.7, lon=-104.9), db)
    pb = store.add_poi("boulder", POICreate(name="Trail", lat=40.0, lon=-105.2), db)

    anchors = [{"start_lat": 39.75, "start_lon": -104.99, "start_name": "Denver",
                "end_lat": 40.01, "end_lon": -105.27, "end_name": "Boulder"}]
    result = {"feasible": True, "total_travel_min": 30, "days": [{"stops": []}]}
    trip = store.save_trip("denver", _route_meta(), [], result, db,
                           anchors=anchors,
                           poi_refs=[("denver", pa.id), ("boulder", pb.id)])

    assert trip.mode == "route" and trip.base_lat is None
    got = store.load_day_anchors(trip.id, db)
    assert len(got) == 1
    assert (got[0].start_name, got[0].end_name) == ("Denver", "Boulder")
    pool = store.load_trip_pool(trip.id, db)
    assert {p.name for p in pool} == {"Museum", "Trail"}   # composed from two towns


def test_delete_route_trip_cascades(db):
    store.add_city("Denver", 39.75, -104.99, "west", db)
    pa = store.add_poi("denver", POICreate(name="X", lat=39.7, lon=-104.9), db)
    trip = store.save_trip(
        "denver", _route_meta(), [], {"days": []}, db,
        anchors=[{"start_lat": 39.75, "start_lon": -104.99, "start_name": None,
                  "end_lat": 39.8, "end_lon": -105.0, "end_name": None}],
        poi_refs=[("denver", pa.id)],
    )
    tid = trip.id
    assert store.delete_trip(tid, db) is True
    assert store.load_day_anchors(tid, db) == []
    assert store.load_trip_pool(tid, db) == []


def test_load_trip_pool_skips_deleted_poi(db):
    store.add_city("Denver", 39.75, -104.99, "west", db)
    pa = store.add_poi("denver", POICreate(name="Keep", lat=39.7, lon=-104.9), db)
    pb = store.add_poi("denver", POICreate(name="Gone", lat=39.71, lon=-104.91), db)
    trip = store.save_trip(
        "denver", _route_meta(), [], {"days": []}, db,
        anchors=[{"start_lat": 39.75, "start_lon": -104.99, "start_name": None,
                  "end_lat": 39.8, "end_lon": -105.0, "end_name": None}],
        poi_refs=[("denver", pa.id), ("denver", pb.id)],
    )
    store.delete_poi("denver", pb.id, db)   # soft ref -> pool drops it silently
    assert {p.name for p in store.load_trip_pool(trip.id, db)} == {"Keep"}


def test_haversine_and_unique_id():
    d = store._haversine_km(39.7392, -104.9903, 40.015, -105.2705)   # Denver -> Boulder
    assert 30 < d < 45
    assert store._unique_id("Foo", set()) == "foo"
    assert store._unique_id("Foo", {"foo"}) == "foo-2"
