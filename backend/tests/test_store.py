"""Store-layer round-trip tests for the HYL-68 route-trip schema, on in-memory SQLite.

The ORM uses generic JSON columns (not Postgres JSONB), so the schema builds on SQLite —
this verifies the new trip_day_anchors / trip_pois tables + store helpers without Postgres.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main, store
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
                day_windows=[[540, 1140]], profile="car", balance=5,
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


def test_add_trip_poi_idempotent(db):
    store.add_city("Denver", 39.75, -104.99, "west", db)
    pa = store.add_poi("denver", POICreate(name="Museum", lat=39.7, lon=-104.9), db)
    trip = store.save_trip("denver", _route_meta(), [], {"days": []}, db,
                           anchors=[{"start_lat": 39.75, "start_lon": -104.99, "start_name": None,
                                     "end_lat": 39.8, "end_lon": -105.0, "end_name": None}])
    store.add_trip_poi(trip.id, "denver", pa.id, db)
    store.add_trip_poi(trip.id, "denver", pa.id, db)   # re-add is a no-op
    db.commit()
    assert {p.name for p in store.load_trip_pool(trip.id, db)} == {"Museum"}


# --- main.create_trip / reoptimize_trip route branch (no engine; canned result) ----------

def _seed_denver_boulder(db):
    store.add_city("Denver", 39.75, -104.99, "west", db)
    store.add_city("Boulder", 40.01, -105.27, "west", db)
    store.add_poi("denver", POICreate(name="Museum", lat=39.7, lon=-104.9), db)


_ANCHOR = {"start_lat": 39.75, "start_lon": -104.99, "start_name": "Denver",
           "end_lat": 40.01, "end_lon": -105.27, "end_name": "Boulder"}
_CANNED = {"feasible": True, "total_travel_min": 30, "dropped": [],
           "days": [{"stops": [], "return_hhmm": "10:00", "travel_min": 30,
                     "start": {"lat": 39.75, "lon": -104.99, "name": "Denver"},
                     "end": {"lat": 40.01, "lon": -105.27, "name": "Boulder"}}]}


def test_create_route_trip_persists(db):
    _seed_denver_boulder(db)
    req = main.TripCreate(
        city="denver", title="RT", mode="route",
        day_anchors=[main.DayAnchor(**_ANCHOR)],
        poi_refs=[main.POIRef(city="denver", id="museum")],
        result=_CANNED,   # supply the solve so no engine is needed
    )
    out = main.create_trip(req, db)
    assert out["mode"] == "route" and "base" not in out
    assert out["days"][0]["start"]["name"] == "Denver"
    assert out["days"][0]["end"]["name"] == "Boulder"
    assert len(store.load_day_anchors(out["id"], db)) == 1
    assert {p.name for p in store.load_trip_pool(out["id"], db)} == {"Museum"}


def test_reoptimize_route_resolves_from_stored_anchors_and_pool(db, monkeypatch):
    _seed_denver_boulder(db)
    req = main.TripCreate(city="denver", title="RT", mode="route",
                          day_anchors=[main.DayAnchor(**_ANCHOR)],
                          poi_refs=[main.POIRef(city="denver", id="museum")], result=_CANNED)
    trip_id = main.create_trip(req, db)["id"]

    captured = {}

    def fake_run_route(pois, anchors, *a, **k):
        captured["pois"] = sorted(p.name for p in pois)
        captured["ndays"] = len(anchors)
        return {"feasible": True, "total_travel_min": 1, "dropped": [],
                "days": [{"stops": [], "return_hhmm": "10:00", "travel_min": 1,
                          "start": {"lat": a[0][0], "lon": a[0][1], "name": a[0][2]},
                          "end": {"lat": a[1][0], "lon": a[1][1], "name": a[1][2]}} for a in anchors]}

    monkeypatch.setattr(main, "_run_route", fake_run_route)
    out = main.reoptimize_trip(trip_id, db=db)
    assert captured["pois"] == ["Museum"]   # re-solved from the stored pool
    assert captured["ndays"] == 1           # and the stored anchors
    assert out["total_travel_min"] == 1


# --- cross-city pool identity: library ids are unique only within a city ------------------

def test_load_pois_by_refs_city_qualifies_ids(db):
    # Same name in two cities -> the same bare library id ("museum") in each. The pool must
    # qualify them ("city:id") so they don't collide into one POI when solved/rendered.
    store.add_city("Denver", 39.75, -104.99, "west", db)
    store.add_city("Boulder", 40.01, -105.27, "west", db)
    store.add_poi("denver", POICreate(name="Museum", lat=39.7, lon=-104.9), db)
    store.add_poi("boulder", POICreate(name="Museum", lat=40.0, lon=-105.2), db)

    pool = store.load_pois_by_refs([("denver", "museum"), ("boulder", "museum")], db)
    by_id = {p.id: p for p in pool}
    assert set(by_id) == {"denver:museum", "boulder:museum"}   # no collision
    assert by_id["denver:museum"].lat == 39.7
    assert by_id["boulder:museum"].lat == 40.0


def test_load_trip_pool_city_qualifies_ids(db):
    store.add_city("Denver", 39.75, -104.99, "west", db)
    store.add_city("Boulder", 40.01, -105.27, "west", db)
    store.add_poi("denver", POICreate(name="Spot", lat=39.7, lon=-104.9), db)
    store.add_poi("boulder", POICreate(name="Spot", lat=40.0, lon=-105.2), db)
    trip = store.save_trip("denver", _route_meta(), [], {"days": []}, db,
                           anchors=[{"start_lat": 39.75, "start_lon": -104.99, "start_name": None,
                                     "end_lat": 40.01, "end_lon": -105.27, "end_name": None}],
                           poi_refs=[("denver", "spot"), ("boulder", "spot")])
    assert {p.id for p in store.load_trip_pool(trip.id, db)} == {"denver:spot", "boulder:spot"}


def test_update_route_to_base_clears_anchors_and_pool(db):
    # Switching a saved trip route -> base (PUT) must drop its now-meaningless route rows.
    _seed_denver_boulder(db)
    tid = main.create_trip(main.TripCreate(
        city="denver", title="RT", mode="route", day_anchors=[main.DayAnchor(**_ANCHOR)],
        poi_refs=[main.POIRef(city="denver", id="museum")], result=_CANNED), db)["id"]
    assert store.load_day_anchors(tid, db) and store.load_trip_pool(tid, db)   # present first

    base_canned = {"feasible": True, "total_travel_min": 1, "dropped": [],
                   "days": [{"stops": [], "return_hhmm": "10:00", "travel_min": 1}]}
    out = main.update_trip(tid, main.TripCreate(
        city="denver", title="RT", mode="base", days=1,
        base_lat=39.75, base_lon=-104.99, result=base_canned), db)

    assert out["mode"] == "base" and "base" in out
    assert store.load_day_anchors(tid, db) == []
    assert store.load_trip_pool(tid, db) == []


# --- HYL-69: per-day windows persist + drive re-optimize ---------------------------------

_TWO_DAY_CANNED = {"feasible": True, "total_travel_min": 0, "dropped": [],
                   "days": [{"stops": [], "return_hhmm": "11:00", "travel_min": 0},
                            {"stops": [], "return_hhmm": "18:00", "travel_min": 0}]}


def _two_day_window_trip(db):
    store.add_city("Denver", 39.75, -104.99, "west", db)
    return main.create_trip(main.TripCreate(
        city="denver", title="WB", mode="base", days=2, base_lat=39.75, base_lon=-104.99,
        day_windows=[main.DayWindow(start="07:00", end="11:00"),
                     main.DayWindow(start="11:00", end="18:00")],
        result=_TWO_DAY_CANNED), db)


def test_base_trip_persists_per_day_windows(db):
    out = _two_day_window_trip(db)
    # Each day surfaces its own window; there is no top-level envelope (HYL-69).
    assert [(d["day_start"], d["day_end"]) for d in out["days"]] == \
        [("07:00", "11:00"), ("11:00", "18:00")]
    assert "day_start" not in out and "day_end" not in out
    assert store.get_trip(out["id"], db).day_windows == [[420, 660], [660, 1080]]


def test_reoptimize_base_passes_stored_day_windows(db, monkeypatch):
    tid = _two_day_window_trip(db)["id"]
    captured = {}

    def fake_run(city, db, days, start, end, blat, blon, balance, tl, profile, locks,
                 day_windows=None):
        captured["win"] = day_windows
        return _TWO_DAY_CANNED

    monkeypatch.setattr(main, "_run", fake_run)
    main.reoptimize_trip(tid, db=db)
    assert captured["win"] == [(420, 660), (660, 1080)]   # the stored per-day windows, in minutes
