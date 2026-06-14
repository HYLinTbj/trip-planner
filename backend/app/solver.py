"""Step 2–4: the scheduling core — Tourist Trip Design Problem via OR-Tools.

Days are modeled as vehicles that start and end at the base (hotel). Opening
hours become time windows, dwell time becomes service time, and importance
becomes a drop-penalty so low-value POIs are shed when a day can't hold
everything.

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
    opening hours and the day. high < low means it can't fit at all."""
    if poi.hours:
        h = poi.hours.get("default") or next(iter(poi.hours.values()))
        open_m, close_m = hhmm_to_min(h.open), hhmm_to_min(h.close)
    else:
        open_m, close_m = day_start, day_end
    low = max(open_m, day_start)
    high = min(close_m, day_end) - poi.dwell_min
    return low, high


def _infeasible(reason: str, auto_dropped: list[str], day_start: int, day_end: int) -> dict:
    return {
        "feasible": False, "reason": reason,
        "days": [], "dropped": [], "auto_dropped": auto_dropped,
        "total_travel_min": 0, "day_start": day_start, "day_end": day_end,
    }


def plan_trip(
    pois: list[POI],
    matrix_min: list[list[int]],
    num_days: int,
    day_start: int,
    day_end: int,
    time_limit_s: int = 5,
    balance: int = 0,
    locks: list[Lock] | None = None,
) -> dict:
    """matrix_min: (N+1)x(N+1) integer-minute matrix; index 0 = base, then pois."""
    locks = locks or []
    excluded = {lk.poi_id for lk in locks if lk.type == "exclude"}
    day_of = {lk.poi_id: lk.day for lk in locks if lk.type in ("day", "pin") and lk.day is not None}
    pin_of = {lk.poi_id: hhmm_to_min(lk.time) for lk in locks if lk.type == "pin" and lk.time}
    mandatory = {lk.poi_id for lk in locks if lk.type in ("day", "include", "pin")} - excluded

    windows = {id(p): _window(p, day_start, day_end) for p in pois}
    active = [p for p in pois if p.id not in excluded and windows[id(p)][1] >= windows[id(p)][0]]
    auto_dropped = [p.id for p in pois if p.id not in excluded and windows[id(p)][1] < windows[id(p)][0]]
    node_of = {p.id: k + 1 for k, p in enumerate(active)}

    missing = [pid for pid in mandatory if pid not in node_of]
    if missing:
        return _infeasible(
            "Can't include locked stop(s) that are excluded or can't fit their hours: "
            + ", ".join(missing),
            auto_dropped, day_start, day_end,
        )

    # A pinned arrival time must land inside the POI's feasible window, else
    # SetRange would throw on an out-of-domain value. Fail gracefully instead.
    active_by_id = {p.id: p for p in active}
    bad_pins = [
        pid for pid, t in pin_of.items()
        if not (windows[id(active_by_id[pid])][0] <= t <= windows[id(active_by_id[pid])][1])
    ]
    if bad_pins:
        return _infeasible(
            "Pinned arrival time is outside the day or opening hours for: " + ", ".join(bad_pins),
            auto_dropped, day_start, day_end,
        )

    orig = {id(p): k for k, p in enumerate(pois)}
    idxs = [0] + [orig[id(p)] + 1 for p in active]
    M = [[matrix_min[i][j] for j in idxs] for i in idxs]
    n = len(active)

    manager = pywrapcp.RoutingIndexManager(n + 1, num_days, 0)
    routing = pywrapcp.RoutingModel(manager)
    solver = routing.solver()

    def travel(from_i, to_i):
        return M[manager.IndexToNode(from_i)][manager.IndexToNode(to_i)]

    travel_cb = routing.RegisterTransitCallback(travel)
    routing.SetArcCostEvaluatorOfAllVehicles(travel_cb)  # objective: minimize travel

    def dwell(node):
        return 0 if node == 0 else active[node - 1].dwell_min

    def time_cb(from_i, to_i):
        f = manager.IndexToNode(from_i)
        return dwell(f) + M[f][manager.IndexToNode(to_i)]

    time_idx = routing.RegisterTransitCallback(time_cb)
    routing.AddDimension(time_idx, day_end - day_start, day_end, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    if balance and num_days > 1:
        # Balance the *number of stops* across days, so we don't get a marathon
        # day next to a free day. Done on a separate count dimension: putting a
        # span cost on the shared wall-clock Time dimension instead would just
        # squash every day into the same hours (huge idle waits).
        def unit(from_i):
            return 0 if manager.IndexToNode(from_i) == 0 else 1

        unit_idx = routing.RegisterUnaryTransitCallback(unit)
        routing.AddDimension(unit_idx, 0, n, True, "Count")
        routing.GetDimensionOrDie("Count").SetGlobalSpanCostCoefficient(balance)

    for v in range(num_days):
        time_dim.CumulVar(routing.Start(v)).SetRange(day_start, day_end)

    for node in range(1, n + 1):
        poi = active[node - 1]
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
            auto_dropped, day_start, day_end,
        )

    days = []
    visited = set()
    total_travel = 0
    for v in range(num_days):
        idx = routing.Start(v)
        stops = []
        prev_node, day_travel = 0, 0
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                poi = active[node - 1]
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
        day_travel += M[prev_node][0]  # leg back to base
        total_travel += day_travel
        days.append({
            "stops": stops,
            "return_min": sol.Value(time_dim.CumulVar(routing.End(v))),
            "travel_min": day_travel,
        })

    dropped = [active[node - 1].id for node in range(1, n + 1) if node not in visited]
    return {
        "feasible": True, "reason": None,
        "days": days, "dropped": dropped, "auto_dropped": auto_dropped,
        "total_travel_min": total_travel, "day_start": day_start, "day_end": day_end,
    }
