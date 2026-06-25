"""Unit tests for the OR-Tools TTDP core (solver.plan_trip + helpers).

plan_trip takes per-day (start, end) anchors and per-day time windows (HYL-69). Most
cases here are **single-base** (every anchor co-located at the base, built by `base_line`)
with **uniform** windows — that's the common path. The `test_route_*` cases exercise
distinct per-day start/end anchors (HYL-68); the `test_per_day_*` cases exercise distinct
per-day windows (HYL-69).

Tiny hand-built matrices solve instantly, so every plan uses time_limit_s=1. Days run
09:00-19:00 (540-1140 minutes from midnight) unless a case needs otherwise.
"""

from app.matrix import inflate_travel
from app.models import Lock
from app.solver import _window, hhmm_to_min, min_to_hhmm, plan_trip
from tests.conftest import base_line, make_poi, matrix_from_positions, uniform_windows

DAY_START, DAY_END = 540, 1140  # 09:00 - 19:00


# --- time helpers ------------------------------------------------------------

def test_hhmm_to_min():
    assert hhmm_to_min("00:00") == 0
    assert hhmm_to_min("09:30") == 570
    assert hhmm_to_min("19:00") == 1140


def test_min_to_hhmm():
    assert min_to_hhmm(0) == "00:00"
    assert min_to_hhmm(570) == "09:30"
    assert min_to_hhmm(1140) == "19:00"


def test_hhmm_round_trip():
    for s in ("00:00", "07:05", "12:00", "23:59"):
        assert min_to_hhmm(hhmm_to_min(s)) == s


# --- _window -----------------------------------------------------------------

def test_window_no_hours_uses_day_bounds():
    low, high = _window(make_poi("a", dwell_min=60), DAY_START, DAY_END)
    assert low == 540
    assert high == 1140 - 60


def test_window_with_hours_clamps_to_opening():
    p = make_poi("a", dwell_min=30, hours={"default": {"open": "10:00", "close": "16:00"}})
    low, high = _window(p, DAY_START, DAY_END)
    assert low == 600                 # max(open 600, day 540)
    assert high == 960 - 30           # min(close 960, day 1140) - dwell


def test_window_infeasible_when_dwell_exceeds_opening_span():
    p = make_poi("a", dwell_min=60, hours={"default": {"open": "09:00", "close": "09:10"}})
    low, high = _window(p, DAY_START, DAY_END)
    assert high < low                 # signals "can't fit" to plan_trip


# --- basic solve (single base, uniform windows) ------------------------------

def test_basic_feasible_visits_all():
    pois = [make_poi(x, dwell_min=30) for x in ("a", "b", "c")]
    anchors, win, m = base_line(1, [1, 2, 3])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1)

    assert res["feasible"] is True
    assert res["reason"] is None
    assert res["dropped"] == [] and res["auto_dropped"] == []
    visited = {s["poi_id"] for d in res["days"] for s in d["stops"]}
    assert visited == {"a", "b", "c"}
    assert res["total_travel_min"] > 0
    for d in res["days"]:
        assert "travel_min" in d
        for s in d["stops"]:
            assert s["departure"] == s["arrival"] + s["dwell"]


def test_result_has_no_top_level_day_window():
    # HYL-69: per-day windows live on each day (attached by main), not a single envelope.
    pois = [make_poi("a", dwell_min=30)]
    anchors, win, m = base_line(1, [1])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1)
    assert "day_start" not in res and "day_end" not in res


def test_drop_penalty_sheds_lowest_importance():
    # 3 * 250-min dwells can't share one 10h day -> the cheapest (low importance) is shed.
    pois = [
        make_poi("a", importance=0.9, dwell_min=250),
        make_poi("b", importance=0.9, dwell_min=250),
        make_poi("c", importance=0.1, dwell_min=250),
    ]
    anchors, win, m = base_line(1, [1, 2, 3])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1)
    assert res["feasible"] is True
    assert res["dropped"] == ["c"]
    visited = {s["poi_id"] for d in res["days"] for s in d["stops"]}
    assert visited == {"a", "b"}


def test_auto_dropped_when_opening_too_short():
    pois = [
        make_poi("ok", dwell_min=30),
        make_poi("tiny", dwell_min=60, hours={"default": {"open": "09:00", "close": "09:10"}}),
    ]
    anchors, win, m = base_line(1, [1, 2])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1)
    assert res["feasible"] is True
    assert res["auto_dropped"] == ["tiny"]
    visited = {s["poi_id"] for d in res["days"] for s in d["stops"]}
    assert visited == {"ok"}


# --- locks -------------------------------------------------------------------

def test_exclude_lock_removes_from_pool():
    pois = [make_poi("a", dwell_min=30), make_poi("b", dwell_min=30)]
    anchors, win, m = base_line(1, [1, 2])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1,
                    locks=[Lock(poi_id="b", type="exclude")])
    assert res["feasible"] is True
    visited = {s["poi_id"] for d in res["days"] for s in d["stops"]}
    assert visited == {"a"}
    # excluded entirely: not even reported as dropped/auto_dropped
    assert "b" not in res["dropped"]
    assert "b" not in res["auto_dropped"]


def test_include_lock_forces_low_importance_visit():
    pois = [
        make_poi("a", importance=0.9, dwell_min=250),
        make_poi("b", importance=0.9, dwell_min=250),
        make_poi("c", importance=0.1, dwell_min=250),
    ]
    anchors, win, m = base_line(1, [1, 2, 3])
    # baseline: c is shed
    assert plan_trip(pois, m, anchors, win, time_limit_s=1)["dropped"] == ["c"]
    # include forces c in (one of the high-value POIs is shed instead)
    res = plan_trip(pois, m, anchors, win, time_limit_s=1,
                    locks=[Lock(poi_id="c", type="include")])
    assert res["feasible"] is True
    visited = {s["poi_id"] for d in res["days"] for s in d["stops"]}
    assert "c" in visited
    assert "c" not in res["dropped"]


def test_day_lock_pins_to_day():
    pois = [make_poi(x, dwell_min=30) for x in ("a", "b", "c", "d")]
    anchors, win, m = base_line(2, [1, 2, 3, 4])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1,
                    locks=[Lock(poi_id="a", type="day", day=1)])
    assert res["feasible"] is True
    day_of = {s["poi_id"]: di for di, d in enumerate(res["days"]) for s in d["stops"]}
    assert day_of["a"] == 1           # forced onto the second day (0-based)


def test_pin_lock_fixes_arrival():
    pois = [make_poi("a", dwell_min=30), make_poi("b", dwell_min=30)]
    anchors, win, m = base_line(1, [1, 2])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1,
                    locks=[Lock(poi_id="a", type="pin", day=0, time="11:00")])
    assert res["feasible"] is True
    stop_a = next(s for d in res["days"] for s in d["stops"] if s["poi_id"] == "a")
    assert stop_a["arrival"] == hhmm_to_min("11:00")


# --- graceful infeasibility --------------------------------------------------

def test_infeasible_include_unfittable_poi():
    pois = [
        make_poi("ok", dwell_min=30),
        make_poi("tiny", dwell_min=60, hours={"default": {"open": "09:00", "close": "09:10"}}),
    ]
    anchors, win, m = base_line(1, [1, 2])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1,
                    locks=[Lock(poi_id="tiny", type="include")])
    assert res["feasible"] is False
    assert "tiny" in res["reason"]
    assert res["days"] == []
    assert "tiny" in res["auto_dropped"]   # still reported in the infeasible payload


def test_infeasible_pin_outside_window():
    pois = [make_poi("a", dwell_min=30)]
    anchors, win, m = base_line(1, [1])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1,
                    locks=[Lock(poi_id="a", type="pin", day=0, time="05:00")])
    assert res["feasible"] is False
    assert "a" in res["reason"]
    assert res["days"] == []


def test_pin_and_exclude_on_same_poi_is_graceful():
    # Contradictory locks: 'a' is both pinned and excluded. exclude wins (it's dropped from the
    # pool and not mandatory), so the pin has nothing to enforce. This must NOT KeyError on the
    # pin pre-check — the solve proceeds and simply omits 'a' (reachable via the API/MCP, which,
    # unlike the web UI, can attach two contradictory locks to one POI).
    pois = [make_poi("a", dwell_min=30), make_poi("b", dwell_min=30)]
    anchors, win, m = base_line(1, [1, 2])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1,
                    locks=[Lock(poi_id="a", type="pin", day=0, time="11:00"),
                           Lock(poi_id="a", type="exclude")])
    assert res["feasible"] is True
    visited = {s["poi_id"] for d in res["days"] for s in d["stops"]}
    assert "a" not in visited and "b" in visited


# --- balance -----------------------------------------------------------------

def test_balance_avoids_a_dead_day():
    pois = [make_poi(x, dwell_min=60) for x in ("a", "b", "c", "d")]
    anchors, win, m = base_line(2, [1, 2, 3, 4])
    res = plan_trip(pois, m, anchors, win, balance=100, time_limit_s=1)
    assert res["feasible"] is True
    counts = [len(d["stops"]) for d in res["days"]]
    assert sum(counts) == 4
    assert min(counts) >= 1           # balance keeps neither day empty


# --- HYL-68: per-day start/end anchors (route mode) --------------------------

def test_route_two_legs_assigns_pois_by_proximity():
    # Day 0: A(0) -> B(10).  Day 1: B(10) -> C(20).  Each POI sits on one leg's path.
    pois = [make_poi("pa", dwell_min=30), make_poi("pc", dwell_min=30)]
    positions = [0, 10, 10, 20] + [5, 15]   # 2 anchors/day; pa@5 (A-B), pc@15 (B-C)
    m = matrix_from_positions(positions, gap=10)
    res = plan_trip(pois, m, [(0, 1), (2, 3)], uniform_windows(2), time_limit_s=1)

    assert res["feasible"] is True
    day_of = {s["poi_id"]: di for di, d in enumerate(res["days"]) for s in d["stops"]}
    assert day_of == {"pa": 0, "pc": 1}     # each picked up on the nearest leg


def test_route_day_with_no_pois_is_direct_leg():
    # One day from A(0) to C(20), no POIs -> the day is just the direct anchor-to-anchor leg.
    m = matrix_from_positions([0, 20], gap=10)
    res = plan_trip([], m, [(0, 1)], uniform_windows(1), time_limit_s=1)
    assert res["feasible"] is True
    assert res["days"][0]["stops"] == []
    assert res["days"][0]["travel_min"] == 200    # |0-20| * 10


def test_no_day_anchors_is_gracefully_infeasible():
    # No days at all -> a graceful feasible:false, not a ValueError from max() of an empty seq.
    res = plan_trip([make_poi("a")], [], [], [], time_limit_s=1)
    assert res["feasible"] is False
    assert res["days"] == []


# --- HYL-69: per-day time windows --------------------------------------------

def test_per_day_poi_lands_on_the_day_whose_window_fits():
    # "m" opens 10:00-12:00 (dwell 60): fits day 0 (09-19) but not day 1 (14-18).
    pois = [make_poi("m", dwell_min=60, hours={"default": {"open": "10:00", "close": "12:00"}})]
    anchors, _, m = base_line(2, [1])
    res = plan_trip(pois, m, anchors, [(540, 1140), (840, 1080)], time_limit_s=1)
    assert res["feasible"] is True
    day_of = {s["poi_id"]: di for di, d in enumerate(res["days"]) for s in d["stops"]}
    assert day_of["m"] == 0


def test_per_day_poi_kept_when_it_fits_only_one_day():
    # A 2h-dwell POI (no hours) fits day 0's long window but not day 1's 1-hour window —
    # it must NOT be auto-dropped (it fits a day), and lands on day 0.
    pois = [make_poi("big", dwell_min=120)]
    anchors, _, m = base_line(2, [1])
    res = plan_trip(pois, m, anchors, [(540, 1140), (540, 600)], time_limit_s=1)
    assert res["feasible"] is True
    assert res["auto_dropped"] == []
    day_of = {s["poi_id"]: di for di, d in enumerate(res["days"]) for s in d["stops"]}
    assert day_of["big"] == 0


def test_per_day_auto_dropped_only_when_no_day_fits():
    # A 3h-dwell POI fits neither a 2h day nor a 1h day -> auto-dropped (no day holds it).
    pois = [make_poi("huge", dwell_min=180)]
    anchors, _, m = base_line(2, [1])
    res = plan_trip(pois, m, anchors, [(540, 660), (540, 600)], time_limit_s=1)
    assert res["feasible"] is True
    assert res["auto_dropped"] == ["huge"]


def test_per_day_day_lock_to_a_day_that_cant_fit_is_infeasible():
    # "big" (2h dwell) locked to day 1, which is only 09:00-10:00 -> graceful infeasible.
    pois = [make_poi("big", dwell_min=120), make_poi("ok", dwell_min=30)]
    anchors, _, m = base_line(2, [1, 2])
    res = plan_trip(pois, m, anchors, [(540, 1140), (540, 600)], time_limit_s=1,
                    locks=[Lock(poi_id="big", type="day", day=1)])
    assert res["feasible"] is False
    assert "big" in res["reason"]
    assert "big" in res["auto_dropped"]


def test_per_day_pin_valid_on_its_locked_day_despite_other_windows():
    # Pin "a" to 13:00 on day 0 (open all day). Day 1 is 09:00-11:00 — 13:00 is outside it,
    # but a pin only has to fit its own day, so the solve stays feasible.
    pois = [make_poi("a", dwell_min=30), make_poi("b", dwell_min=30)]
    anchors, _, m = base_line(2, [1, 2])
    res = plan_trip(pois, m, anchors, [(540, 1140), (540, 660)], time_limit_s=1,
                    locks=[Lock(poi_id="a", type="pin", day=0, time="13:00")])
    assert res["feasible"] is True
    stop_a = next(s for d in res["days"] for s in d["stops"] if s["poi_id"] == "a")
    assert stop_a["arrival"] == hhmm_to_min("13:00")


def test_per_day_pin_without_day_lock_in_window_gap_is_infeasible():
    # No day lock, so the pin may land on any day. Day 0 is 09:00-12:00 and day 1 is
    # 15:00-18:00 — a 13:00 pin fits the union (09:00-18:00) but neither single day's
    # window. It must fail gracefully with the *pin-specific* reason, not the generic
    # "couldn't fit all locked stops" one (HYL-85).
    pois = [make_poi("a", dwell_min=30), make_poi("b", dwell_min=30)]
    anchors, _, m = base_line(2, [1, 2])
    res = plan_trip(pois, m, anchors, [(540, 720), (900, 1080)], time_limit_s=1,
                    locks=[Lock(poi_id="a", type="pin", time="13:00")])
    assert res["feasible"] is False
    assert "Pinned arrival time is outside" in res["reason"]
    assert "a" in res["reason"]


def test_per_day_pin_without_day_lock_fits_one_day():
    # Same gapped windows, but a 16:00 pin lands inside day 1's 15:00-18:00 window — so a
    # day-less pin that fits *some* day stays feasible and arrives at the pinned time.
    pois = [make_poi("a", dwell_min=30), make_poi("b", dwell_min=30)]
    anchors, _, m = base_line(2, [1, 2])
    res = plan_trip(pois, m, anchors, [(540, 720), (900, 1080)], time_limit_s=1,
                    locks=[Lock(poi_id="a", type="pin", time="16:00")])
    assert res["feasible"] is True
    stop_a = next(s for d in res["days"] for s in d["stops"] if s["poi_id"] == "a")
    assert stop_a["arrival"] == hhmm_to_min("16:00")


def test_per_day_short_day_caps_the_return_time():
    # Day 1's 09:00-11:00 window must bound that day's end cumul (return) at <= 11:00.
    pois = [make_poi(x, dwell_min=30) for x in ("a", "b", "c")]
    anchors, _, m = base_line(2, [1, 2, 3])
    res = plan_trip(pois, m, anchors, [(540, 1140), (540, 660)], time_limit_s=1)
    assert res["feasible"] is True
    assert res["days"][1]["return_min"] <= 660


# --- HYL-72 per-stop contingency buffer --------------------------------------

def _arrivals(res):
    return {s["poi_id"]: s["arrival"] for d in res["days"] for s in d["stops"]}

def test_stop_buffer_delays_later_stops_but_not_the_visit_duration():
    # Two co-linear POIs visited a->b from the base. A per-stop buffer reserves time AFTER
    # each visit, so the *second* stop's arrival shifts by the buffer while the first is
    # unchanged; the visit duration (departure - arrival == dwell) never changes.
    pois = [make_poi(x, dwell_min=30) for x in ("a", "b")]
    anchors, win, m = base_line(1, [1, 2])

    base = plan_trip(pois, m, anchors, win, time_limit_s=1)
    padded = plan_trip(pois, m, anchors, win, time_limit_s=1, stop_buffer_min=15)

    a0, a1 = _arrivals(base), _arrivals(padded)
    assert a1["a"] == a0["a"]            # first stop: nothing reserved before it
    assert a1["b"] == a0["b"] + 15       # buffer after 'a' pushes 'b' later by exactly 15
    for d in padded["days"]:
        for s in d["stops"]:
            assert s["departure"] == s["arrival"] + s["dwell"]   # visit length unchanged

def test_stop_buffer_does_not_shrink_opening_window():
    # 'm' fits its 09:00-10:00 window exactly (dwell 60, co-located with base so arrival=540).
    # A large per-stop buffer reserves time *after* the visit but must NOT tighten the window
    # — unlike bumping dwell, which would auto-drop it. So 'm' is still visited.
    pois = [make_poi("m", dwell_min=60, hours={"default": {"open": "09:00", "close": "10:00"}})]
    anchors, win, m = base_line(1, [0])   # POI co-located with the base: zero travel
    res = plan_trip(pois, m, anchors, win, time_limit_s=1, stop_buffer_min=120)
    assert res["feasible"] is True
    assert _arrivals(res) == {"m": 540}   # still pinned to its only feasible arrival
    assert res["dropped"] == [] and res["auto_dropped"] == []


# --- travel-buffer reporting (HYL-92) ----------------------------------------

def test_travel_buffer_reported_apart_from_real_travel():
    # Base trip: base(0) -> a(2) -> base(0), a one-way leg of 20 min each way (40 round trip).
    # Solving on a +50% inflated matrix still reserves the slack, but travel_min reports the
    # raw 40 and buffer_min reports the 20-min padding (half of 40) as its own number.
    pois = [make_poi("a", dwell_min=30)]
    anchors, win, raw = base_line(1, [2])         # |0-2|*10 = 20 per leg
    inflated = inflate_travel(raw, pct=50)        # each 20-min leg -> 30 (buffer 10 each)
    res = plan_trip(pois, inflated, anchors, win, time_limit_s=1, raw_matrix_min=raw)
    assert res["feasible"] is True
    day = res["days"][0]
    assert day["travel_min"] == 40                # real round-trip road time, unpadded
    assert day["buffer_min"] == 20                # the reserved contingency, kept separate
    assert res["total_travel_min"] == 40 and res["total_buffer_min"] == 20
    assert day["stops"][0]["travel_in"] == 20 and day["stops"][0]["buffer_in"] == 10


def test_no_buffer_when_raw_matrix_omitted():
    # Backward-compatible default: without a raw matrix, there's no padding to report.
    pois = [make_poi("a", dwell_min=30)]
    anchors, win, m = base_line(1, [2])
    res = plan_trip(pois, m, anchors, win, time_limit_s=1)
    assert res["days"][0]["buffer_min"] == 0
    assert res["total_buffer_min"] == 0
    assert res["days"][0]["stops"][0]["buffer_in"] == 0
