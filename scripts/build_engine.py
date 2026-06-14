#!/usr/bin/env python3
"""Build a REGIONAL Valhalla engine (scale-up phase 2).

A region (data/regions.json) = ONE pre-merged Geofabrik US census-region extract
(us-northeast/midwest/south/west) + its GTFS feeds, built into one Valhalla
tileset served on the region's port. The api routes each request to a region by
the city's `region`.

NB: we deliberately use Geofabrik's single merged regional PBF rather than
downloading per-state extracts and feeding several files to valhalla_build_tiles.
Multi-PBF tile building is buggy (valhalla/valhalla#3925 — border tiles whose data
spans two extracts crash with a vector range_check, e.g. the NY/NJ tile 2/754983);
a single merged extract sidesteps it entirely.

Build ONE region at a time and watch `docker stats`. Uses the official Valhalla
3.7.0 image (gis-ops's is frozen at 3.5.1) — see scripts/valhalla_build.sh.

    python scripts/build_engine.py <region> [--rebuild]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onboard_city as oc  # noqa: E402  (reuse _curl/download_extract/fetch_gtfs/start/wait_status)

ROOT = Path(__file__).resolve().parents[1]
REGIONS = ROOT / "data" / "regions.json"
SCRIPTS = ROOT / "scripts"

# Official Valhalla image, pinned to the 3.7.0 digest. gis-ops/docker-valhalla
# (used by onboard_city.py) is frozen at 3.5.1, whose tile builder core-dumps on
# dense-metro `level=` tags (e.g. the NYC tile 2/754983/0); 3.7.0 builds it fine.
# The official image has no auto-build entrypoint, so we drive the build (via
# scripts/valhalla_build.sh) and the service ourselves.
IMAGE = "ghcr.io/valhalla/valhalla@sha256:ef65407f8cae345084a1be3eae450d3e985ecba01778dc0d96b8c9caa935cdc6"
CPUSET = oc.CPUSET


def _name(region: str) -> str:
    return f"valhalla-{region}"


def _wipe(custom: Path) -> None:
    """Remove built tiles (root-owned) but keep the downloaded *.osm.pbf extracts.
    -maxdepth 1 so find only removes top-level entries (and rm -rf their subtrees) —
    without it, find descends into dirs rm has already deleted and exits non-zero."""
    oc._docker("run", "--rm", "--user", "0", "--entrypoint", "sh", "-v", f"{custom}:/c", IMAGE,
               "-c", "find /c -mindepth 1 -maxdepth 1 ! -name '*.osm.pbf' -exec rm -rf {} +",
               check=True)


def _build(region: str, custom: Path, gtfs: Path, want_transit: bool) -> None:
    """Ephemeral container: run the full Valhalla build to completion, then exit."""
    arg = "transit" if want_transit else "street"
    oc._docker("run", "--rm", "--name", f"{_name(region)}-build", "--cpuset-cpus", CPUSET,
               "-v", f"{custom}:/custom_files", "-v", f"{gtfs}:/gtfs_feeds",
               "-v", f"{SCRIPTS}:/scripts:ro", "--entrypoint", "bash", IMAGE,
               "/scripts/valhalla_build.sh", arg, check=True)


def _serve(region: str, port: int, custom: Path) -> None:
    """Detached container: serve the already-built tiles on :port (8002 inside)."""
    oc._docker("rm", "-f", _name(region), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    oc._docker("run", "-d", "--name", _name(region), "-p", f"{port}:8002",
               "--restart", "unless-stopped", "-v", f"{custom}:/custom_files",
               "--entrypoint", "valhalla_service", IMAGE,
               "/custom_files/valhalla.json", "2", check=True, stdout=subprocess.DEVNULL)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("region")
    ap.add_argument("--rebuild", action="store_true", help="wipe tiles and rebuild from scratch")
    a = ap.parse_args()

    regions = json.loads(REGIONS.read_text())
    if a.region not in regions:
        sys.exit(f"unknown region '{a.region}'. known: {', '.join(regions)}")
    r = regions[a.region]
    port = r["port"]
    rdir = ROOT / "data" / "regions" / a.region
    custom, gtfs = rdir / "custom", rdir / "gtfs"
    custom.mkdir(parents=True, exist_ok=True)
    gtfs.mkdir(parents=True, exist_ok=True)

    tiles_exist = (custom / "valhalla_tiles").exists()
    if tiles_exist and not a.rebuild:
        print(f">> {a.region}: tiles exist -> serving (no rebuild). Use --rebuild to force.")
    else:
        if tiles_exist:
            _wipe(custom)
        pbf = custom / "region.osm.pbf"
        if not pbf.exists():
            oc.download_extract(r["pbf_url"], pbf)
        for feed in r.get("gtfs", []):
            oc.fetch_gtfs(feed, gtfs)
        subprocess.run(["chmod", "-R", "777", str(rdir)], stderr=subprocess.DEVNULL)
        want_transit = bool(r.get("gtfs"))
        print(f">> building {a.region}: 1 merged extract + {len(r.get('gtfs', []))} GTFS "
              f"(cpuset {CPUSET}, valhalla 3.7) on :{port} — this can take a while…")
        _build(a.region, custom, gtfs, want_transit)   # blocks until the build finishes

    _serve(a.region, port, custom)
    print(f">> waiting for :{port}/status …")
    ok = oc.wait_status(port, minutes=3)    # tiles already built -> service comes up fast
    where = f"http://localhost:{port}"
    print(f">> {a.region}: {'READY at ' + where if ok else 'service slow — docker logs ' + _name(a.region)}")


if __name__ == "__main__":
    main()
