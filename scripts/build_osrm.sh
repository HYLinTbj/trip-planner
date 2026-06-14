#!/usr/bin/env bash
# Build an OSRM routing graph for ONE region (the spike's self-hosted data path).
# Requires Docker (see README for enabling Docker Desktop's WSL integration).
#
# Usage:
#   PBF_URL=<url> PROFILE=foot bash scripts/build_osrm.sh
#
# Defaults to the Kansai extract (covers Kyoto/Osaka). For a much smaller /
# faster build, draw a bounding box at https://extract.bbbike.org/ and pass its
# .osm.pbf URL as PBF_URL.
set -euo pipefail

PBF_URL="${PBF_URL:-https://download.geofabrik.de/asia/japan/kansai-latest.osm.pbf}"
PROFILE="${PROFILE:-foot}"                 # foot | car | bicycle (one mode per build)
IMAGE="${OSRM_IMAGE:-osrm/osrm-backend:latest}"

DATA_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/osrm"
OUT="$DATA_DIR/$PROFILE"          # one graph per profile -> foot (:5000), car (:5001)
mkdir -p "$OUT"
PBF="$DATA_DIR/region.osm.pbf"    # the extract is downloaded once and shared

if [ ! -f "$PBF" ]; then
  echo ">> Downloading extract: $PBF_URL"
  curl -L --fail -o "$PBF.part" "$PBF_URL"
  sz=$(stat -c%s "$PBF.part" 2>/dev/null || echo 0)
  if [ "$sz" -lt 1000000 ]; then   # a wrong region slug 302-redirects to a small HTML page
    echo "!! Downloaded only $sz bytes — not a PBF. Check the region slug at" >&2
    echo "!! https://download.geofabrik.de/asia/japan.html and pass PBF_URL=..." >&2
    rm -f "$PBF.part"; exit 1
  fi
  mv "$PBF.part" "$PBF"
else
  echo ">> Using cached extract: $PBF"
fi

# Build into the per-profile dir so foot and car graphs can run side by side.
ln -f "$PBF" "$OUT/region.osm.pbf"   # hardlink the shared extract in (no extra disk)
run_osrm() { docker run --rm -t -v "$OUT:/data" "$IMAGE" "$@"; }

echo ">> osrm-extract (profile: $PROFILE) -> data/osrm/$PROFILE/"
run_osrm osrm-extract -p "/opt/$PROFILE.lua" /data/region.osm.pbf
echo ">> osrm-partition"
run_osrm osrm-partition /data/region.osrm
echo ">> osrm-customize"
run_osrm osrm-customize /data/region.osrm
rm -f "$OUT/region.osm.pbf"           # keep only the built graph in the profile dir

echo ">> Done ($PROFILE). Build the other mode with:  PROFILE=car bash scripts/build_osrm.sh"
echo ">> Start both servers with:  docker compose up -d"
