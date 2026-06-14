#!/usr/bin/env python3
"""Onboard a city's routing data — one repeatable command.

Turns a `data/cities.json` entry (OSM extract + GTFS feeds) into a self-hosted
Valhalla bundle that serves walk / drive / transit, baking in the lessons from the
transit spike:
  * cap CPUs so transit ingest doesn't OOM            (ONBOARD_CPUSET, default 0-3)
  * optionally trim a GTFS feed to certain route_types so a sprawly/multi-operator
    feed fits the street extract (else `enhance` segfaults)
  * tile files are root-owned -> wipe via a root container
  * pre-built tiles SERVE fast: re-running just starts the container (load != build)

    python scripts/onboard_city.py <city> [--rebuild]

Builds into data/cities/<city>/ and serves on the catalog `port`.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "cities.json"
IMAGE = os.environ.get("VALHALLA_IMAGE", "ghcr.io/gis-ops/docker-valhalla/valhalla:latest")
CPUSET = os.environ.get("ONBOARD_CPUSET", "0-3")  # fewer threads -> lower peak memory


def _curl(url: str, dest: Path) -> None:
    """Download (follows redirects, retries) — robust for large extracts."""
    subprocess.run(["curl", "-L", "--fail", "--retry", "3", "--retry-delay", "2",
                    "-A", "trip-planner-onboard/0.1", "-o", str(dest), url], check=True)


def download_extract(url: str, dest: Path) -> None:
    print(f">> extract  <- {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    _curl(url, tmp)
    if tmp.stat().st_size < 1_000_000:          # a wrong slug redirects to a small HTML page
        tmp.unlink(missing_ok=True)
        sys.exit("!! extract is < 1 MB — not a real .pbf. Check pbf_url for this city.")
    tmp.replace(dest)


def fetch_gtfs(feed: dict, gdir: Path) -> None:
    sub = gdir / feed["name"]
    sub.mkdir(parents=True, exist_ok=True)
    print(f">> gtfs {feed['name']}  <- {feed['url']}")
    tmp = gdir / f"{feed['name']}.zip"
    _curl(feed["url"], tmp)
    zipfile.ZipFile(tmp).extractall(sub)
    tmp.unlink(missing_ok=True)
    if feed.get("route_types"):
        _trim(sub, {str(t) for t in feed["route_types"]})


def _trim(d: Path, keep_types: set[str]) -> None:
    """Keep only routes whose route_type is in keep_types (+ their trips/stops/shapes),
    so a sprawly feed stays inside the street extract (avoids the enhance segfault)."""
    rd = lambda f: list(csv.DictReader(open(d / f, encoding="utf-8-sig")))

    def wr(f, rows, fields):
        with open(d / f, "w", newline="", encoding="utf-8") as o:
            w = csv.DictWriter(o, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})

    routes = rd("routes.txt")
    rf = list(routes[0].keys())
    keepR = {r["route_id"] for r in routes if r.get("route_type") in keep_types}
    trips = rd("trips.txt")
    tf = list(trips[0].keys())
    trips = [t for t in trips if t["route_id"] in keepR]
    keepT = {t["trip_id"] for t in trips}
    keepShp = {t.get("shape_id") for t in trips}
    fin = open(d / "stop_times.txt", encoding="utf-8-sig")
    rdr = csv.DictReader(fin)
    stf = rdr.fieldnames
    st = [r for r in rdr if r["trip_id"] in keepT]
    fin.close()
    keepStop = {r["stop_id"] for r in st}
    stops = rd("stops.txt")
    sf = list(stops[0].keys())
    parents = {s.get("parent_station") for s in stops if s["stop_id"] in keepStop and s.get("parent_station")}
    stops = [s for s in stops if s["stop_id"] in keepStop or s["stop_id"] in parents]
    wr("routes.txt", [r for r in routes if r["route_id"] in keepR], rf)
    wr("trips.txt", trips, tf)
    wr("stop_times.txt", st, stf)
    wr("stops.txt", stops, sf)
    if (d / "shapes.txt").exists():
        sh = rd("shapes.txt")
        wr("shapes.txt", [r for r in sh if r.get("shape_id") in keepShp], list(sh[0].keys()))
    print(f"   trimmed to route_types {sorted(keep_types)}: {len(keepR)} routes, {len(trips)} trips")


def _name(city: str) -> str:
    return f"valhalla-{city}"


def _docker(*args, **kw):
    return subprocess.run(["docker", *args], **kw)


def wipe_tiles(custom: Path) -> None:
    """Tile files are root-owned -> remove them via a root container (keep the .pbf)."""
    _docker("run", "--rm", "--user", "0", "--entrypoint", "sh", "-v", f"{custom}:/c", IMAGE,
            "-c", "find /c -mindepth 1 ! -name region.osm.pbf -exec rm -rf {} +", check=True)


def start(city: str, port: int, custom: Path, gtfs: Path, build: bool) -> None:
    _docker("rm", "-f", _name(city), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    args = ["run", "-d", "--name", _name(city), "-p", f"{port}:8002", "--cpuset-cpus", CPUSET,
            "-v", f"{custom}:/custom_files", "-v", f"{gtfs}:/gtfs_feeds"]
    if build:
        args += ["-e", "build_time_zones=True", "-e", "build_admins=True"]
        if gtfs.exists() and any(gtfs.iterdir()):   # only build transit when feeds are present —
            args += ["-e", "build_transit=True"]     # else valhalla_ingest_transit aborts the build
    args.append(IMAGE)
    _docker(*args, check=True, stdout=subprocess.DEVNULL)


def wait_status(port: int, minutes: int = 20) -> bool:
    url = f"http://localhost:{port}/status"
    for _ in range((minutes * 60) // 5):
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except Exception:
            time.sleep(5)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("city")
    ap.add_argument("--rebuild", action="store_true", help="wipe tiles and rebuild from scratch")
    a = ap.parse_args()

    catalog = json.loads(CATALOG.read_text())
    if a.city not in catalog:
        sys.exit(f"unknown city '{a.city}'. catalog: {', '.join(catalog)}")
    c = catalog[a.city]
    port = c["port"]
    cdir = ROOT / "data" / "cities" / a.city
    custom, gtfs = cdir / "custom", cdir / "gtfs"
    custom.mkdir(parents=True, exist_ok=True)
    gtfs.mkdir(parents=True, exist_ok=True)
    pbf = custom / "region.osm.pbf"

    tiles_exist = (custom / "valhalla_tiles").exists() or (custom / "valhalla_tiles.tar").exists()
    if tiles_exist and not a.rebuild:
        print(f">> {a.city}: tiles exist -> serving (no rebuild). Use --rebuild to force.")
        start(a.city, port, custom, gtfs, build=False)
    else:
        if tiles_exist:
            wipe_tiles(custom)
        if not pbf.exists():
            download_extract(c["pbf_url"], pbf)
        for feed in c.get("gtfs", []):
            fetch_gtfs(feed, gtfs)
        subprocess.run(["chmod", "-R", "777", str(cdir)], stderr=subprocess.DEVNULL)
        print(f">> building Valhalla for {a.city} (transit, cpuset {CPUSET}) on :{port} — a few minutes…")
        start(a.city, port, custom, gtfs, build=True)

    print(f">> waiting for :{port}/status …")
    ok = wait_status(port)
    where = f"http://localhost:{port}"
    print(f">> {a.city}: {'READY at ' + where if ok else 'still building — check: docker logs ' + _name(a.city)}")


if __name__ == "__main__":
    main()
