#!/usr/bin/env python3
"""Seed Postgres from the legacy JSON stores (scale-up phase 1, one-shot, idempotent).

  data/cities.json        -> cities table
  data/pois.<city>.json   -> pois table (city becomes the `city_slug` column)

Re-runnable: rows are upserted by primary key via db.merge(), so re-running won't
duplicate. has_transit/transit_operator are derived from each city's gtfs[] feeds.

  .venv/bin/python scripts/migrate_json_to_db.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import models_db as m       # noqa: E402
from app.db import SessionLocal      # noqa: E402

DATA = ROOT / "data"

# City -> regional engine (phase 2). US census regions; kyoto is legacy/foreign.
REGION = {"boston": "northeast", "denver": "west", "kyoto": "kansai"}


def main() -> None:
    cities = json.loads((DATA / "cities.json").read_text())
    n_cities = n_pois = 0
    with SessionLocal() as db:
        for slug, c in cities.items():
            b = c["base"]
            gtfs = c.get("gtfs") or []
            db.merge(m.City(
                slug=slug,
                label=c.get("label", slug.title()),
                base_lat=b["lat"], base_lon=b["lon"], base_name=b["name"],
                bbox=None,
                has_transit=bool(gtfs),
                transit_operator=(gtfs[0]["name"] if gtfs else None),
                default_depart=c.get("depart"),
                region=REGION.get(slug),
                user_created=False,            # curated catalog (protected from delete)
            ))
            n_cities += 1

            pois_file = DATA / f"pois.{slug}.json"
            if pois_file.exists():
                for pid, f in json.loads(pois_file.read_text()).items():
                    db.merge(m.POI(
                        city_slug=slug, id=pid,
                        name=f["name"], lat=f["lat"], lon=f["lon"],
                        dwell_min=f.get("dwell_min", 60),
                        importance=f.get("importance", 0.5),
                        hours=f.get("hours"),
                        tags=f.get("tags", []),
                        notes=f.get("notes"),
                        status=f.get("status", "idea"),
                    ))
                    n_pois += 1
        db.commit()
    print(f"seeded {n_cities} cities, {n_pois} POIs")


if __name__ == "__main__":
    main()
