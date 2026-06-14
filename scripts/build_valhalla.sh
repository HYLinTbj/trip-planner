#!/usr/bin/env bash
# Prepare + serve the self-hosted Valhalla engine (the app's default routing).
# Reuses the Kansai extract if the OSRM build already fetched it; else downloads it.
# The gis-ops image auto-builds routing tiles from the .pbf on first `docker compose up`.
set -euo pipefail
cd "$(dirname "$0")/.."

PBF_URL="${PBF_URL:-https://download.geofabrik.de/asia/japan/kansai-latest.osm.pbf}"
VDIR="data/valhalla"
PBF="$VDIR/region.osm.pbf"
SHARED="data/osrm/region.osm.pbf"   # the OSRM build may have already downloaded this
mkdir -p "$VDIR"

if [ -f "$PBF" ]; then
  echo ">> Using cached extract: $PBF"
elif [ -f "$SHARED" ]; then
  echo ">> Reusing the OSRM extract: $SHARED"
  ln -f "$SHARED" "$PBF"
else
  echo ">> Downloading extract: $PBF_URL"
  curl -L --fail -o "$PBF.part" "$PBF_URL"
  sz=$(stat -c%s "$PBF.part" 2>/dev/null || echo 0)
  if [ "$sz" -lt 1000000 ]; then   # a wrong region slug 302-redirects to a small HTML page
    echo "!! Downloaded only $sz bytes — not a PBF. Check the region slug at" >&2
    echo "!! https://download.geofabrik.de/asia/japan.html and pass PBF_URL=..." >&2
    rm -f "$PBF.part"; exit 1
  fi
  mv "$PBF.part" "$PBF"
fi
chmod -R 777 "$VDIR"   # the container builds tiles into this dir

echo ">> Starting Valhalla (auto-builds tiles on first run — a few minutes)…"
docker compose up -d
echo ">> Watch the build:  docker compose logs -f valhalla"
echo ">> Ready when:        curl -s localhost:8002/status"
