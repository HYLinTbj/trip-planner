"""Step 2–4: the scheduling core — Tourist Trip Design Problem via OR-Tools.

Each day is a vehicle with its own start and end *anchor* (HYL-68) and its own
*time window* (HYL-69): day i runs from anchor start_i to anchor end_i, between
day_windows[i] = (open_min, close_min), picking up POIs along the way. Opening
hours become time windows, dwell time becomes service time, and importance
becomes a drop-penalty so low-value POIs are shed when a day can't hold
everything. A single-base trip is the special case where every anchor shares the
base's coordinates; a same-hours-every-day trip is the special case where every
day repeats one window.

Step 4 adds *locks* — the user's edits become hard constraints, then we
re-solve around them:
  - exclude : drop the POI from the candidate pool entirely
  - include : the POI must be visited (any day); exempt from the drop-penalty
  - day     : the POI must be visited on a specific day
  - pin     : the POI must be visited on a specific day at a fixed arrival time

Simplifying assumption (fine for the MVP): a POI's opening hours are constant
across the trip's days (uses the "default" entry).
"""

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .models import POI, Lock

IMPORTANCE_SCALE = 100_000  # makes dropping a last resort vs. saving travel minutes


def hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def min_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _window(poi: POI, day_start: int, day_end: int) -> tuple[int, int]:
    """Arrival-time window [low, high] so the visit fits within both the POI's
    opening hours and the given day bounds. high < low means it can't fit at all."""
    if poi.hours:
        h = poi.hours.get("default") or next(iter(poi.hours.values()))
        open_m, close_m = hhmm_to_min(h.open), hhmm_to_min(h.close)
    else:
        open_m, close_m = day_start, day_end
    low = max(open_m, day_start)
    high = min(close_m, day_end) - poi.dwell_min
    return low, high


def _infeasible(reason: str, auto_dropped: list[str]) -> dict:
    return {
        "feasible": False, "reason": reason,
        "days": [], "dropped": [], "auto_dropped": auto_dropped,
        "total_travel_min": 0,
    }


def plan_trip(
    pois: list[POI],
    matrix_min: list[list[int]],
    day_anchors: list[tuple[int, int]],
    day_windows: list[tuple[int, int]],
    time_limit_s: int = 5,
    balance: int = 0,
    locks: list[Lock] | None = None,
) -> dict:
    """Solve an itinerary over per-day (start, end) anchors with per-day time windows.

    matrix_min: square integer-minute matrix over [anchor nodes…, *pois] — anchor
      nodes occupy indices 0..A-1, POIs occupy A..A+len(pois)-1.
    day_anchors: one (start_node, end_node) index pair per day (num_days =
      len(day_anchors)); every value indexes an anchor node. OR-Tools requires the
      start/end node indices to be distinct, so co-located anchors (a single base, or
      last night's hotel == this morning's start) are passed as distinct nodes that
      happen to share coordinates. A single-base trip = every anchor at the base.
    day_windows: one (open_min, close_min) pair per day, aligned 1:1 with day_anchors
      (minutes from midnight). Days may differ (HYL-69); a uniform trip just repeats one
      window. The Time dimension's horizon spans min(open)..max(close), and each day's
      own open/close is enforced on that day's start/end cumul — so a POI placed on a
      short day still can't overrun it (no per-day POI windows needed).
    """
    locks = locks or []
    if not day_anchors:
        return _infeasible("A trip needs at least one day.", [])
    num_days = len(day_anchors)
    n_anchor = 1 + max(max(s, e) for s, e in day_anchors)   # anchors occupy 0..n_anchor-1
    min_start = min(ds for ds, _ in day_windows)
    max_end = max(de for _, de in day_windows)

    excluded = {lk.poi_id for lk in locks if lk.type == "exclude"}
    day_of = {lk.poi_id: lk.day for lk in locks if lk.type in ("day", "pin") and lk.day is not None}
    pin_of = {lk.poi_id: hhmm_to_min(lk.time) for lk in locks if lk.type == "pin" and lk.time}
    mandatory = {lk.poi_id for lk in locks if lk.type in ("day", "include", "pin")} - excluded

    def candidate_days(poi_id: str):
        """The day(s) a POI may land on: just its locked day (a day/pin lock with a valid
        index), otherwise any day."""
        d = day_of.get(poi_id)
        if d is not None and 0 <= d < num_days:
            return [d]
        return range(num_days)

    def fits_any(poi: POI) -> bool:
        """True if the POI's hours fit within at least one of its candidate days' windows."""
        for d in candidate_days(poi.id):
            low, high = _window(poi, *day_windows[d])
            if high >= low:
                return True
        return False

    # A POI is schedulable if it fits at least one day it's allowed on; a day-locked POI
    # that can't fit *its* day is reported (auto_dropped) and, if mandatory, fails below.
    active, auto_dropped = [], []
    for p in pois:
        if p.id in excluded:
            continue
        (active if fits_any(p) else auto_dropped).append(p)
    auto_dropped = [p.id for p in auto_dropped]
    active_ids = {p.id for p in active}

    missing = [pid for pid in mandatory if pid not in active_ids]
    if missing:
        return _infeasible(
            "Can't include locked stop(s) that are excluded or can't fit their hours: "
            + ", ".join(missing),
            auto_dropped,
        )

    # A pinned arrival time must land inside the POI's window *on its pinned day*, else
    # SetRange would throw on an out-of-domain value. Fail gracefully instead.
    active_by_id = {p.id: p for p in active}
    bad_pins = []
    for pid, t in pin_of.items():
        d = day_of.get(pid)
        dw = day_windows[d] if (d is not None and 0 <= d < num_days) else (min_start, max_end)
        low, high = _window(active_by_id[pid], *dw)
        if not (low <= t <= high):
            bad_pins.append(pid)
    if bad_pins:
        return _infeasible(
            "Pinned arrival time is outside the day or opening hours for: " + ", ".join(bad_pins),
            auto_dropped,
        )

    # Subset the matrix to [all anchor nodes] + [active POI nodes]; in the local matrix M
    # anchors keep indices 0..n_anchor-1 and active POIs follow at n_anchor.. .
    orig = {id(p): k for k, p in enumerate(pois)}
    idxs = list(range(n_anchor)) + [n_anchor + orig[id(p)] for p in active]
    M = [[matrix_min[i][j] for j in idxs] for i in idxs]
    n = len(active)

    # POI cumul domain: the union window across the whole horizon. Each day's tighter
    # close is enforced by that day's end cumul below, so this can stay loose.
    windows = {id(p): _window(p, min_start, max_end) for p in active}

    starts = [s for s, _ in day_anchors]
    ends = [e for _, e in day_anchors]
    manager = pywrapcp.RoutingIndexManager(n_anchor + n, num_days, starts, ends)
    routing = pywrapcp.RoutingModel(manager)
    solver = routing.solver()

    def travel(from_i, to_i):
        return M[manager.IndexToNode(from_i)][manager.IndexToNode(to_i)]

    travel_cb = routing.RegisterTransitCallback(travel)
    routing.SetArcCostEvaluatorOfAllVehicles(travel_cb)  # objective: minimize travel

    def dwell(node):
        return 0 if node < n_anchor else active[node - n_anchor].dwell_min

    def time_cb(from_i, to_i):
        f = manager.IndexToNode(from_i)
        return dwell(f) + M[f][manager.IndexToNode(to_i)]

    time_idx = routing.RegisterTransitCallback(time_cb)
    # Horizon spans the widest day; each day's own open/close is set on its start/end
    # cumul below (the single dimension capacity can't express per-day closes).
    routing.AddDimension(time_idx, max_end - min_start, max_end, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    if balance and num_days > 1:
        # Balance the *number of stops* across days, so we don't get a marathon
        # day next to a free day. Done on a separate count dimension: putting a
        # span cost on the shared wall-clock Time dimension instead would just
        # squash every day into the same hours (huge idle waits).
        def unit(from_i):
            return 0 if manager.IndexToNode(from_i) < n_anchor else 1

        unit_idx = routing.RegisterUnaryTransitCallback(unit)
        routing.AddDimension(unit_idx, 0, n, True, "Count")
        routing.GetDimensionOrDie("Count").SetGlobalSpanCostCoefficient(balance)

    for v in range(num_days):
        ds_v, de_v = day_windows[v]
        # This day opens at ds_v and must be wrapped up by de_v — enforced on both its
        # start and end cumul (the end bound is what closes a short day).
        time_dim.CumulVar(routing.Start(v)).SetRange(ds_v, de_v)
        time_dim.CumulVar(routing.End(v)).SetRange(ds_v, de_v)

    for k, poi in enumerate(active):
        node = n_anchor + k
        index = manager.NodeToIndex(node)
        low, high = windows[id(poi)]
        time_dim.CumulVar(index).SetRange(low, high)

        if poi.id not in mandatory:  # locked-in POIs must be visited → no disjunction
            routing.AddDisjunction([index], int(poi.importance * IMPORTANCE_SCALE))
        if poi.id in pin_of:         # fixed arrival time (a reservation)
            time_dim.CumulVar(index).SetRange(pin_of[poi.id], pin_of[poi.id])
        day = day_of.get(poi.id)     # pin to a specific day
        if day is not None and 0 <= day < num_days:
            solver.Add(routing.VehicleVar(index) == day)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(time_limit_s)

    sol = routing.SolveWithParameters(params)
    if sol is None:
        return _infeasible(
            "Couldn't fit all locked stops within the day limits — relax a lock or add a day.",
            auto_dropped,
        )

    days = []
    visited = set()
    total_travel = 0
    for v in range(num_days):
        idx = routing.Start(v)
        stops = []
        prev_node = manager.IndexToNode(idx)   # the day's start anchor
        day_travel = 0
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node >= n_anchor:               # a POI (anchor depots are skipped)
                poi = active[node - n_anchor]
                visited.add(node)
                arr = sol.Value(time_dim.CumulVar(idx))
                leg = M[prev_node][node]
                day_travel += leg
                stops.append({
                    "poi_id": poi.id, "name": poi.name, "arrival": arr,
                    "departure": arr + poi.dwell_min, "dwell": poi.dwell_min,
                    "travel_in": leg,
                })
                prev_node = node
            idx = sol.Value(routing.NextVar(idx))
        end_node = manager.IndexToNode(routing.End(v))
        day_travel += M[prev_node][end_node]   # leg to the day's end anchor
        total_travel += day_travel
        days.append({
            "stops": stops,
            "return_min": sol.Value(time_dim.CumulVar(routing.End(v))),
            "travel_min": day_travel,
        })

    dropped = [active[node - n_anchor].id
               for node in range(n_anchor, n_anchor + n) if node not in visited]
    return {
        "feasible": True, "reason": None,
        "days": days, "dropped": dropped, "auto_dropped": auto_dropped,
        "total_travel_min": total_travel,
    }
