"""Unit tests for the travel-time matrix builder + on-disk cache.

`table_durations` (the routing-engine call) is monkeypatched to a synthetic seconds
matrix, and `base_url` is always passed explicitly so no live engine is resolved.
"""

import json

from app import matrix
from app.matrix import UNREACHABLE, _key, _repair_unreachable, get_matrix_min, inflate_travel


# --- cache key ---------------------------------------------------------------

def test_key_deterministic():
    coords = [(1.0, 2.0), (3.0, 4.0)]
    assert _key(coords, "foot", "http://e") == _key(coords, "foot", "http://e")


def test_key_changes_with_profile_and_url():
    coords = [(1.0, 2.0)]
    base = _key(coords, "foot", "http://e")
    assert _key(coords, "car", "http://e") != base       # profile in key
    assert _key(coords, "foot", "http://other") != base  # engine url in key


def test_key_stable_under_subprecision_jitter():
    # coordinates are rounded to 6 dp, so jitter below that doesn't bust the cache
    a = _key([(1.0000001, 2.0000001)], "foot", "http://e")
    b = _key([(1.0000002, 2.0000002)], "foot", "http://e")
    assert a == b


# --- conversion + caching ----------------------------------------------------

def test_converts_seconds_to_int_minutes_and_caches(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_table(coords, profile=None, base_url=None):
        calls["n"] += 1
        return [[0.0, 120.0], [60.0, 0.0]]

    monkeypatch.setattr(matrix, "table_durations", fake_table)
    cache = tmp_path / "cache.json"
    coords = [(1.0, 2.0), (3.0, 4.0)]

    out = get_matrix_min(coords, profile="car", cache_path=str(cache), base_url="http://test")
    assert out == [[0, 2], [1, 0]]        # 120s -> 2 min, 60s -> 1 min
    assert calls["n"] == 1
    assert cache.exists()

    # second call is served from cache: table_durations is NOT re-invoked
    again = get_matrix_min(coords, profile="car", cache_path=str(cache), base_url="http://test")
    assert again == out
    assert calls["n"] == 1
    assert len(json.loads(cache.read_text())) == 1   # exactly one cached key


def test_none_becomes_unreachable(monkeypatch):
    monkeypatch.setattr(matrix, "table_durations",
                        lambda *a, **k: [[0.0, None], [None, 0.0]])
    out = get_matrix_min([(0, 0), (1, 1)], profile="car", base_url="http://test")
    assert out[0][1] == UNREACHABLE
    assert out[1][0] == UNREACHABLE


def test_distinct_base_urls_dont_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "table_durations",
                        lambda *a, **k: [[0.0, 120.0], [120.0, 0.0]])
    cache = tmp_path / "c.json"
    coords = [(0, 0), (1, 1)]
    get_matrix_min(coords, profile="car", cache_path=str(cache), base_url="http://a")
    get_matrix_min(coords, profile="car", cache_path=str(cache), base_url="http://b")
    assert len(json.loads(cache.read_text())) == 2   # one entry per engine url


# --- unreachable-arc repair --------------------------------------------------

def test_repair_mirrors_one_way_unreachable():
    m = [[0, 5], [UNREACHABLE, 0]]
    _repair_unreachable(m)
    assert m[1][0] == 5      # mirrored from the reachable reverse
    assert m[0][1] == 5


def test_repair_keeps_two_way_unreachable():
    m = [[0, UNREACHABLE], [UNREACHABLE, 0]]
    _repair_unreachable(m)
    assert m[0][1] == UNREACHABLE
    assert m[1][0] == UNREACHABLE


def test_symmetric_profile_triggers_repair(monkeypatch):
    # foot is symmetric -> a one-directional dead arc is mirrored
    monkeypatch.setattr(matrix, "table_durations",
                        lambda *a, **k: [[0.0, 300.0], [None, 0.0]])
    out = get_matrix_min([(0, 0), (1, 1)], profile="foot", base_url="http://test")
    assert out[0][1] == 5
    assert out[1][0] == 5            # repaired


def test_asymmetric_profile_skips_repair(monkeypatch):
    # car has one-way streets -> a dead arc is left unreachable
    monkeypatch.setattr(matrix, "table_durations",
                        lambda *a, **k: [[0.0, 300.0], [None, 0.0]])
    out = get_matrix_min([(0, 0), (1, 1)], profile="car", base_url="http://test")
    assert out[0][1] == 5
    assert out[1][0] == UNREACHABLE


# --- HYL-72 travel buffer (inflate_travel) -----------------------------------

def test_inflate_travel_noop_when_zero():
    m = [[0, 10], [10, 0]]
    out = inflate_travel(m, 0, 0)
    assert out is m            # untouched object — cheap no-op default

def test_inflate_travel_pct_and_floor():
    m = [[0, 10, 5], [10, 0, 3], [5, 3, 0]]
    out = inflate_travel(m, 20, 2)   # ceil(v*1.2) + 2
    assert out[0][1] == 14           # ceil(12)+2
    assert out[1][2] == 6            # ceil(3.6)=4, +2
    assert out[0][2] == 8            # ceil(6)+2

def test_inflate_travel_rounds_up():
    # a 1-minute leg at +5% rounds up (never truncates contingency away)
    assert inflate_travel([[0, 1], [1, 0]], 5, 0)[0][1] == 2   # ceil(1.05)=2

def test_inflate_travel_leaves_zero_and_unreachable():
    m = [[0, UNREACHABLE], [UNREACHABLE, 0]]
    out = inflate_travel(m, 50, 9)
    assert out[0][0] == 0                 # co-located/self leg never padded
    assert out[0][1] == UNREACHABLE       # dead arc never revived
    assert out is not m                   # but a copy is returned when knobs are set
