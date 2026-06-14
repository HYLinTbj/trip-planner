#!/usr/bin/env python3
"""Spike check: confirm the OSRM data path end to end.

Prints an N x N travel-time matrix (minutes) between the sample POIs.

Quick test with NO Docker (public demo server, driving profile):
    OSRM_URL=https://router.project-osrm.org OSRM_PROFILE=driving \
        python scripts/table_matrix.py

Against self-hosted OSRM (after `docker compose up -d`):
    python scripts/table_matrix.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.engine import DEFAULT_PROFILE, ROUTING_ENGINE, base_url, table_durations, to_minutes  # noqa: E402
from app.store import load_pois  # noqa: E402

POIS_PATH = ROOT / "data" / "pois.sample.json"


def main() -> None:
    pois = load_pois(POIS_PATH)
    ids = list(pois.keys())
    coords = [(pois[i].lat, pois[i].lon) for i in ids]
    names = [pois[i].name for i in ids]

    print(f"engine:  {ROUTING_ENGINE} {base_url(DEFAULT_PROFILE)}  (profile: {DEFAULT_PROFILE})")
    print(f"Querying {len(ids)} POIs ...\n")

    durations = table_durations(coords)
    mins = to_minutes(durations)

    label_w = max(len(n) for n in names)
    header = " " * (label_w + 3) + "".join(f"{j:>7}" for j in range(len(ids)))
    print(header)
    for i, name in enumerate(names):
        cells = "".join(
            f"{mins[i][j]:>7}" if mins[i][j] is not None else f"{'-':>7}"
            for j in range(len(ids))
        )
        print(f"{name:<{label_w}}  {i}{cells}")

    print("\nLegend:")
    for j, name in enumerate(names):
        print(f"  [{j}] {name}")
    print("\nValues are minutes. Sanity check: Arashiyama (far west) and")
    print("Fushimi Inari (south) should read noticeably farther from the rest.")


if __name__ == "__main__":
    main()
