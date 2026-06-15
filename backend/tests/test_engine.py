"""Unit tests for the small pure helpers in the routing-engine clients
(valhalla.py / osrm.py). The network calls themselves are not exercised here.
"""

import datetime

from app import osrm, valhalla


# --- valhalla ----------------------------------------------------------------

def test_costing_for_maps_profiles():
    assert valhalla.costing_for("foot") == "pedestrian"
    assert valhalla.costing_for("car") == "auto"
    assert valhalla.costing_for("bicycle") == "bicycle"
    assert valhalla.costing_for("transit") == "multimodal"
    assert valhalla.costing_for(None) == "pedestrian"
    assert valhalla.costing_for("nonsense") == "pedestrian"


def test_url_for_region_falls_back_to_default():
    assert valhalla.url_for_region("definitely-not-a-region") == valhalla.VALHALLA_URL
    assert valhalla.url_for_region(None) == valhalla.VALHALLA_URL


def test_depart_rolls_to_a_weekday(monkeypatch):
    monkeypatch.setattr(valhalla, "VALHALLA_DEPART", "")   # force the computed path
    iso = valhalla._depart()
    d = datetime.date.fromisoformat(iso.split("T")[0])
    assert d.weekday() < 5            # Mon-Fri
    assert iso.endswith("T10:00")


def test_pairwise_matrix_shape(monkeypatch):
    coords = [(0, 0), (1, 1), (2, 2)]

    def fake_route_time(a, b, base_url, costing="multimodal", depart=None):
        return None if (a == (1, 1) and b == (2, 2)) else 60.0

    monkeypatch.setattr(valhalla, "_route_time", fake_route_time)
    m = valhalla._pairwise_matrix(coords, "http://test", "auto")
    assert m[0][0] == 0.0 and m[1][1] == 0.0    # diagonal is zero
    assert m[0][1] == 60.0
    assert m[1][2] is None                      # unroutable pair preserved


def test_valhalla_to_minutes():
    assert valhalla.to_minutes([[0.0, 90.0], [None, 0.0]]) == [[0.0, 1.5], [None, 0.0]]


# --- osrm --------------------------------------------------------------------

def test_osrm_url_for_profile(monkeypatch):
    monkeypatch.setattr(osrm, "OSRM_URL_ALL", "")   # ignore any single-url override
    assert osrm.url_for("foot") == osrm.OSRM_FOOT_URL
    assert osrm.url_for("car") == osrm.OSRM_CAR_URL
    assert osrm.url_for("driving") == osrm.OSRM_CAR_URL


def test_osrm_to_minutes():
    assert osrm.to_minutes([[120.0, None]]) == [[2.0, None]]
