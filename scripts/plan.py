#!/usr/bin/env python3
"""Step 2 demo: build a feasible multi-day itinerary from the sample POIs.

Examples (defaults to the self-hosted foot OSRM at localhost:5000):
    python scripts/plan.py --days 2
    # Tighten the day to watch low-importance POIs get dropped:
    python scripts/plan.py --days 1 --end 14:00
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.matrix import get_matrix_min  # noqa: E402
from app.engine import DEFAULT_PROFILE, ROUTING_ENGINE, base_url  # noqa: E402
from app.solver import hhmm_to_min, min_to_hhmm, plan_trip  # noqa: E402
from app.store import load_pois  # noqa: E402

POIS = ROOT / "data" / "pois.json"               # live store (shared with the web app)
POIS_SEED = ROOT / "data" / "pois.sample.json"   # seed copied on first run
CACHE = ROOT / "data" / "matrix_cache.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--start", default="09:00")
    ap.add_argument("--end", default="19:00")
    ap.add_argument("--base-lat", type=float, default=34.9858)   # Kyoto Station
    ap.add_argument("--base-lon", type=float, default=135.7588)
    ap.add_argument("--time-limit", type=int, default=5)
    ap.add_argument("--balance", type=int, default=5, help="day-balancing weight; 0 = off")
    args = ap.parse_args()

    pois = list(load_pois(POIS, seed=POIS_SEED).values())
    coords = [(args.base_lat, args.base_lon)] + [(p.lat, p.lon) for p in pois]
    matrix = get_matrix_min(coords, profile=DEFAULT_PROFILE, cache_path=CACHE)

    ds, de = hhmm_to_min(args.start), hhmm_to_min(args.end)
    res = plan_trip(pois, matrix, args.days, ds, de, args.time_limit, balance=args.balance)

    print(f"\n{len(pois)} POIs · {args.days} day(s) · {args.start}–{args.end} · "
          f"base ({args.base_lat}, {args.base_lon})")
    print(f"engine: {ROUTING_ENGINE} {base_url(DEFAULT_PROFILE)} (profile: {DEFAULT_PROFILE})\n")

    for d, day in enumerate(res["days"], 1):
        print(f"── Day {d} " + "─" * 42)
        if not day["stops"]:
            print("   (free day)\n")
            continue
        print(f"   {min_to_hhmm(ds)}  ◦ leave base")
        for s in day["stops"]:
            print(f"   {min_to_hhmm(s['arrival'])}  ▸ {s['name']:<30} "
                  f"stay {s['dwell']:>3}m   (+{s['travel_in']}m travel)")
        print(f"   {min_to_hhmm(day['return_min'])}  ◦ back at base   "
              f"[{len(day['stops'])} stops · {day['travel_min']}m driving]\n")

    drop = res["dropped"] + res["auto_dropped"]
    if drop:
        by_id = {p.id: p for p in pois}
        print("Dropped (didn't fit):")
        for pid in sorted(drop, key=lambda x: -by_id[x].importance):
            print(f"   ✗ {by_id[pid].name:<30} importance {by_id[pid].importance}")
        print()
    print(f"Total driving: {res['total_travel_min']}m across {args.days} day(s)")


if __name__ == "__main__":
    main()
