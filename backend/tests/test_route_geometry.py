"""Unit tests for HYL-70 route visualization: the polyline decoder, the engine clients'
`route_geometry` helpers, and the `/route-geometry` endpoint. Every network/DB seam is
monkeypatched — nothing here touches a live routing engine or Postgres.
"""

import pytest

from app import main, osrm, valhalla
from app.polyline import decode_polyline


class FakeResp:
    """Minimal stand-in for an httpx.Response (raise_for_status + json)."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --- decode_polyline ---------------------------------------------------------

def test_decode_polyline_canonical_precision5():
    # The canonical example from Google's polyline-algorithm reference.
    pts = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert pts == [
        pytest.approx((38.5, -120.2), abs=1e-5),
        pytest.approx((40.7, -120.95), abs=1e-5),
        pytest.approx((43.252, -126.453), abs=1e-5),
    ]


def test_decode_polyline_precision_scales_by_ten():
    s = "_p~iF~ps|U"                       # one point: (38.5, -120.2) at precision 5
    (p5,) = decode_polyline(s, precision=5)
    (p6,) = decode_polyline(s, precision=6)
    assert p6[0] == pytest.approx(p5[0] / 10)
    assert p6[1] == pytest.approx(p5[1] / 10)


def test_decode_polyline_empty():
    assert decode_polyline("") == []


# --- valhalla.route_geometry -------------------------------------------------

def test_valhalla_route_geometry_concats_legs_and_dedupes_seam(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"], captured["body"] = url, json
        return FakeResp({"trip": {"legs": [{"shape": "S1"}, {"shape": "S2"}]}})

    monkeypatch.setattr(valhalla.httpx, "post", fake_post)
    shapes = {"S1": [(0.0, 0.0), (1.0, 1.0)], "S2": [(1.0, 1.0), (2.0, 2.0)]}
    monkeypatch.setattr(valhalla, "decode_polyline", lambda s, *a, **k: shapes[s])

    path = valhalla.route_geometry([(0, 0), (1, 1), (2, 2)], "car", "http://engine")
    assert path == [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]    # shared (1,1) seam dropped once
    assert captured["url"] == "http://engine/route"
    assert captured["body"]["costing"] == "auto"
    assert "date_time" not in captured["body"]             # non-transit: no departure time


def test_valhalla_route_geometry_transit_sets_departure(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["body"] = json
        return FakeResp({"trip": {"legs": [{"shape": "S"}]}})

    monkeypatch.setattr(valhalla.httpx, "post", fake_post)
    monkeypatch.setattr(valhalla, "decode_polyline", lambda s, *a, **k: [(0.0, 0.0)])
    monkeypatch.setattr(valhalla, "_depart", lambda: "2026-06-22T10:00")

    valhalla.route_geometry([(0, 0), (1, 1)], "transit", "http://engine")
    assert captured["body"]["costing"] == "multimodal"
    assert captured["body"]["date_time"] == {"type": 1, "value": "2026-06-22T10:00"}


def test_valhalla_route_geometry_none_on_error(monkeypatch):
    # No leg yields geometry (multi-waypoint and every per-leg call fail) -> None.
    def boom(*a, **k):
        raise RuntimeError("unroutable waypoint")

    monkeypatch.setattr(valhalla.httpx, "post", boom)
    assert valhalla.route_geometry([(0, 0), (1, 1)], "car", "http://engine") is None


def test_valhalla_route_geometry_retries_failed_foot_leg_in_reverse(monkeypatch):
    # The pedestrian bug is directional: A->B 500s but B->A routes. For a symmetric mode we retry
    # reversed and flip the shape, so the leg still gets real geometry (not a straight segment).
    A, B = (0.0, 0.0), (1.0, 1.0)
    seen = []

    def fake_post(url, json=None, timeout=None):
        locs = [(loc["lat"], loc["lon"]) for loc in json["locations"]]
        seen.append(locs)
        if len(locs) > 2 or locs == [A, B]:
            raise RuntimeError("GetTags: offset exceeds size of text list")   # forward 500s
        return FakeResp({"trip": {"legs": [{"shape": "S"}]}})                  # B->A routes

    monkeypatch.setattr(valhalla.httpx, "post", fake_post)
    monkeypatch.setattr(valhalla, "decode_polyline", lambda s, *a, **k: [B, A])  # the B->A shape

    path = valhalla.route_geometry([A, B], "foot", "http://engine")
    assert seen[0] == [A, B] and [B, A] in seen   # tried forward, then the reverse
    assert path == [A, B]                          # reversed B->A shape flipped back to A->B


def test_valhalla_route_geometry_straight_only_when_leg_fails_both_ways(monkeypatch):
    # A leg that 500s in BOTH directions degrades to a straight segment; the rest stay real.
    A, B, C = (0.0, 0.0), (1.0, 1.0), (2.0, 2.0)

    def fake_post(url, json=None, timeout=None):
        locs = [(loc["lat"], loc["lon"]) for loc in json["locations"]]
        if len(locs) > 2 or set(locs) == {A, B}:    # A-B unroutable either way
            raise RuntimeError("GetTags: offset exceeds size of text list")
        return FakeResp({"trip": {"legs": [{"shape": "S"}]}})

    monkeypatch.setattr(valhalla.httpx, "post", fake_post)
    monkeypatch.setattr(valhalla, "decode_polyline", lambda s, *a, **k: [B, C])

    path = valhalla.route_geometry([A, B, C], "foot", "http://engine")
    assert path[:2] == [A, B]      # A->B straight (both directions failed)
    assert path[-1] == C           # B->C still real geometry, path continuous


def test_valhalla_route_geometry_car_does_not_retry_reverse(monkeypatch):
    # Driving is not symmetric (one-way streets), so a failed leg must NOT be reversed.
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append([(loc["lat"], loc["lon"]) for loc in json["locations"]])
        raise RuntimeError("boom")

    monkeypatch.setattr(valhalla.httpx, "post", fake_post)
    assert valhalla.route_geometry([(0, 0), (1, 1)], "car", "http://engine") is None
    assert [(1, 1), (0, 0)] not in seen   # never tried the reverse direction


# --- osrm.route_geometry -----------------------------------------------------

def test_osrm_route_geometry_decodes_and_requests_polyline6(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"], captured["params"] = url, params
        return FakeResp({"code": "Ok", "routes": [{"geometry": "S"}]})

    monkeypatch.setattr(osrm.httpx, "get", fake_get)
    monkeypatch.setattr(osrm, "decode_polyline", lambda s, *a, **k: [(0.0, 0.0), (1.0, 1.0)])

    path = osrm.route_geometry([(0, 0), (1, 1)], "foot", "http://osrm")
    assert path == [(0.0, 0.0), (1.0, 1.0)]
    assert captured["params"]["geometries"] == "polyline6"   # matches the precision-6 decoder
    assert captured["url"] == "http://osrm/route/v1/foot/0,0;1,1"   # OSRM wants lon,lat


def test_osrm_route_geometry_none_on_bad_code(monkeypatch):
    monkeypatch.setattr(osrm.httpx, "get",
                        lambda *a, **k: FakeResp({"code": "NoRoute", "routes": []}))
    assert osrm.route_geometry([(0, 0), (1, 1)], "foot", "http://osrm") is None


def test_route_geometry_needs_at_least_two_points():
    assert valhalla.route_geometry([(0, 0)]) is None
    assert osrm.route_geometry([(0, 0)]) is None


# --- POST /route-geometry endpoint -------------------------------------------

def test_route_geometry_endpoint_base_mode_uses_city_engine(monkeypatch):
    monkeypatch.setattr(main, "_engine_url", lambda city, db: "http://city-engine")
    calls = []

    def fake_geo(coords, profile, url):
        calls.append((profile, url))
        return [(0.0, 0.0), (1.0, 1.0)]

    monkeypatch.setattr(main, "engine_route_geometry", fake_geo)
    req = main.RouteGeometryRequest(city="kyoto", profile="foot", mode="base",
                                    days=[[(0, 0), (1, 1)], [(2, 2), (3, 3)]])
    out = main.route_geometry(req, db=None)
    assert out["days"] == [[(0.0, 0.0), (1.0, 1.0)], [(0.0, 0.0), (1.0, 1.0)]]
    assert calls == [("foot", "http://city-engine"), ("foot", "http://city-engine")]


def test_route_geometry_endpoint_route_mode_resolves_region_from_anchors(monkeypatch):
    seen = {}

    def fake_region(pts):
        seen["anchors"] = list(pts)
        return "west"

    monkeypatch.setattr(main.places, "region_for_points", fake_region)
    monkeypatch.setattr(main, "engine_base_url", lambda region=None: f"http://{region}")
    monkeypatch.setattr(main, "engine_route_geometry", lambda coords, profile, url: [url])

    req = main.RouteGeometryRequest(profile="car", mode="route",
                                    days=[[(0, 0), (1, 1), (2, 2)], [(2, 2), (3, 3)]])
    out = main.route_geometry(req, db=None)
    # region resolved from first+last of each day (the anchors), mirroring _run_route
    assert seen["anchors"] == [(0.0, 0.0), (2.0, 2.0), (2.0, 2.0), (3.0, 3.0)]
    assert out["days"] == [["http://west"], ["http://west"]]


def test_route_geometry_endpoint_rejects_cross_region(monkeypatch):
    monkeypatch.setattr(main.places, "region_for_points", lambda pts: None)
    req = main.RouteGeometryRequest(mode="route", days=[[(0, 0), (1, 1)]])
    with pytest.raises(main.HTTPException) as exc:
        main.route_geometry(req, db=None)
    assert exc.value.status_code == 422


def test_route_geometry_endpoint_nulls_degenerate_day(monkeypatch):
    monkeypatch.setattr(main, "_engine_url", lambda city, db: "http://city-engine")
    monkeypatch.setattr(main, "engine_route_geometry", lambda coords, profile, url: [(9.0, 9.0)])
    # a single-point day can't be routed -> null passthrough (and no engine call for it)
    req = main.RouteGeometryRequest(mode="base", days=[[(0, 0), (1, 1)], [(2, 2)]])
    out = main.route_geometry(req, db=None)
    assert out["days"] == [[(9.0, 9.0)], None]
