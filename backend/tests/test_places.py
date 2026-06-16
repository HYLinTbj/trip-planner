"""Unit tests for US census-region resolution (places.py).

`region_for_state` is pure and table-driven; `region_for_point` is tested with the
geocoder monkeypatched (no network).
"""

from app import places
from app.places import region_for_point, region_for_points, region_for_state


def test_region_for_state_usps_codes():
    assert region_for_state("CO") == "west"
    assert region_for_state("NY") == "northeast"
    assert region_for_state("TX") == "south"
    assert region_for_state("IL") == "midwest"


def test_region_for_state_iso_and_full_name():
    assert region_for_state("US-CO") == "west"   # ISO 3166-2
    assert region_for_state("Colorado") == "west"
    assert region_for_state("colorado") == "west"


def test_region_for_state_unsupported_states():
    # AK/HI are outside the us-west tile footprint -> deliberately None
    for v in ("AK", "Alaska", "HI", "Hawaii"):
        assert region_for_state(v) is None


def test_region_for_state_junk():
    for v in ("ZZ", "US-ZZ", "", None):
        assert region_for_state(v) is None


def test_region_for_point_uses_iso_code(monkeypatch):
    monkeypatch.setattr(places.geocode, "reverse",
                        lambda lat, lon, detail=False: {"address": {"ISO3166-2-lvl4": "US-CO"}})
    assert region_for_point(39.7, -104.9) == "west"


def test_region_for_point_falls_back_to_state_name(monkeypatch):
    monkeypatch.setattr(places.geocode, "reverse",
                        lambda lat, lon, detail=False: {"address": {"state": "Colorado"}})
    assert region_for_point(39.7, -104.9) == "west"


def test_region_for_point_non_us_is_none(monkeypatch):
    monkeypatch.setattr(places.geocode, "reverse",
                        lambda lat, lon, detail=False: {"address": {"country": "Japan"}})
    assert region_for_point(35.0, 135.0) is None


# --- region_for_points (HYL-68 within-region guard) --------------------------

def test_region_for_points_single_region(monkeypatch):
    monkeypatch.setattr(places, "region_for_point", lambda lat, lon: "west")
    assert region_for_points([(1, 2), (3, 4), (5, 6)]) == "west"


def test_region_for_points_spanning_regions_is_none(monkeypatch):
    seq = iter(["west", "midwest"])
    monkeypatch.setattr(places, "region_for_point", lambda lat, lon: next(seq))
    assert region_for_points([(1, 2), (3, 4)]) is None


def test_region_for_points_out_of_coverage_is_none(monkeypatch):
    monkeypatch.setattr(places, "region_for_point", lambda lat, lon: None)
    assert region_for_points([(1, 2)]) is None


def test_region_for_points_mixed_coverage_is_none(monkeypatch):
    seq = iter(["west", None])
    monkeypatch.setattr(places, "region_for_point", lambda lat, lon: next(seq))
    assert region_for_points([(1, 2), (3, 4)]) is None


def test_region_for_points_dedupes_repeated_coords(monkeypatch):
    # A route's anchors repeat (each day's end == the next day's start), so the same coord
    # arrives several times. region_for_point hits rate-limited Nominatim, so each distinct
    # coordinate must be looked up only once.
    calls = []
    monkeypatch.setattr(places, "region_for_point",
                        lambda lat, lon: calls.append((lat, lon)) or "west")
    # 3-day chain over 4 distinct anchors, passed as 6 (start+end per day).
    pts = [(0, 0), (1, 1), (1, 1), (2, 2), (2, 2), (3, 3)]
    assert region_for_points(pts) == "west"
    assert len(calls) == 4               # 4 distinct coords, not 6 calls
