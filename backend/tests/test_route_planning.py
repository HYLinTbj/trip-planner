"""Unit tests for the route-mode solve path (main._run_route).

The routing engine + region lookup are monkeypatched, so no network/DB is touched — this
exercises the within-region guard and the per-day-leg response shaping (HYL-68).
"""

import pytest

from app import main
from tests.conftest import make_poi, matrix_from_positions


def test_run_route_shapes_per_day_anchors(monkeypatch):
    monkeypatch.setattr(main.places, "region_for_points", lambda pts: "west")
    # coords = [start(0,0), end(10,0), p1(3,0), p2(7,0)] -> positions on a line
    monkeypatch.setattr(main, "get_matrix_min",
                        lambda coords, **kw: matrix_from_positions([0, 10, 3, 7], gap=10))

    pois = [make_poi("p1", lat=3, lon=0), make_poi("p2", lat=7, lon=0)]
    anchors = [((0, 0, "A"), (10, 0, "B"))]
    res = main._run_route(pois, anchors, "09:00", "19:00", 5, 1, "car", [])

    assert res["feasible"] is True
    assert "base" not in res                       # a route trip has no single base
    day = res["days"][0]
    assert day["start"] == {"lat": 0, "lon": 0, "name": "A"}
    assert day["end"] == {"lat": 10, "lon": 0, "name": "B"}
    assert {s["poi_id"] for s in day["stops"]} == {"p1", "p2"}   # both on the A→B leg


def test_run_route_rejects_cross_region(monkeypatch):
    monkeypatch.setattr(main.places, "region_for_points", lambda pts: None)
    with pytest.raises(main.HTTPException) as exc:
        main._run_route([make_poi("p1", lat=1, lon=1)],
                        [((0, 0, "A"), (1, 1, "B"))], "09:00", "19:00", 5, 1, "car", [])
    assert exc.value.status_code == 422


def test_run_route_keeps_same_name_pois_from_different_cities_distinct(monkeypatch):
    # A cross-city pool gives two POIs the same NAME but distinct city-qualified ids
    # (store.pool_poi_id). The output must keep them separate — before the fix, _solve's
    # by_id collapsed equal ids and cross-wired one stop's coordinates onto the other.
    monkeypatch.setattr(main.places, "region_for_points", lambda pts: "west")
    # coords = [start(0), end(30), denver "Museum"(10), boulder "Museum"(20)] on a line
    monkeypatch.setattr(main, "get_matrix_min",
                        lambda coords, **kw: matrix_from_positions([0, 30, 10, 20], gap=10))

    pois = [make_poi("denver:museum", name="Museum", lat=10, lon=0),
            make_poi("boulder:museum", name="Museum", lat=20, lon=0)]
    res = main._run_route(pois, [((0, 0, "A"), (30, 0, "B"))], "09:00", "19:00", 5, 1, "car", [])

    assert res["feasible"] is True
    stops = {s["poi_id"]: s for s in res["days"][0]["stops"]}
    assert set(stops) == {"denver:museum", "boulder:museum"}
    assert stops["denver:museum"]["lat"] == 10      # coordinates not cross-wired
    assert stops["boulder:museum"]["lat"] == 20
