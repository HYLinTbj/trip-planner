#!/usr/bin/env python3
"""Routing-engine spike: compare Valhalla against the running OSRM, side by side.

Prereqs:
  - OSRM up:      docker compose up -d                                  (foot :5000, car :5001)
  - Valhalla up:  docker compose -f docker-compose.valhalla.yml up -d   (:8002)

For each mode, prints the OSRM vs Valhalla travel-time matrix over the sample POIs:
parity (mean/max % difference), a few concrete legs, and query timing. One Valhalla
instance answers every mode via `costing`; OSRM needs an instance per mode.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import osrm, valhalla   # noqa: E402
from app.store import load_pois  # noqa: E402

POIS = ROOT / "data" / "pois.sample.json"
BASE = (34.9858, 135.7588)  # Kyoto Station


def timed(fn, coords, profile):
    t = time.perf_counter()
    secs = fn(coords, profile=profile)
    return secs, (time.perf_counter() - t) * 1000


def compare(coords, names, profile):
    o_secs, o_ms = timed(osrm.table_durations, coords, profile)
    v_secs, v_ms = timed(valhalla.table_durations, coords, profile)
    print(f"\n=== {profile.upper()}   OSRM {osrm.url_for(profile)}  vs  "
          f"Valhalla :8002 (costing={valhalla.costing_for(profile)}) ===")
    print(f"  query time:  OSRM {o_ms:5.0f} ms    Valhalla {v_ms:5.0f} ms")
    diffs = [
        abs(v_secs[i][j] - o_secs[i][j]) / o_secs[i][j] * 100
        for i in range(len(coords)) for j in range(len(coords))
        if i != j and o_secs[i][j] and v_secs[i][j] and o_secs[i][j] > 0
    ]
    if diffs:
        print(f"  parity vs OSRM:  mean {sum(diffs)/len(diffs):4.0f}%   "
              f"max {max(diffs):4.0f}%   (over {len(diffs)} legs)")
    print("  sample legs from base:")
    for j in range(1, min(len(coords), 5)):
        o, v = o_secs[0][j], v_secs[0][j]
        om = f"{o/60:.0f}m" if o else "-"
        vm = f"{v/60:.0f}m" if v else "-"
        print(f"    -> {names[j]:30} OSRM {om:>5}   Valhalla {vm:>5}")


def main():
    pois = list(load_pois(POIS).values())
    coords = [BASE] + [(p.lat, p.lon) for p in pois]
    names = ["(base) Kyoto Station"] + [p.name for p in pois]
    print(f"Comparing over {len(coords)} points ({len(pois)} sample POIs + base).")
    for profile in ("foot", "car"):
        try:
            compare(coords, names, profile)
        except Exception as e:
            print(f"\n=== {profile.upper()} ===  ERROR: {e}")
    print("\nNote: one Valhalla instance (:8002) answered BOTH modes via `costing`;")
    print("      OSRM needed two instances (:5000 foot, :5001 car).")


if __name__ == "__main__":
    main()
